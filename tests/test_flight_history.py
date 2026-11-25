"""Recorded flights for replay (flight_history.py) and their endpoints."""
import asyncio
from datetime import datetime, timedelta

import flight_history as fh
import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

client = api.client
db = api._shared
UID = str(api.USER_ID)
ICAO = "abc123"
T0 = datetime(2026, 11, 24, 15, 0, 0)


def _reset():
    fh._open.clear()
    fh._closed.clear()
    fh._last_prune = None


def at(s):
    return T0 + timedelta(seconds=s)


def fly(s, nm=5.0, alt=2500, gs=110, ground=False, src="adsb.lol", icao=ICAO):
    fh.record(UID, icao, "N172AK", at(s), 33.2 + nm / 60, -97.2, alt, gs, 180, ground, src)


def _save(now_s):
    fh.save(db, now=at(now_s))


def _rows():
    from models import FlightHistory
    db.expire_all()
    return db.query(FlightHistory).order_by(FlightHistory.started_at).all()


# ── recording ────────────────────────────────────────────────────────────────

def test_a_flight_is_recorded_with_altitude_speed_and_track():
    _reset()
    for i, nm in enumerate((10, 8, 6, 4, 2)):
        fly(i * 30, nm, alt=3000 - i * 400)
    _save(150)
    (row,) = _rows()
    assert row.n_points == 5 and not row.ended and row.tail == "N172AK"
    assert row.points[0][3:8] == [3000, 110, 180, 0, "a"]
    assert row.started_at == at(0)


def test_parked_aircraft_start_nothing_and_taxi_only_is_not_kept():
    _reset()
    for s in range(0, 600, 30):
        fly(s, 0.1, alt=None, gs=0, ground=True)          # parked
    assert fh._open == {}
    fly(700, 0.1, alt=None, gs=12, ground=True)            # taxis out...
    fly(760, 0.1, alt=None, gs=0, ground=True)             # ...and back, never flies
    fly(1500, 0.1, alt=None, gs=0, ground=True)            # 10+ min on the ground ends it
    _save(1500)
    assert _rows() == []


def test_ten_minutes_on_the_ground_ends_a_flight_and_a_new_one_starts():
    _reset()
    fly(0, 5); fly(30, 1)
    fly(60, 0.2, alt=None, gs=20, ground=True)             # landed, taxiing
    fly(700, 0.1, alt=None, gs=0, ground=True)             # parked 10+ min
    fly(1000, 0.3, alt=None, gs=15, ground=True)           # taxis out again
    fly(1100, 2, alt=1500, gs=95)                          # takes off: flight two
    _save(1100)
    rows = _rows()
    assert len(rows) == 2 and rows[0].ended and not rows[1].ended


def test_touch_and_goes_stay_one_flight():
    _reset()
    s = 0
    for lap in range(3):
        fly(s, 2); s += 30
        fly(s, 0.1, alt=None, gs=60, ground=True); s += 30  # touchdown, rolling
        fly(s, 1, alt=1200, gs=80); s += 30
    _save(s)
    assert len(_rows()) == 1


def test_receiver_keeps_one_fix_a_second_and_late_network_fixes_slot_in():
    _reset()
    for s in range(0, 10):
        fly(s, 1.0 - s * 0.01, src="receiver")
    fly(4.5, 0.95, src="adsb.lol")                          # within 1 s of receiver fixes: dropped
    fly(30, 0.5, src="receiver")
    fly(20, 0.7, src="FlyItalyADSB")                        # arrives late, goes in order
    _save(30)
    (row,) = _rows()
    times = [p[0] for p in row.points]
    assert times == sorted(times) and len(times) == 12
    assert [p[7] for p in row.points].count("f") == 1


