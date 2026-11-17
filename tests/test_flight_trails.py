"""The flight trail the Live Map draws when an aircraft is picked: the whole
flight so far, kept by the backend so it is there however late the map is
opened.

The rules are checked on the store itself; the rest drives the real tracker
cycle and endpoints with the fake adsb.lol from test_gs_and_adsb_together.
"""
from datetime import datetime, timedelta

import flight_trails as ft
import test_api_integration as api
import test_gs_and_adsb_together as both
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

client = api.client
UID, ICAO, TAIL = both.UID, both.ICAO, both.TAIL
T0 = datetime(2026, 11, 20, 14, 0, 0)


def setup_function():
    ft.forget()


def _fly(owner, icao, start, steps, every_s=10, lat0=33.0, on_ground=False):
    """`steps` positions heading north, about 300 m apart."""
    at = start
    for i in range(steps):
        ft.record(owner, icao, lat0 + i * 0.003, -97.2, on_ground, at)
        at += timedelta(seconds=every_s)
    return at


def _points(owner="u1", icao="abc123", now=None):
    trail = ft.get(owner, icao, now or T0 + timedelta(minutes=1))
    return trail["points"] if trail else []


# ── the store ────────────────────────────────────────────────────────────────

def test_a_flight_is_kept_from_its_first_position():
    _fly("u1", "abc123", T0, 30)
    pts = _points(now=T0 + timedelta(minutes=6))
    assert len(pts) == 30
    assert pts[0][:2] == [33.0, -97.2] and pts[-1][0] > pts[0][0]
    assert [p[2] for p in pts] == sorted(p[2] for p in pts)
    assert ft.get("u1", "abc123", T0 + timedelta(minutes=6))["started_at"] == T0


def test_nothing_for_an_aircraft_never_heard_or_someone_elses():
    _fly("u1", "abc123", T0, 5)
    assert ft.get("u1", "ffffff", T0) is None
    assert ft.get("u2", "abc123", T0) is None


def test_jitter_and_bursts_do_not_fill_the_trail():
    ft.record("u1", "abc123", 33.0, -97.2, False, T0)
    ft.record("u1", "abc123", 33.00001, -97.2, False, T0 + timedelta(seconds=30))   # has not moved
    ft.record("u1", "abc123", 33.01, -97.2, False, T0 + timedelta(seconds=40))
    ft.record("u1", "abc123", 33.011, -97.2, False, T0 + timedelta(seconds=41))     # one second after the last kept
    ft.record("u1", "abc123", 33.02, -97.2, False, T0 + timedelta(seconds=50))
    assert [p[0] for p in _points()] == [33.0, 33.01, 33.02]


def test_an_older_position_never_goes_after_a_newer_one():
    """The receiver's fix is the newest; the network's older one for the same
    moment is dropped rather than drawn as a step backwards."""
    ft.record("u1", "abc123", 33.00, -97.2, False, T0)
    ft.record("u1", "abc123", 33.02, -97.2, False, T0 + timedelta(seconds=20))
    ft.record("u1", "abc123", 33.01, -97.2, False, T0 + timedelta(seconds=10))
    assert [p[0] for p in _points()] == [33.0, 33.02]


def test_touch_and_goes_are_one_flight():
    at = _fly("u1", "abc123", T0, 10)
    at = _fly("u1", "abc123", at, 3, lat0=33.05, on_ground=True)      # rolling on the runway
    _fly("u1", "abc123", at, 10, lat0=33.08)
    pts = _points(now=at + timedelta(minutes=5))
    assert len(pts) == 23 and pts[0][0] == 33.0


def test_flying_again_after_a_stop_starts_a_new_trail_from_the_parking_spot():
    at = _fly("u1", "abc123", T0, 10)
    ft.record("u1", "abc123", 33.1, -97.2, True, at)                     # landed and parked
    again = at + ft.GROUND_ENDS_FLIGHT + timedelta(minutes=1)
    ft.record("u1", "abc123", 33.1, -97.2, True, again - timedelta(seconds=30))
    # still shows the flight it arrived on while it sits there
    assert len(_points(now=again)) == 11
    _fly("u1", "abc123", again, 4, lat0=33.11)
    pts = _points(now=again + timedelta(minutes=2))
    assert [p[0] for p in pts] == [33.1, 33.11, 33.113, 33.116, 33.119]
    assert ft.get("u1", "abc123", again + timedelta(minutes=2))["started_at"] == again


def test_a_long_silence_starts_a_new_trail():
    at = _fly("u1", "abc123", T0, 10)
    later = at + ft.SILENCE_ENDS_FLIGHT + timedelta(minutes=1)
    _fly("u1", "abc123", later, 3, lat0=34.0)
    assert [p[0] for p in _points(now=later + timedelta(minutes=1))] == [34.0, 34.003, 34.006]


def test_a_trail_is_forgotten_once_the_aircraft_has_been_gone_a_while():
    at = _fly("u1", "abc123", T0, 10)
    assert _points(now=at + ft.FORGET_AFTER - timedelta(minutes=1))
    assert ft.get("u1", "abc123", at + ft.FORGET_AFTER + timedelta(minutes=1)) is None


def test_a_long_flight_keeps_its_whole_shape_within_the_cap():
    _fly("u1", "abc123", T0, ft.MAX_POINTS * 3, every_s=6, lat0=10.0)
    pts = _points(now=T0 + timedelta(hours=8))
    assert len(pts) <= ft.MAX_POINTS
    assert pts[0][0] == 10.0                                   # still starts where the flight did
    assert pts[-1][0] == round(10.0 + (ft.MAX_POINTS * 3 - 1) * 0.003, 5)


