"""One paid plan: 15 aircraft and nothing else capped, Ground Station included,
a monthly allowance of texts, and nothing at all for an account without a plan.

Uses the app and throwaway database set up in test_api_integration (whose
fixture gives the test account the paid plan).
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import test_api_integration as api
import plans
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

main = api.main
client = api.client
db = api._shared
USER_ID = api.USER_ID


def _add_aircraft(n):
    return client.post("/api/aircraft", json={"tail_number": f"N{100 + n}FP", "icao24": f"a{n:05x}"})


def _user():
    return db.get(api.User, USER_ID)


# ── limits ───────────────────────────────────────────────────────────────────

def test_fifteen_aircraft_and_no_more():
    for n in range(15):
        assert _add_aircraft(n).status_code == 200, n
    res = _add_aircraft(15)
    assert res.status_code == 403
    assert "15 aircraft" in res.json()["detail"]


def test_every_old_plan_name_has_the_same_limits():
    for tier in ("starter", "premium", "pro", "starter-yearly"):
        assert main.get_tier_limit(tier, "aircraft") == 15
        for feature in ("zones", "locations", "integrations"):
            assert main.get_tier_limit(tier, feature) is None


def test_alert_distances_are_not_capped():
    res = client.post("/api/airport/config", json=dict(api.AIRPORT, alert_distances_nm=[20, 15, 10, 7, 5, 3, 2, 1]))
    assert res.status_code == 200


def test_every_channel_can_be_connected_on_the_plan():
    for kind, config in (
        ("telegram", {"bot_token": "1:abc", "chat_id": "-100"}),
        ("sms", {"phone_number": "+15555550100"}),
        ("slack", {"webhook_url": "https://hooks.slack.com/services/x"}),
    ):
        assert client.post("/api/integrations", json={"type": kind, "config": config}).status_code == 200, kind


# ── no plan ──────────────────────────────────────────────────────────────────

def test_no_plan_cannot_add_aircraft():
    api._drop_plan()
    res = _add_aircraft(1)
    assert res.status_code == 402
    assert res.json()["detail"] == "no_plan"


def test_lapsed_plan_counts_as_no_plan():
    api._give_plan("starter", expires_at=datetime.utcnow() - timedelta(days=5))
    assert main.get_user_tier(_user(), db) == "free"
    # Inside the two-day grace period the plan still stands.
    api._give_plan("starter", expires_at=datetime.utcnow() - timedelta(hours=3))
    assert main.get_user_tier(_user(), db) == "starter"


def test_no_plan_is_not_tracked_and_gets_no_alerts(monkeypatch):
    client.post("/api/airport/config", json=api.AIRPORT)
    assert _add_aircraft(1).status_code == 200
    api._integration("discord")
    assert str(USER_ID) in main.tracker.user_trackers

    api._drop_plan()
    asyncio.run(main.tracker.update_user_aircraft(str(USER_ID), db))
    assert str(USER_ID) not in main.tracker.user_trackers

    sent = []

    async def fake_send(integration, message):
        sent.append(integration.type)
        return True

    monkeypatch.setattr(main.tracker, "send_via_integration", fake_send)
    note = {"type": "10nm", "tail": "N101FP", "distance": 10.2, "altitude": 2500, "eta": 6, "speed": 95, "time": datetime.now()}
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [note]))
    assert sent == []


def test_tracking_starts_and_stops_with_the_plan():
    client.post("/api/airport/config", json=api.AIRPORT)
    _add_aircraft(1)
    lic = main.live_license(db, _user())
    key = lic.license_key

    # The website says the subscription ended.
    asyncio.run(main.refresh_entitlement(db, _user(), {"tier": "free", "license_key": None}))
    assert main.get_user_tier(_user(), db) == "free"
    assert str(USER_ID) not in main.tracker.user_trackers

    # They subscribe again.
    asyncio.run(main.refresh_entitlement(db, _user(), {"tier": "starter", "license_key": key, "expires_at": None}))
    assert main.get_user_tier(_user(), db) == "starter"
    assert str(USER_ID) in main.tracker.user_trackers


def test_account_info_says_free_without_a_plan(monkeypatch):
    async def no_answer(email):
        return None

    monkeypatch.setattr(main, "fetch_entitlement", no_answer)
    assert client.get("/api/user/me").json()["license_tier"] == "starter"
    api._drop_plan()
    assert client.get("/api/user/me").json()["license_tier"] == "free"


# ── Ground Station is part of the plan ───────────────────────────────────────

def test_ground_station_needs_only_the_plan():
    user = _user()
    user.ground_station_enabled = False
    db.commit()
    assert main.gs_allowed(user, db) is True
    assert client.get("/api/ground/status").status_code == 200

    api._drop_plan()
    assert main.gs_allowed(_user(), db) is False
    assert client.get("/api/ground/status").status_code == 403


def test_hand_granted_ground_station_survives_without_a_plan():
    api._drop_plan()
    user = _user()
    user.ground_station_enabled = True
    db.commit()
    assert main.gs_allowed(user, db) is True


# ── texts ────────────────────────────────────────────────────────────────────

def _sms_logs(count, when):
    for _ in range(count):
        db.add(api.NotificationLog(id=uuid.uuid4(), user_id=USER_ID, aircraft_tail="N101FP", alert_type="10nm",
                                   message="x", integration_type="sms", status="sent", sent_at=when))
    db.commit()


def test_texts_stop_at_the_monthly_allowance(monkeypatch):
    api._integration("sms")
    api._integration("discord")
    _sms_logs(plans.SMS_MONTHLY_LIMIT - 1, datetime.utcnow())
    sent = []

    async def fake_send(integration, message):
        sent.append(integration.type)
        return True

    monkeypatch.setattr(main.tracker, "send_via_integration", fake_send)
    note = {"type": "10nm", "tail": "N101FP", "distance": 10.2, "altitude": 2500, "eta": 6, "speed": 95, "time": datetime.now()}
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [dict(note)]))
    assert sorted(sent) == ["discord", "sms"]          # the last text of the month goes out

    sent.clear()
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [dict(note)]))
    assert sent == ["discord"]                          # the next one does not, other channels do
    capped = db.query(api.NotificationLog).filter(api.NotificationLog.status == "limit").all()
    assert [c.integration_type for c in capped] == ["sms"]


def test_last_months_texts_do_not_count():
    last_month = datetime.utcnow().replace(day=1) - timedelta(days=3)
    _sms_logs(plans.SMS_MONTHLY_LIMIT + 5, last_month)
    assert main.sms_over_limit(db, USER_ID) is False
