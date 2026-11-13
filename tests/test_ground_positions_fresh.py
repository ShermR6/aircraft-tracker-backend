"""The receiver only reports while it can hear a tracked aircraft, so its last
batch of positions must stop being used once it goes quiet."""
from datetime import datetime, timedelta

import ground_state as gs


def setup_function():
    gs.ground_positions.clear()
    gs.ground_positions_at.clear()


def test_no_positions_reported():
    assert gs.fresh_ground_positions("u1") == ({}, None)


def test_positions_without_a_receive_time_are_not_used():
    gs.ground_positions["u1"] = {"abc123": {"lat": 33.2, "lon": -97.2}}
    assert gs.fresh_ground_positions("u1") == ({}, None)


def test_recent_positions_come_back_with_their_receive_time():
    at = datetime.utcnow()
    gs.ground_positions["u1"] = {"abc123": {"lat": 33.2, "lon": -97.2}}
    gs.ground_positions_at["u1"] = at
    positions, seen = gs.fresh_ground_positions("u1", now=at + timedelta(seconds=10))
    assert positions == {"abc123": {"lat": 33.2, "lon": -97.2}}
    assert seen == at


def test_positions_are_dropped_once_the_receiver_goes_quiet():
    at = datetime.utcnow()
    gs.ground_positions["u1"] = {"abc123": {"lat": 33.2, "lon": -97.2}}
    gs.ground_positions_at["u1"] = at
    late = at + timedelta(seconds=gs.GROUND_POSITION_MAX_AGE_SECONDS + 1)
    assert gs.fresh_ground_positions("u1", now=late) == ({}, None)