def test_thirty_minutes_unheard_ends_it_and_old_flights_are_dropped():
    _reset()
    fly(0, 5); fly(30, 3)
    _save(30 + 31 * 60)                                     # quiet 31 min: closed
    (row,) = _rows()
    assert row.ended
    fly(4 * 3600, 6)                                        # a later flight
    _save(4 * 3600)
    assert len(_rows()) == 2
    fh._last_prune = None
    _save(49 * 3600)                                        # the first is 49 h old now
    rows = _rows()
    assert len(rows) == 1 and rows[0].started_at == at(4 * 3600)


def test_open_flights_survive_a_restart():
    _reset()
    fly(0, 5); fly(30, 4)
    _save(30)
    _reset()
    assert fh.load(db, now=at(60)) == 1
    fly(60, 3)
    _save(60)
    (row,) = _rows()
    assert row.n_points == 3


# ── filling in from FlyItalyADSB ─────────────────────────────────────────────

def test_history_fills_between_polls_inside_the_flight_only():
    base = fh._epoch(at(0))
    ours = [[base, 33.3, -97.2, 3000, 110, 180, 0, "a"], [base + 60, 33.25, -97.2, 2600, 110, 180, 0, "a"]]
    trace = {"timestamp": base - 1000, "trace": [
        [1000 + o, 33.29, -97.2, 2900, 108.0, 181.0, 0, 64] for o in (0.5, 5, 10, 55, 59)
    ] + [[0, 34.0, -97.0, 5000, 120.0, 10.0, 0, 0],                 # long before: left out
         [1030, 33.27, -97.2, "ground", 0, 0, 0, 0]]}
    out = fh.fill_from_history(ours, [trace])
    srcs = [p[7] for p in out]
    assert srcs.count("h") == 4                                     # 5, 10, 30(ground), 55
    assert [p[0] for p in out] == sorted(p[0] for p in out)
    g = next(p for p in out if p[6] == 1)
    assert g[3] is None and g[7] == "h"


# ── endpoints ────────────────────────────────────────────────────────────────

def test_list_and_replay_a_flight_with_its_alerts(monkeypatch):
    _reset()
    db.add(api.Aircraft(user_id=api.USER_ID, tail_number="N172AK", icao24=ICAO))
    db.commit()
    now = datetime.utcnow()
    T = now - timedelta(minutes=20)
    for i, nm in enumerate((10, 8, 6, 4)):
        fh.record(UID, ICAO, "N172AK", T + timedelta(seconds=30 * i), 33.2 + nm / 60, -97.2, 2500, 110, 180, False)
    fh.save(db, now=now)
    for ch in ("discord", "email"):                                 # one alert, two channels
        db.add(api.NotificationLog(user_id=api.USER_ID, aircraft_tail="N172AK", alert_type="10nm", message="x",
                                   integration_type=ch, status="sent", sent_at=T + timedelta(seconds=10)))
    db.commit()

    async def no_history(icao, day):
        return None
    monkeypatch.setattr(api.main, "_flyitaly_trace", no_history)

    listed = client.get("/api/aircraft/flights?tail=N172AK").json()
    assert listed["keep_hours"] == 48 and len(listed["flights"]) == 1
    f = listed["flights"][0]
    assert f["in_progress"] and f["minutes"] == 2 and f["max_alt_ft"] == 2500

    one = client.get(f"/api/aircraft/flights/{f['id']}").json()
    assert len(one["points"]) == 4 and one["alerts"] == [{"type": "10nm", "at": one["alerts"][0]["at"]}]
    assert one["filled_from_history"] is False


def test_another_accounts_flight_is_not_found():
    _reset()
    from models import FlightHistory
    db.add(FlightHistory(owner=str(api.OTHER_ID), icao24=ICAO, tail="N1", started_at=T0, last_at=T0,
                         ended=True, n_points=1, points=[[0, 1, 2, None, None, None, 0, "a"]]))
    db.commit()
    fid = db.query(FlightHistory).one().id
    assert client.get(f"/api/aircraft/flights/{fid}").status_code == 404
    assert client.get("/api/aircraft/flights?tail=NOPE").status_code == 404
