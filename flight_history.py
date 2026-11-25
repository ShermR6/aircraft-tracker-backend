"""Recorded flights, kept 48 hours, for replaying a flight from start to finish
on the Live Map (2026-11-24).

Every position the tracker gets for a tracked aircraft is recorded with its
altitude, speed and track: from adsb.lol and FlyItalyADSB every cycle, and
from the account's receiver every second while it hears the aircraft. A
flight starts when the aircraft first moves and ends after it has sat on the
ground for 10 minutes or gone unheard for 30, the same rules as the Live Map's
trail, so pattern work with touch-and-goes is one flight.

When a flight is replayed, FlyItalyADSB's track for it (a point about every
5 seconds) fills in between the network polls: see fill_from_history.

Worked on in memory and saved to the database every tracker cycle; a restart
picks the open flights back up. Nothing here can stop tracking.
"""
import bisect
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

KEEP_HOURS = float(os.getenv("FLIGHT_HISTORY_HOURS", "48"))
GROUND_ENDS_FLIGHT = timedelta(minutes=10)
SILENCE_ENDS_FLIGHT = timedelta(minutes=30)
PARKED_KT = 3                      # slower than this on the ground is standing still
MAX_POINTS = 20000                 # about 5 hours of one fix a second
# Closest two points may be, by source: the receiver is kept at full rate for
# smooth landings and taxiing; standing still on the ground, far less.
MIN_GAP_S = {"r": 1.0, "a": 3.0, "f": 3.0, "h": 3.0}
PARKED_GAP_S = 15.0
SOURCE_CODES = {"receiver": "r", "adsb.lol": "a", "adsb": "a", "FlyItalyADSB": "f", "history": "h"}

# (owner, icao24) -> open flight:
# {"id", "tail", "points": [[t, lat, lon, alt, gs, trk, ground, src]], "last", "ground_since", "dirty"}
_open: Dict[Tuple[str, str], dict] = {}
_closed: List[dict] = []           # ended since the last save, still to be written
_last_prune: Optional[datetime] = None


def _epoch(at: datetime) -> float:
    return (at - datetime(1970, 1, 1)).total_seconds()


def _num(x) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if x == x else None


def record(owner: str, icao24: str, tail: str, at: datetime, lat, lon, alt_ft=None,
           speed_kt=None, track=None, on_ground=False, source: str = "adsb.lol") -> None:
    """One position for a tracked aircraft. Never raises."""
    try:
        _record(owner, icao24, tail, at, lat, lon, alt_ft, speed_kt, track, on_ground, source)
    except Exception as e:
        print(f"Flight history: position skipped ({type(e).__name__}: {e})")


def _record(owner, icao24, tail, at, lat, lon, alt_ft, speed_kt, track, on_ground, source):
    lat, lon = _num(lat), _num(lon)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return
    if not icao24 or not isinstance(icao24, str):
        return
    key = (str(owner), icao24.lower())
    t = _epoch(at)
    gs = _num(speed_kt)
    grounded = bool(on_ground)
    parked = grounded and (gs is None or gs < PARKED_KT)
    f = _open.get(key)

    if f is not None and _ended(f, t):
        _close(key)
        f = None
    if f is None:
        if parked:
            return                 # a parked aircraft is not a flight
        f = {"id": None, "tail": tail, "points": [], "last": t,
             "ground_since": t if grounded else None, "dirty": True}
        _open[key] = f

    src = SOURCE_CODES.get(source, "a")
    gap = PARKED_GAP_S if parked else MIN_GAP_S.get(src, 3.0)
    pts = f["points"]
    times = [p[0] for p in pts[-50:]]
    i = bisect.bisect_left(times, t)
    near = [times[j] for j in (i - 1, i) if 0 <= j < len(times)]
    if any(abs(t - x) < gap for x in near):
        return
    if len(pts) >= MAX_POINTS:
        return
    alt = _num(alt_ft)
    point = [round(t, 1), round(lat, 5), round(lon, 5),
             None if grounded or alt is None else int(alt),
             None if gs is None else int(round(gs)),
             None if _num(track) is None else int(round(float(track))) % 360,
             1 if grounded else 0, src]
    if not pts or t >= pts[-1][0]:
        pts.append(point)
    else:
        at_i = len(pts) - len(times) + i
        pts.insert(at_i, point)
    if t > f["last"]:
        f["last"] = t
        if grounded:
            f["ground_since"] = f["ground_since"] or t
        else:
            f["ground_since"] = None
    f["tail"] = tail or f["tail"]
    f["dirty"] = True


