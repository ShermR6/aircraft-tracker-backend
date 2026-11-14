"""The account's Ground Station and adsb.lol working together so an alert is
not missed: the receiver's positions are used for alerts when fresh, adsb.lol
covers the rest, rings re-arm properly, and nothing fires twice.

Drives the real tracker cycle (track_all_users) with a fake adsb.lol and the
throwaway database from test_api_integration.
"""
import asyncio
from datetime import datetime, timedelta

import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

import ground_state as _gs
import tracker as tracker_mod

main = api.main
client = api.client
db = api._shared
USER_ID = api.USER_ID
UID = str(USER_ID)
ICAO = "abc123"
TAIL = "N172AK"
LAT0, LON0 = 33.2007, -97.1980          # the test airport (KDTO)


# ── fakes ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """aiohttp.ClientSession stand-in answering with whatever ADSB holds."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, timeout=None):
        if ADSB["fail"]:
            return _Resp(503, {})
        return _Resp(200, {"ac": ADSB["ac"]})


ADSB = {"fail": False, "ac": []}


def _setup(monkeypatch, rings=(15, 10, 5, 2), corridor=None):
    payload = dict(api.AIRPORT, alert_distances_nm=list(rings))
    if corridor is not None:
        payload.update(approach_corridor_enabled=True, approach_runway_heading=corridor, approach_runway_headings=[corridor])
    assert client.post("/api/airport/config", json=payload).status_code == 200
    db.add(api.Aircraft(user_id=USER_ID, tail_number=TAIL, icao24=ICAO))
    db.commit()
    asyncio.run(main.tracker.update_user_aircraft(UID, db))

    ADSB.update(fail=False, ac=[])
    _gs.ground_positions.pop(UID, None)
    _gs.ground_positions_at.pop(UID, None)
    monkeypatch.setattr(tracker_mod.aiohttp, "ClientSession", _Session)

    sent = []

    async def fake_send(user_id, notifications):
        sent.extend(n["type"] for n in notifications)
        # Log like the real sender does, so repeat checks can see it.
        for n in notifications:
            db.add(api.NotificationLog(user_id=USER_ID, aircraft_tail=n["tail"], alert_type=n["type"],
                                       message="x", integration_type="discord", status="sent", sent_at=datetime.utcnow()))
        db.commit()

    monkeypatch.setattr(main.tracker, "send_notifications", fake_send)
    return sent


def _north(nm):
    return LAT0 + nm / 60.0


def adsb_at(nm, alt=2500, gs=110, track=180):
    """adsb.lol sees the aircraft nm north of the field, heading south."""
    ADSB["ac"] = [{"hex": ICAO, "lat": _north(nm), "lon": LON0, "alt_baro": alt, "gs": gs, "track": track, "seen_pos": 1}]


def adsb_gone():
    ADSB["ac"] = []


def ground_at(nm, speed=90, on_ground=False, heading=180, age_s=2):
    """The account's own receiver hears the aircraft nm north of the field."""
    now = datetime.utcnow()
    _gs.ground_positions[UID] = {ICAO: {
        "lat": _north(nm), "lon": LON0, "altitude": 642 if on_ground else 1500,
        "speed": speed, "heading": heading, "on_ground": on_ground,
        "updated_at": (now - timedelta(seconds=age_s)).isoformat(),
    }}
    _gs.ground_positions_at[UID] = now - timedelta(seconds=age_s)


def ground_gone():
    _gs.ground_positions.pop(UID, None)
    _gs.ground_positions_at.pop(UID, None)


def cycle():
    asyncio.run(main.tracker.track_all_users())


def minutes_pass(n=10):
    """Age the per-alert cooldown, as a real return flight takes minutes."""
    ut = main.tracker.user_trackers[UID]
    ut.last_notifications = {k: v - timedelta(minutes=n) for k, v in ut.last_notifications.items()}


# ── the receiver fills the gaps ──────────────────────────────────────────────

def test_rings_below_public_coverage_come_from_the_receiver(monkeypatch):
    sent = _setup(monkeypatch)
    for nm in (18, 14, 9):            # adsb.lol has it out here
        adsb_at(nm); cycle()
    assert sent == ["15nm", "10nm"]
    adsb_gone()                        # dropped below public coverage
    for nm in (4.5, 1.5):
        ground_at(nm); cycle()
    assert sent == ["15nm", "10nm", "5nm", "2nm"]
    ground_at(0.2, speed=20, on_ground=True); cycle()
    assert sent[-1] == "landing"


