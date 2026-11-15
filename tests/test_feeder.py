"""The free plan for feeding adsb.lol: a receiver that was feeding on 5 of the
last 7 days has the plan, a new feeder has it from its first heartbeat, and a
paid plan is never touched.

The rule itself is checked on plain data; the rest uses the app and throwaway
database set up in test_api_integration (whose fixture gives the test account
the paid plan, so most tests here drop it first).
"""
import asyncio
from datetime import date, datetime, timedelta

import jwt

import feeder
import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

main = api.main
client = api.client
db = api._shared
USER_ID = api.USER_ID

DAY = date(2026, 11, 20)
FULL = feeder.FEED_MINUTES_PER_DAY


def _iso(d):
    return d.isoformat()


def _record(since, days):
    """A stored record: `days` maps a day to its feeding minutes."""
    return {"since": _iso(since), "days": {_iso(d): m for d, m in days.items()}, "last": None}


def _user():
    db.expire_all()
    return db.get(api.User, USER_ID)


def _feeding_heartbeat(feeding=True):
    return client.post("/api/ground/heartbeat", json={"version": "3.9", "uptime_s": 60, "feeding": feeding})


# ── the rule ─────────────────────────────────────────────────────────────────

def test_a_new_feeder_has_the_plan_from_its_first_minute():
    feed = feeder.record_feed_minute(None, datetime(2026, 11, 20, 14, 0))
    assert feed["since"] == "2026-11-20"
    assert feeder.qualifies(feed, DAY)
    # ...and keeps it for two more days if the receiver is then unplugged
    assert feeder.valid_until(feed, DAY) == DAY + timedelta(days=2)
    assert not feeder.qualifies(feed, DAY + timedelta(days=3))


def test_a_steady_feeder_keeps_the_plan_two_days_past_its_last_feed():
    days = {DAY - timedelta(days=n): 1440 for n in range(10)}
    feed = _record(DAY - timedelta(days=40), days)
    assert feeder.days_counted(feed, DAY) == 7
    assert feeder.valid_until(feed, DAY) == DAY + timedelta(days=2)


def test_five_days_in_seven_is_enough_and_four_is_not():
    since = DAY - timedelta(days=40)
    five = _record(since, {DAY - timedelta(days=n): FULL for n in (0, 1, 3, 4, 6)})
    assert feeder.qualifies(five, DAY)
    four = _record(since, {DAY - timedelta(days=n): FULL for n in (0, 1, 3, 4)})
    assert not feeder.qualifies(four, DAY)
    assert feeder.valid_until(four, DAY) is None


def test_a_day_needs_its_hours_except_the_very_first():
    since = DAY - timedelta(days=40)
    short = _record(since, {DAY - timedelta(days=n): FULL - 1 for n in range(7)})
    assert feeder.fed_days(short) == set()
    assert not feeder.qualifies(short, DAY)
    first = _record(DAY, {DAY: 1})
    assert feeder.fed_days(first) == {DAY}


def test_the_starting_credit_is_given_once():
    """Someone who fed for a day long ago and comes back has to feed five days again."""
    back = _record(DAY - timedelta(days=60), {DAY: FULL})
    assert feeder.days_counted(back, DAY) == 1
    assert not feeder.qualifies(back, DAY)
    back["days"].update({_iso(DAY - timedelta(days=n)): FULL for n in range(1, 5)})
    assert feeder.qualifies(back, DAY)


def test_heartbeats_close_together_count_once_and_old_days_are_dropped():
    t = datetime(2026, 11, 20, 9, 0, 0)
    feed = feeder.record_feed_minute(None, t)
    feed = feeder.record_feed_minute(feed, t + timedelta(seconds=10))
    assert feed["days"]["2026-11-20"] == 1
    feed = feeder.record_feed_minute(feed, t + timedelta(seconds=61))
    assert feed["days"]["2026-11-20"] == 2

    feed["days"]["2026-10-01"] = 900
    feed = feeder.record_feed_minute(feed, t + timedelta(minutes=5))
    assert "2026-10-01" not in feed["days"]


def test_a_damaged_record_is_read_as_empty():
    for junk in (None, [], "x", {"days": "nope"}, {"since": "soon", "days": {"bad": 5, "2026-11-20": -3}}):
        assert feeder.days_counted(junk, DAY) == 0
        assert feeder.valid_until(junk, DAY) is None


# ── the plan it gives ────────────────────────────────────────────────────────

def test_feeding_without_a_plan_gives_the_plan(monkeypatch):
    api._drop_plan()
    started = []

    async def note(user_id, _db):
        started.append(user_id)

    monkeypatch.setattr(main.tracker, "update_user_aircraft", note)

    assert _feeding_heartbeat().status_code == 200
    user = _user()
    lic = db.get(api.main.License, user.license_id)
    assert feeder.is_feeder_license(lic) and lic.tier == "starter"
    assert main.plan_tier(db, user) == "starter"
    assert started == [str(USER_ID)]          # tracking starts at once

    status = client.get("/api/ground/status").json()["feeder"]
    assert status["plan"] == "feeder" and status["qualifies"] is True
    assert status["days_needed"] == 5 and status["window_days"] == 7

    # a second heartbeat changes nothing and does not reload the tracker
    assert _feeding_heartbeat().status_code == 200
    assert started == [str(USER_ID)]