def _ended(f: dict, t: float) -> bool:
    if t - f["last"] >= SILENCE_ENDS_FLIGHT.total_seconds():
        return True
    gs = f["ground_since"]
    return gs is not None and t - gs >= GROUND_ENDS_FLIGHT.total_seconds()


def _close(key) -> None:
    f = _open.pop(key, None)
    if f is not None:
        _closed.append(dict(f, owner=key[0], icao24=key[1], ended=True))


def _airborne(points) -> bool:
    return any(p[6] == 0 and (p[4] is None or p[4] > 30) for p in points)


def save(db, now: Optional[datetime] = None) -> None:
    """Writes changed and ended flights, closes ones that went quiet, and once
    an hour drops flights older than KEEP_HOURS. Never raises."""
    global _last_prune
    now = now or datetime.utcnow()
    try:
        from models import FlightHistory
        t_now = _epoch(now)
        for key in [k for k, f in _open.items() if _ended(f, t_now)]:
            _close(key)
        todo = [dict(f, owner=k[0], icao24=k[1], ended=False) for k, f in _open.items() if f["dirty"]] + _closed
        for f in todo:
            pts = f["points"]
            if not pts:
                continue
            row = db.get(FlightHistory, f["id"]) if f["id"] else None
            if not _airborne(pts):
                # Taxi only, so far. Not a flight to replay; an ended one is dropped.
                if row is not None and f["ended"]:
                    db.delete(row)
                continue
            if row is None:
                row = FlightHistory(owner=f["owner"], icao24=f["icao24"])
                db.add(row)
            row.tail = f["tail"]
            row.started_at = datetime(1970, 1, 1) + timedelta(seconds=pts[0][0])
            row.last_at = datetime(1970, 1, 1) + timedelta(seconds=pts[-1][0])
            row.ended = bool(f["ended"])
            row.points = list(pts)
            row.n_points = len(pts)
            db.flush()
            key = (f["owner"], f["icao24"])
            if not f["ended"] and key in _open:
                _open[key]["id"] = row.id
                _open[key]["dirty"] = False
        _closed.clear()
        if _last_prune is None or now - _last_prune >= timedelta(hours=1):
            db.query(FlightHistory).filter(
                FlightHistory.last_at < now - timedelta(hours=KEEP_HOURS)).delete()
            _last_prune = now
        db.commit()
    except Exception as e:
        print(f"Flight history not saved: {type(e).__name__}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        del _closed[:-200]


def load(db, now: Optional[datetime] = None) -> int:
    """At startup: flights still open pick up where they were."""
    from models import FlightHistory
    now = now or datetime.utcnow()
    rows = db.query(FlightHistory).filter(
        FlightHistory.ended == False,  # noqa: E712
        FlightHistory.last_at >= now - SILENCE_ENDS_FLIGHT).all()
    for r in rows:
        pts = list(r.points or [])
        if not pts:
            continue
        last = pts[-1]
        _open[(r.owner, r.icao24)] = {
            "id": r.id, "tail": r.tail, "points": pts, "last": last[0],
            "ground_since": last[0] if last[6] else None, "dirty": False}
    return len(rows)


# ── replay ───────────────────────────────────────────────────────────────────

def fill_from_history(points: List[list], traces: List[dict], pad_s: float = 120.0) -> List[list]:
    """Adds FlyItalyADSB track points (readsb trace_full: [offset, lat, lon,
    alt or "ground", gs, track, ...] after `timestamp`) that fall inside this
    flight and are more than 2 s from any point already recorded."""
    if not points:
        return points
    start, end = points[0][0] - pad_s, points[-1][0] + pad_s
    have = sorted(p[0] for p in points)
    extra = []
    for tr in traces:
        base = _num(tr.get("timestamp")) if isinstance(tr, dict) else None
        if base is None:
            continue
        for p in tr.get("trace") or []:
            if not isinstance(p, list) or len(p) < 6:
                continue
            off, lat, lon = _num(p[0]), _num(p[1]), _num(p[2])
            if off is None or lat is None or lon is None:
                continue
            t = base + off
            if t < start or t > end:
                continue
            i = bisect.bisect_left(have, t)
            if any(0 <= j < len(have) and abs(have[j] - t) < 2.0 for j in (i - 1, i)):
                continue
            ground = p[3] == "ground"
            alt = None if ground else _num(p[3])
            gs, trk = _num(p[4]), _num(p[5])
            extra.append([round(t, 1), round(lat, 5), round(lon, 5),
                          None if alt is None else int(alt),
                          None if gs is None else int(round(gs)),
                          None if trk is None else int(round(trk)) % 360,
                          1 if ground else 0, "h"])
    if not extra:
        return points
    return sorted(points + extra, key=lambda p: p[0])