def test_receiver_is_preferred_when_both_have_the_aircraft(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(12); ground_at(12); cycle()
    adsb_at(12)                        # adsb.lol is behind
    ground_at(1.5); cycle()            # the receiver has it on short final
    assert sent == ["10nm", "2nm"]


def test_adsb_failure_does_not_stop_the_receiver_or_fake_a_landing(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(12); cycle()
    adsb_at(3); cycle()                # last public sight 3 nm out, airborne
    ADSB["fail"] = True
    ground_gone()
    cycle(); cycle(); cycle()          # adsb.lol down: no "vanished near the field" landing
    assert "landing" not in sent
    ground_at(1.0); cycle()            # the receiver still covers it
    assert sent == ["10nm", "2nm"]


def test_stale_receiver_positions_are_ignored(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(12); cycle()
    adsb_at(11.5)
    ground_at(1.0, age_s=45); cycle()  # receiver data too old: adsb.lol is used
    assert sent == []


# ── re-arming ────────────────────────────────────────────────────────────────

def test_a_ring_re_arms_once_the_aircraft_is_2_nm_outside_it(monkeypatch):
    sent = _setup(monkeypatch)
    for nm in (18, 14, 9, 4, 1.5):
        adsb_at(nm); cycle()
    assert sent == ["15nm", "10nm", "5nm", "2nm"]
    sent.clear()
    minutes_pass()
    for nm in (6, 12.5, 9, 4, 1.5):     # local flight out to 12.5 nm and back
        adsb_at(nm); cycle()
    assert sent == ["10nm", "5nm", "2nm"]   # 15 nm not re-armed: never beyond 17


def test_pattern_work_does_not_repeat_the_2_nm_alert(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    for nm in (12, 9, 1.5):
        adsb_at(nm); cycle()
    sent.clear()
    minutes_pass()
    for nm in (2.5, 3.5, 2.5, 1.5, 3.5, 1.0):   # circuits stay inside 4 nm
        adsb_at(nm); cycle()
    assert sent == []


def test_a_long_absence_re_arms_every_ring(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    for nm in (12, 9):
        adsb_at(nm); cycle()
    adsb_gone()
    state = main.tracker.user_trackers[UID].aircraft_state[ICAO]
    state["last_update"] = datetime.now() - timedelta(minutes=45)
    state["last_distance"] = 12.0               # last seen outside, back after a long time
    minutes_pass(45)
    sent.clear()
    adsb_at(9); cycle()
    assert sent == ["10nm"]


# ── approach filter holds instead of dropping ────────────────────────────────

def test_approach_filter_fires_once_the_aircraft_lines_up(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2), corridor=180)
    adsb_at(12, track=90); cycle()
    adsb_at(9.5, track=90); cycle()    # crossed 10 nm on a base leg: held
    assert sent == []
    adsb_at(8, track=178); cycle()     # turned final, still inside the ring
    assert sent == ["10nm"]


# ── state survives a settings save ───────────────────────────────────────────

def test_saving_settings_mid_approach_keeps_the_alert(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(12); cycle()
    asyncio.run(main.tracker.update_user_aircraft(UID, db))   # e.g. airport settings saved
    adsb_at(9); cycle()
    assert sent == ["10nm"]
    asyncio.run(main.tracker.update_user_aircraft(UID, db))
    adsb_at(8); cycle()
    assert sent == ["10nm"]                                     # and no second one


# ── landings: no early ones, no repeats ──────────────────────────────────────

def later(minutes):
    """Move time on: the in-memory cooldown and the alert log both age."""
    minutes_pass(minutes)
    for row in db.query(api.NotificationLog).all():
        row.sent_at = row.sent_at - timedelta(minutes=minutes)
    db.commit()


def lap(receiver_too=True):
    """One stop-and-go or touch-and-go as adsb.lol reports it (the transponder
    says "ground" on the runway), with the receiver hearing it as well."""
    adsb_at(0.4, alt="ground", gs=45)
    if receiver_too:
        ground_at(0.4, speed=55)          # the receiver alone would call this airborne
    cycle()
    adsb_at(0.8, alt=925, gs=80)
    if receiver_too:
        ground_at(0.8, speed=80)
    cycle()


def test_pattern_laps_report_every_landing_and_takeoff(monkeypatch):
    """Like the log of 14 Nov: laps 4 min 43 s apart, each one reported."""
    sent = _setup(monkeypatch, rings=(15, 10, 5, 2))
    adsb_at(1.5, alt=1500, gs=75); ground_at(1.5, speed=75); cycle()
    for _ in range(3):
        lap()
        later(4)
    assert sent.count("landing") == 3
    assert sent.count("takeoff") == 3


def test_laps_still_count_when_only_adsb_sees_them(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(1.5, alt=1500, gs=75); cycle()
    for _ in range(2):
        lap(receiver_too=False)
        later(4)
    assert sent.count("landing") == 2 and sent.count("takeoff") == 2


def test_receiver_decides_landing_when_adsb_has_lost_the_aircraft(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    adsb_at(3, alt=1200, gs=70); cycle()
    adsb_gone()                                         # below public coverage
    ground_at(0.3, speed=40, on_ground=True); cycle()   # receiver: on the runway
    assert sent.count("landing") == 1


def test_a_landing_already_logged_is_not_sent_again(monkeypatch):
    sent = _setup(monkeypatch, rings=(10, 2))
    db.add(api.NotificationLog(user_id=USER_ID, aircraft_tail=TAIL, alert_type="landing", message="x",
                               integration_type="discord", status="sent", sent_at=datetime.utcnow()))
    db.commit()
    ground_at(2, speed=70); cycle()
    ground_at(0.1, speed=15, on_ground=True); cycle()
    assert "landing" not in sent
