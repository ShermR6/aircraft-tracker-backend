"""Coverage gaps: every time a tracked aircraft near its account's airport went
unheard by every source the tracker uses, for how long, where, how low, and
which source picked it up again. Also how each adsb.lol request went.

The point is to know where the gaps are before paying for more data: whether
they are low and close to the field (a receiver or MLAT fixes those), far out
(another network fixes those), or our own failed requests to adsb.lol.

The tracker calls observe() for each tracked aircraft every cycle and flush()
once per cycle. Nothing here can stop tracking: every database write is
wrapped, and a failure only prints.
"""
from collections import deque
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, asin
from typing import Dict, List, Optional, Tuple

# Only aircraft airborne within this distance of the airport count: further
# out nobody is waiting on an alert.
GAP_RADIUS_NM = 25.0
# One missed poll (about 60 s between fixes, a little more on a slow cycle)
# is normal. Two or more in a row is a gap.
GAP_MIN_SECONDS = 80
# Unheard this long: logged as never seen again (it landed below coverage, or
# shut down, or left).
GAP_GIVE_UP_SECONDS = 30 * 60
KEEP_DAYS = 90
FETCH_HISTORY = 2880 * 2          # two days of 30 s cycles

# (owner, icao24) -> the newest position any source had.
_last: Dict[Tuple[str, str], dict] = {}
_pending: List[dict] = []
fetches: deque = deque(maxlen=FETCH_HISTORY)
_last_prune: Optional[datetime] = None


def _nm(lat1, lon1, lat2, lon2) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * asin(sqrt(a)) * 3440.065


def position(source: str, lat, lon, at: datetime, alt_ft=None, on_ground=False, kind: Optional[str] = None) -> Optional[dict]:
    """One source's position, or None when it has no usable one."""
    if lat is None or lon is None or at is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    alt = alt_ft if isinstance(alt_ft, (int, float)) else None
    return {'source': source, 'kind': kind or source, 'lat': lat, 'lon': lon, 'at': at,
            'alt': alt, 'on_ground': bool(on_ground)}


def _place(p: dict, airspace: dict) -> dict:
    """Distance from the airport and height above it."""
    try:
        clat, clon = float(airspace['center_lat']), float(airspace['center_lon'])
        nm = round(_nm(p['lat'], p['lon'], clat, clon), 2)
    except (KeyError, TypeError, ValueError):
        nm = None
    field = float(airspace.get('field_elevation_ft_msl') or 0)
    agl = None if p['alt'] is None or p['on_ground'] else int(p['alt'] - field)
    return dict(p, nm=nm, agl=agl)


def _counts(p: dict) -> bool:
    return (not p['on_ground'] and p.get('nm') is not None and p['nm'] <= GAP_RADIUS_NM)


def observe(owner: str, icao24: str, tail: str, airspace: dict, positions: List[Optional[dict]],
            adsb_ok: bool, receiver_up: bool, now: Optional[datetime] = None) -> None:
    """One tracker cycle for one aircraft. positions holds what each source
    had this cycle (None where it had nothing)."""
    now = now or datetime.utcnow()
    key = (owner, icao24)
    prev = _last.get(key)
    have = [p for p in positions if p is not None]
    newest = max(have, key=lambda p: p['at']) if have else None

    if prev is None:
        if newest is not None:
            _last[key] = _start(newest, airspace)
        return

    # A source re-reporting the same old fix is not news: adsb.lol gives the
    # age of its last position, which shifts by a fraction of a second.
    if newest is not None and (newest['at'] - prev['at']).total_seconds() > 2:
        quiet = (newest['at'] - prev['at']).total_seconds()
        if quiet >= GAP_MIN_SECONDS and _counts(prev) and not prev.get('logged'):
            _pending.append(_row(owner, icao24, tail, prev, _place(newest, airspace), quiet, 'back'))
        _last[key] = _start(newest, airspace)
        return

    # Nothing newer this cycle.
    prev['missed'] += 1
    if not adsb_ok:
        prev['fetch_failed'] += 1
    if receiver_up:
        prev['receiver_up'] += 1
    quiet = (now - prev['at']).total_seconds()
    if quiet >= GAP_GIVE_UP_SECONDS and _counts(prev) and not prev.get('logged'):
        _pending.append(_row(owner, icao24, tail, prev, None, quiet, 'not_seen_again'))
        prev['logged'] = True