def test_user_me_reports_the_feeder_plan_without_an_end_date(monkeypatch):
    api._drop_plan()

    async def website_says_free(_email):
        return {"tier": "free", "status": "none", "expires_at": None, "license_key": None}

    monkeypatch.setattr(main, "fetch_entitlement", website_says_free)
    _feeding_heartbeat()
    me = client.get("/api/user/me").json()
    # the website knowing of no paid plan must not take the feeder plan away
    assert me["license_tier"] == "starter"
    assert me["plan_source"] == "feeder"
    assert me["expires_at"] is None


def test_a_receiver_that_is_not_feeding_earns_nothing():
    api._drop_plan()
    for body in ({"version": "3.9", "feeding": False}, {"version": "3.8"}, {"version": "3.9", "feeding": None},
                 {"version": "3.9", "feeding": "yes"}):
        assert client.post("/api/ground/heartbeat", json=body).status_code == 200
    assert _user().license_id is None
    assert client.get("/api/ground/status").json()["feeder"]["plan"] == "none"


def test_a_paid_plan_is_left_alone_and_the_minutes_still_count():
    paid = _user().license_id
    assert _feeding_heartbeat().status_code == 200
    user = _user()
    assert user.license_id == paid
    assert client.get("/api/ground/status").json()["feeder"]["plan"] == "paid"
    # so cancelling the paid plan drops straight onto the free one
    api._drop_plan()
    assert feeder.sync_feeder_license(db, _user(), _user().gs_feed) is True
    assert feeder.is_feeder_license(db.get(main.License, _user().license_id))


def test_the_plan_ends_when_the_feeding_does(monkeypatch):
    api._drop_plan()
    _feeding_heartbeat()
    user = _user()
    # as if that first heartbeat was ten days ago and nothing since
    long_ago = datetime.utcnow() - timedelta(days=10)
    user.gs_feed = {"since": _iso(long_ago.date()), "days": {_iso(long_ago.date()): 30}, "last": long_ago.isoformat()}
    db.get(main.License, user.license_id).expires_at = long_ago + timedelta(days=3)
    db.commit()

    assert main.plan_tier(db, _user()) == "free"

    async def no_answer(_email):
        return None

    monkeypatch.setattr(main, "fetch_entitlement", no_answer)
    me = client.get("/api/user/me").json()
    assert me["license_tier"] == "free" and me["plan_source"] is None
    assert _user().license_id is None


def test_a_lapsed_feeder_plan_is_no_plan_not_an_expired_one():
    """An expired paid licence signs the account out as "license_expired". A
    feeder whose receiver went quiet just drops to the preview."""
    api._drop_plan()
    _feeding_heartbeat()
    user = _user()
    db.get(main.License, user.license_id).expires_at = datetime.utcnow() - timedelta(days=30)
    db.commit()
    token = jwt.encode({"sub": str(USER_ID), "exp": datetime.utcnow() + timedelta(hours=1)},
                       main.SECRET_KEY, algorithm=main.ALGORITHM)
    assert main._authenticate_jwt(token, db).id == USER_ID


def test_the_feeder_plan_gets_no_grace_period():
    api._drop_plan()
    _feeding_heartbeat()
    lic = db.get(main.License, _user().license_id)
    lic.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    assert main.live_license(db, _user()) is None


# ── a receiver on an account without a plan ──────────────────────────────────

def test_a_receiver_can_sign_in_without_a_plan():
    api._drop_plan()
    user = _user()
    user.ground_station_enabled = False
    user.gs_device_key = "a" * 64
    db.commit()
    assert main._authenticate_device_key("a" * 64, db).id == USER_ID
    assert client.post("/api/ground/validate").json() == {
        "enabled": True, "plan": False, "email": "pilot@example.com", "message": "Ground station access confirmed",
    }


def test_without_a_plan_the_receiver_is_given_no_aircraft_and_its_positions_are_dropped():
    client.post("/api/airport/config", json=api.AIRPORT)
    client.post("/api/aircraft", json={"tail_number": "N123FP", "icao24": "a1b2c3"})
    assert [a["tail"] for a in client.get("/api/ground/config").json()["aircraft"]] == ["N123FP"]

    api._drop_plan()
    user = _user()
    user.ground_station_enabled = False
    db.commit()
    assert client.get("/api/ground/config").json()["aircraft"] == []

    main._gs.ground_positions.pop(str(USER_ID), None)
    res = client.post("/api/ground/positions", json={"positions": {"a1b2c3": {"lat": 33.2, "lon": -97.2}}})
    assert res.status_code == 200
    assert str(USER_ID) not in main._gs.ground_positions
    # range and status still answer, so the page can show the receiver
    assert client.post("/api/ground/range", json={"range_nm": [1.0] * 36}).status_code == 200
    assert client.get("/api/ground/range").status_code == 200
    assert client.get("/api/ground/status").status_code == 200
