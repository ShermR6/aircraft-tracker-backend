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

GROUND_STATION_TIMEOUT_SECONDS = 180  # 3 minutes


def is_ground_station_online(user_id_str: str) -> bool:
    last_seen = ground_last_seen.get(user_id_str)
    if last_seen is None:
        return False
    return (datetime.utcnow() - last_seen).total_seconds() < GROUND_STATION_TIMEOUT_SECONDS
