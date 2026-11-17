"""
Where each tracked aircraft has flown on its current flight, for the trail the
Live Map draws when an aircraft is picked.

Kept in memory only: a restart starts every trail again. Positions come from
both sources the tracker uses (the account's own receiver and adsb.lol), in
the order they were heard.

A flight is everything since the aircraft last sat on the ground for a while,
so pattern work with touch-and-goes is one flight, and so is a taxi-back. Its
trail stays after it lands, until it flies again.
"""
from datetime import datetime, timedelta
from typing import Optional

MAX_POINTS = 1500
MIN_MOVE_DEG = 0.0003            # about 30 m: ignores jitter while parked
MIN_SECONDS = 5                  # the receiver hears an aircraft every second
GROUND_ENDS_FLIGHT = timedelta(minutes=10)
SILENCE_ENDS_FLIGHT = timedelta(minutes=30)
FORGET_AFTER = timedelta(hours=2)
MAX_AIRCRAFT_PER_OWNER = 60

# { owner: { icao24: trail } }. A trail: points [(epoch_seconds, lat, lon)],
# last_at (when it was last heard), ground_since (None while airborne).
_trails: dict = {}


def _number(x) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if x == x else None


def _epoch(at: datetime) -> float:
    return (at - datetime(1970, 1, 1)).total_seconds()


def record(owner: str, icao24: str, lat, lon, on_ground, at: datetime) -> None:
    """Add a position. `at` is when it was heard, in UTC. One that is older
    than the newest already held is dropped, which is also what keeps the two
    sources from zig-zagging: while the receiver hears an aircraft its fixes
    are the newest, and the network's slightly older ones fall away."""
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return
    if not isinstance(icao24, str) or not icao24:
        return
    icao24 = icao24.lower()
    mine = _trails.setdefault(owner, {})
    trail = mine.get(icao24)

    if trail is not None and at <= trail["last_at"]:
        return
    if trail is not None and at - trail["last_at"] >= SILENCE_ENDS_FLIGHT:
        trail = None

    grounded = bool(on_ground)
    if trail is not None and not grounded and trail["ground_since"] is not None \
            and at - trail["ground_since"] >= GROUND_ENDS_FLIGHT:
        # Flying again after a stop: a new flight, starting where it was parked.
        parked = trail["points"][-1:]
        trail = {"points": list(parked), "last_at": at, "ground_since": None, "started": at}
        mine[icao24] = trail

    if trail is None:
        if icao24 not in mine and len(mine) >= MAX_AIRCRAFT_PER_OWNER:
            return
        trail = {"points": [], "last_at": at, "ground_since": at if grounded else None, "started": at}
        mine[icao24] = trail

    trail["last_at"] = at
    if grounded:
        if trail["ground_since"] is None:
            trail["ground_since"] = at
    else:
        trail["ground_since"] = None

    points = trail["points"]
    t = _epoch(at)
    if points:
        last_t, last_lat, last_lon = points[-1]
        if t - last_t < MIN_SECONDS:
            return
        if abs(lat - last_lat) < MIN_MOVE_DEG and abs(lon - last_lon) < MIN_MOVE_DEG:
            return
    points.append((t, lat, lon))
    if len(points) > MAX_POINTS:
        # A long flight keeps its whole shape: the older half loses every
        # second point, the recent half stays as it is.
        half = len(points) // 2
        trail["points"] = points[:half:2] + points[half:]


def get(owner: str, icao24: str, now: datetime = None) -> Optional[dict]:
    """The aircraft's flight so far: {"started_at": datetime, "points":
    [[lat, lon, epoch_seconds], ...]}, oldest first. None when there is none
    or it was last heard too long ago to be worth showing."""
    now = now or datetime.utcnow()
    mine = _trails.get(owner)
    if not mine:
        return None
    for key in [k for k, tr in mine.items() if now - tr["last_at"] > FORGET_AFTER]:
        del mine[key]
    if not mine:
        _trails.pop(owner, None)
        return None
    trail = mine.get((icao24 or "").lower())
    if trail is None or not trail["points"]:
        return None
    return {
        "started_at": trail["started"],
        "points": [[round(lat, 5), round(lon, 5), int(t)] for t, lat, lon in trail["points"]],
    }


def forget(owner: str = None) -> None:
    """Drop one owner's trails, or all of them."""
    if owner is None:
        _trails.clear()
    else:
        _trails.pop(owner, None)
