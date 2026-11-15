"""
Shared in-memory state for ground station heartbeat and range data.
Imported by both main.py and tracker.py to avoid circular imports.
"""
from datetime import datetime

# { user_id_str: datetime } — updated by heartbeat and ingest endpoints
ground_last_seen: dict = {}

# { user_id_str: {"range_nm": [...], "updated_at": str} }
ground_range: dict = {}

# { user_id_str: { icao24: { lat, lon, altitude, speed, heading, on_ground, updated_at } } }
ground_positions: dict = {}

# { user_id_str: datetime } — when the latest batch of positions arrived
ground_positions_at: dict = {}

# { user_id_str: { icao24: (fix_dict, datetime) } } — each aircraft's last
# position from the receiver and when it was heard, kept after the batch moves
# on, so the live map can hold an aircraft through a short gap rather than
# fall back to an older network position.
ground_last_fix: dict = {}
LAST_FIX_MAX_AGE_SECONDS = 1800

GROUND_STATION_TIMEOUT_SECONDS = 180  # 3 minutes

# The receiver sends positions every 5 seconds, but only while it can hear a
# tracked aircraft. Once it stops, the last batch is no longer where anything is.
GROUND_POSITION_MAX_AGE_SECONDS = 30


def is_ground_station_online(user_id_str: str) -> bool:
    last_seen = ground_last_seen.get(user_id_str)
    if last_seen is None:
        return False
    return (datetime.utcnow() - last_seen).total_seconds() < GROUND_STATION_TIMEOUT_SECONDS


def fix_time(fix: dict, default: datetime) -> datetime:
    """When the receiver heard this position: its own `updated_at` (agents
    3.8+ give each aircraft its real time), else when the batch arrived."""
    stamp = fix.get("updated_at") if isinstance(fix, dict) else None
    if stamp:
        try:
            return datetime.fromisoformat(str(stamp).replace("Z", "")).replace(tzinfo=None)
        except ValueError:
            pass
    return default


def remember_fixes(user_id_str: str, positions: dict, at: datetime) -> None:
    """Keep each aircraft's newest receiver fix, dropping ones past their age."""
    kept = ground_last_fix.setdefault(user_id_str, {})
    for icao, fix in positions.items():
        if not isinstance(fix, dict) or fix.get("lat") is None or fix.get("lon") is None:
            continue
        t = fix_time(fix, at)
        old = kept.get(icao)
        if old is None or t >= old[1]:
            kept[icao] = (fix, t)
    for icao in [i for i, (_, t) in kept.items() if (at - t).total_seconds() > LAST_FIX_MAX_AGE_SECONDS]:
        del kept[icao]


def fresh_ground_positions(user_id_str: str, now: datetime = None):
    """The receiver's latest positions and when they arrived: ({icao24: {...}}, datetime).

    Returns ({}, None) when the last batch is too old to trust. Without this an
    aircraft that flew out of the receiver's range stayed on the map at its
    last position, marked as seen just now, for as long as the receiver was online.
    """
    at = ground_positions_at.get(user_id_str)
    if at is None:
        return {}, None
    if ((now or datetime.utcnow()) - at).total_seconds() > GROUND_POSITION_MAX_AGE_SECONDS:
        return {}, None
    return ground_positions.get(user_id_str, {}), at