def _start(p: dict, airspace: dict) -> dict:
    return dict(_place(p, airspace), missed=0, fetch_failed=0, receiver_up=0)


def _row(owner, icao24, tail, prev, end, seconds, outcome) -> dict:
    missed = prev['missed']
    if prev['receiver_up'] == 0:
        receiver = 'down'
    elif prev['receiver_up'] >= missed:
        receiver = 'up'
    else:
        receiver = 'part'
    return {
        'owner': owner, 'icao24': icao24, 'tail': tail, 'outcome': outcome,
        'started_at': prev['at'], 'ended_at': end['at'] if end else None, 'seconds': int(seconds),
        'start_lat': prev['lat'], 'start_lon': prev['lon'], 'start_nm': prev['nm'],
        'start_agl': prev['agl'], 'start_source': prev['kind'],
        'end_lat': end['lat'] if end else None, 'end_lon': end['lon'] if end else None,
        'end_nm': end['nm'] if end else None, 'end_agl': end['agl'] if end else None,
        'end_on_ground': end['on_ground'] if end else None, 'end_source': end['kind'] if end else None,
        'missed_cycles': missed, 'fetch_failures': prev['fetch_failed'], 'receiver': receiver,
    }


def forget(owner: str) -> None:
    for key in [k for k in _last if k[0] == owner]:
        _last.pop(key, None)


def record_fetch(ok: bool, attempts: int, seconds: float, error: Optional[str], aircraft: int,
                 at: Optional[datetime] = None) -> None:
    fetches.append({'at': at or datetime.utcnow(), 'ok': ok, 'attempts': attempts,
                    'seconds': round(seconds, 2), 'error': error, 'aircraft': aircraft})


def fetch_summary(hours: float = 24, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    rows = [f for f in fetches if f['at'] >= now - timedelta(hours=hours)]
    times = sorted(f['seconds'] for f in rows if f['ok'])
    errors: Dict[str, int] = {}
    for f in rows:
        if f['error']:
            errors[f['error']] = errors.get(f['error'], 0) + 1

    def pct(q):
        return times[min(len(times) - 1, int(q * len(times)))] if times else None

    return {
        'hours': hours,
        'requests': len(rows),
        'ok_first_try': sum(1 for f in rows if f['ok'] and f['attempts'] == 1),
        'ok_after_retry': sum(1 for f in rows if f['ok'] and f['attempts'] > 1),
        'failed': sum(1 for f in rows if not f['ok']),
        'median_seconds': pct(0.5),
        'p95_seconds': pct(0.95),
        'errors': errors,
        'since': rows[0]['at'].isoformat() if rows else None,
    }


def flush(db_factory) -> None:
    """Writes the gaps found this cycle, and once a day drops old ones."""
    global _last_prune
    if not _pending and _last_prune and datetime.utcnow() - _last_prune < timedelta(days=1):
        return
    try:
        from models import CoverageGap
        db = db_factory()
        try:
            for r in _pending:
                db.add(CoverageGap(**r))
            if _last_prune is None or datetime.utcnow() - _last_prune >= timedelta(days=1):
                db.query(CoverageGap).filter(
                    CoverageGap.started_at < datetime.utcnow() - timedelta(days=KEEP_DAYS)).delete()
                _last_prune = datetime.utcnow()
            db.commit()
            _pending.clear()
        finally:
            db.close()
    except Exception as e:
        print(f"Coverage gaps not saved: {type(e).__name__}: {e}")
        # Keep them for the next cycle, but never let the list grow unbounded.
        del _pending[:-500]


def band(value, edges, labels):
    if value is None:
        return 'unknown'
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


NM_BANDS = ((2, 5, 10, float('inf')), ('under 2 nm', '2 to 5 nm', '5 to 10 nm', '10 to 25 nm'))
AGL_BANDS = ((500, 1500, 3000, float('inf')), ('under 500 ft', '500 to 1,500 ft', '1,500 to 3,000 ft', 'over 3,000 ft'))