def test_bad_positions_are_ignored():
    for lat, lon in ((None, -97.2), ("33", -97.2), (True, -97.2), (91, 0), (0, 181), (float("nan"), 0)):
        ft.record("u1", "abc123", lat, lon, False, T0)
    assert ft.get("u1", "abc123", T0) is None


def test_one_account_cannot_fill_memory_with_made_up_aircraft():
    for i in range(ft.MAX_AIRCRAFT_PER_OWNER + 20):
        ft.record("u1", f"{i:06x}", 33.0, -97.2, False, T0)
    assert len(ft._trails["u1"]) == ft.MAX_AIRCRAFT_PER_OWNER


# ── across a restart ─────────────────────────────────────────────────────────

def _restart(now):
    """Save, lose everything in memory, read it back."""
    ft.save(api._shared, now)
    ft.forget()
    return ft.load(api._shared, now)


def test_a_flight_in_progress_survives_a_restart():
    at = _fly("u1", "abc123", T0, 20)
    before = ft.get("u1", "abc123", at)
    assert _restart(at) == 1
    assert ft.get("u1", "abc123", at) == before
    # and carries on as the same flight
    _fly("u1", "abc123", at + timedelta(seconds=30), 5, lat0=33.2)
    pts = _points(now=at + timedelta(minutes=2))
    assert len(pts) == 25 and pts[0][:2] == [33.0, -97.2]
    assert ft.get("u1", "abc123", at + timedelta(minutes=2))["started_at"] == T0


def test_a_parked_aircraft_still_starts_a_new_trail_after_a_restart():
    at = _fly("u1", "abc123", T0, 10)
    ft.record("u1", "abc123", 33.1, -97.2, True, at)
    assert _restart(at) == 1
    again = at + ft.GROUND_ENDS_FLIGHT + timedelta(minutes=1)
    _fly("u1", "abc123", again, 3, lat0=33.11)
    assert [p[0] for p in _points(now=again + timedelta(minutes=1))] == [33.1, 33.11, 33.113, 33.116]


def test_only_changed_trails_are_written_and_old_ones_are_deleted():
    from models import FlightTrailRow
    at = _fly("u1", "abc123", T0, 5)
    _fly("u1", "def456", T0, 5)
    assert ft.save(api._shared, at) == 2
    assert ft.save(api._shared, at) == 0
    ft.record("u1", "abc123", 33.5, -97.2, False, at + timedelta(seconds=30))
    assert ft.save(api._shared, at + timedelta(minutes=1)) == 1
    assert api._shared.query(FlightTrailRow).count() == 2
    # def456 goes quiet: its saved copy is dropped, and a restart does not bring it back
    late = at + ft.FORGET_AFTER + timedelta(seconds=10)
    ft.record("u1", "abc123", 33.6, -97.2, False, late)
    ft.save(api._shared, late)
    assert [r.icao24 for r in api._shared.query(FlightTrailRow).all()] == ["abc123"]
    ft.forget()
    assert ft.load(api._shared, late) == 1


def test_a_trail_already_in_memory_is_not_replaced_by_the_saved_one():
    at = _fly("u1", "abc123", T0, 5)
    ft.save(api._shared, at)
    ft.forget()
    ft.record("u1", "abc123", 40.0, -97.2, False, at + timedelta(seconds=5))
    assert ft.load(api._shared, at + timedelta(seconds=10)) == 0
    assert [p[0] for p in _points(now=at + timedelta(minutes=1))] == [40.0]


# ── through the tracker and the API ──────────────────────────────────────────

def test_the_tracker_records_the_network_position_each_cycle(monkeypatch):
    both._setup(monkeypatch)
    for nm in (20, 18, 16):
        both.adsb_at(nm)
        both.cycle()
        # the next cycle's fix is 30 s newer in real life
        for tr in ft._trails[UID].values():
            tr["last_at"] -= timedelta(seconds=30)
            tr["points"] = [(t - 30, la, lo) for t, la, lo in tr["points"]]
    body = client.get(f"/api/aircraft/trail?tail={TAIL}").json()
    assert body["tail"] == TAIL and body["started_at"]
    assert [round((p[0] - both.LAT0) * 60) for p in body["points"]] == [20, 18, 16]


def test_the_receivers_positions_are_recorded_as_they_arrive(monkeypatch):
    both._setup(monkeypatch)
    now = datetime.utcnow()
    for i, nm in enumerate((3.0, 2.5, 2.0)):
        res = client.post("/api/ground/positions", json={"positions": {
            ICAO: {"lat": both._north(nm), "lon": both.LON0, "altitude": 1500, "on_ground": False,
                   "updated_at": (now - timedelta(seconds=60 - i * 10)).isoformat()},
            "ffffff": {"lat": 10.0, "lon": 10.0, "updated_at": now.isoformat()},     # not one of the account's
        }})
        assert res.status_code == 200
    body = client.get(f"/api/aircraft/trail?tail={TAIL.lower()}").json()
    assert [round((p[0] - both.LAT0) * 60, 1) for p in body["points"]] == [3.0, 2.5, 2.0]
    assert "ffffff" not in ft._trails[UID]


def test_an_aircraft_with_no_trail_answers_empty_and_an_unknown_one_404(monkeypatch):
    both._setup(monkeypatch)
    assert client.get(f"/api/aircraft/trail?tail={TAIL}").json() == {"tail": TAIL, "started_at": None, "points": []}
    assert client.get("/api/aircraft/trail?tail=N00000").status_code == 404
