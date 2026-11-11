"""End-to-end checks of the web-app additions against a throwaway SQLite
database: the real FastAPI app, the real ORM models and the real tracker.

Skipped unless the backend's libraries are installed (they are not part of the
plain local Python). Run with a virtualenv that has fastapi + sqlalchemy:

    python -m pytest tests -q
"""

import asyncio
import os
import sys
import types
import uuid
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("WEBHOOK_INTERNAL_SECRET", "test-internal")
os.environ["SENTRY_DSN"] = ""

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import declarative_base, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy.sql import sqltypes  # noqa: E402

# PostgreSQL accepts a string where a UUID is expected; SQLite's emulation does
# not. The app passes strings (path parameters, tracker keys), so teach the
# test database to accept them too.
_orig_bind = sqltypes.Uuid.bind_processor


def _lenient_bind(self, dialect):
    proc = _orig_bind(self, dialect)
    if proc is None:
        return None

    def process(value):
        if isinstance(value, str):
            value = uuid.UUID(value)
        return proc(value)

    return process


sqltypes.Uuid.bind_processor = _lenient_bind

# Stand in for database.py, which is hard-wired to PostgreSQL connection options.
_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
_shared = _SessionLocal()


def _get_db():
    yield _shared


_db_mod = types.ModuleType("database")
_db_mod.engine = _engine
_db_mod.SessionLocal = _SessionLocal
_db_mod.Base = declarative_base()
_db_mod.get_db = _get_db
sys.modules["database"] = _db_mod

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models import Aircraft, AirportConfig, Integration, NotificationLog, User  # noqa: E402

USER_ID = uuid.uuid4()
OTHER_ID = uuid.uuid4()


def _current_user():
    return _shared.get(User, USER_ID)


main.app.dependency_overrides[main.get_current_user] = _current_user
main.app.dependency_overrides[main.get_ground_user] = _current_user
client = TestClient(main.app)   # no context manager: startup (tracker loop) never runs


@pytest.fixture(autouse=True)
def _fresh_db():
    _shared.rollback()
    _db_mod.Base.metadata.drop_all(_engine)
    _db_mod.Base.metadata.create_all(_engine)
    _shared.expunge_all()
    _shared.add(User(id=USER_ID, email="pilot@example.com", ground_station_enabled=True))
    _shared.add(User(id=OTHER_ID, email="other@example.com"))
    _shared.commit()
    main.tracker.user_trackers.clear()
    yield


def _integration(kind, owner=USER_ID):
    row = Integration(id=uuid.uuid4(), user_id=owner, type=kind, config={"webhook_url": "https://example.com/x"}, enabled=True)
    _shared.add(row)
    _shared.commit()
    return str(row.id)


AIRPORT = {
    "airport_code": "KDTO", "airport_name": "Denton Enterprise",
    "latitude": 33.2007, "longitude": -97.1980, "elevation_ft_msl": 642,
    "alert_distances_nm": [10, 2],
}


# ── routing ──────────────────────────────────────────────────────────────────

def test_routing_defaults_to_empty():
    assert client.get("/api/settings/routing").json() == {"routing": {}}


def test_routing_round_trip_drops_foreign_ids_and_bad_keys():
    mine = _integration("discord")
    theirs = _integration("discord", owner=OTHER_ID)
    r = client.put("/api/settings/routing", json={"routing": {
        "10nm": [mine, theirs], "landing": [theirs], "bogus": [mine],
    }})
    assert r.status_code == 200
    assert r.json() == {"routing": {"10nm": [mine]}}
    assert client.get("/api/settings/routing").json() == {"routing": {"10nm": [mine]}}

    assert client.put("/api/settings/routing", json={"routing": {}}).json() == {"routing": {}}
    assert client.get("/api/settings/routing").json() == {"routing": {}}


def test_deleting_an_integration_removes_it_from_routing():
    a, b = _integration("discord"), _integration("email")
    client.put("/api/settings/routing", json={"routing": {"10nm": [a, b], "landing": [a]}})
    assert client.delete(f"/api/integrations/{a}").status_code == 200
    assert client.get("/api/settings/routing").json() == {"routing": {"10nm": [b]}}


def test_integrations_still_list_and_delete_without_routing():
    a = _integration("discord")
    assert len(client.get("/api/integrations").json()) == 1
    assert client.delete(f"/api/integrations/{a}").status_code == 200
    assert client.get("/api/integrations").json() == []


# ── receiver stats ───────────────────────────────────────────────────────────

def test_heartbeat_from_an_old_agent_still_works_and_reports_no_stats():
    assert client.post("/api/ground/heartbeat", json={}).json() == {"ok": True}
    status = client.get("/api/ground/status").json()
    assert status["online"] is True
    assert status["stats"] is None


def test_heartbeat_with_no_body_at_all_still_works():
    assert client.post("/api/ground/heartbeat").json() == {"ok": True}
    assert client.get("/api/ground/status").json()["online"] is True


def test_heartbeat_stats_are_stored_and_survive_an_empty_heartbeat():
    body = {"version": "3.7", "uptime_s": 120, "msg_rate": 84.26, "aircraft_count": 12, "junk": "x"}
    assert client.post("/api/ground/heartbeat", json=body).status_code == 200
    expected = {"version": "3.7", "uptime_s": 120, "msg_rate": 84.3, "aircraft_count": 12}
    assert client.get("/api/ground/status").json()["stats"] == expected
    client.post("/api/ground/heartbeat", json={})
    assert client.get("/api/ground/status").json()["stats"] == expected


# ── airport config / runway headings ─────────────────────────────────────────

def test_desktop_style_config_never_sets_a_heading_list():
    payload = dict(AIRPORT, approach_corridor_enabled=True, approach_runway_heading=180)
    assert client.post("/api/airport/config", json=payload).status_code == 200
    cfg = client.get("/api/airport/config").json()
    assert cfg["approach_runway_heading"] == 180
    assert cfg["approach_runway_headings"] is None
    # ...and again on update
    assert client.post("/api/airport/config", json=dict(payload, approach_runway_heading=0)).status_code == 200
    cfg = client.get("/api/airport/config").json()
    assert cfg["approach_runway_heading"] == 0
    assert cfg["approach_runway_headings"] is None


def test_web_style_config_stores_the_list_and_desktop_can_override_it():
    client.post("/api/airport/config", json=AIRPORT)
    web = dict(AIRPORT, approach_corridor_enabled=True,
               approach_runway_headings=[180, 0], approach_runway_heading=180)
    assert client.post("/api/airport/config", json=web).status_code == 200
    cfg = client.get("/api/airport/config").json()
    assert cfg["approach_runway_headings"] == [180.0, 0.0]

    # Desktop echoes the fetched config back with only the rings changed: list kept.
    echoed = dict(cfg, alert_distances_nm=[5, 2])
    assert client.post("/api/airport/config", json=echoed).status_code == 200
    assert client.get("/api/airport/config").json()["approach_runway_headings"] == [180.0, 0.0]

    # Desktop picks a different single runway: its choice wins.
    assert client.post("/api/airport/config", json=dict(AIRPORT, approach_runway_heading=0)).status_code == 200
    cfg = client.get("/api/airport/config").json()
    assert cfg["approach_runway_headings"] is None
    assert cfg["approach_runway_heading"] == 0


def test_new_config_can_be_created_with_a_list():
    web = dict(AIRPORT, approach_corridor_enabled=True,
               approach_runway_headings=[180, 0], approach_runway_heading=180)
    assert client.post("/api/airport/config", json=web).status_code == 200
    assert client.get("/api/airport/config").json()["approach_runway_headings"] == [180.0, 0.0]


def test_saving_rings_keeps_a_custom_takeoff_message():
    client.post("/api/airport/config", json=AIRPORT)
    for kind in ("takeoff", "landing", "10nm"):
        r = client.post("/api/settings/alerts", json={"alert_type": kind, "enabled": True, "message_template": f"{kind} {{tail}}"})
        assert r.status_code == 200
    client.post("/api/airport/config", json=dict(AIRPORT, alert_distances_nm=[5, 2]))
    kinds = {s["alert_type"] for s in client.get("/api/settings/alerts").json()}
    assert kinds == {"takeoff", "landing"}


# ── a database where migrate_web_app.py has not run ──────────────────────────

def test_everything_the_desktop_uses_works_without_the_new_columns(monkeypatch):
    from sqlalchemy import text
    for stmt in (
        "ALTER TABLE users DROP COLUMN routing",
        "ALTER TABLE users DROP COLUMN gs_stats",
        "ALTER TABLE airport_configs DROP COLUMN approach_runway_headings",
    ):
        _shared.execute(text(stmt))
    _shared.commit()

    # Airport config: create, read, update with the payloads the desktop sends.
    payload = dict(AIRPORT, approach_corridor_enabled=True, approach_runway_heading=180)
    assert client.post("/api/airport/config", json=payload).status_code == 200
    cfg = client.get("/api/airport/config").json()
    assert cfg["approach_runway_heading"] == 180 and cfg["approach_runway_headings"] is None
    assert client.post("/api/airport/config", json=dict(cfg, alert_distances_nm=[5, 2])).status_code == 200
    assert client.post("/api/airport/config", json=dict(payload, approach_runway_heading=0)).status_code == 200
    assert client.get("/api/airport/config").json()["approach_runway_heading"] == 0

    # Ground station heartbeat and status, from old and new agents.
    assert client.post("/api/ground/heartbeat", json={}).json() == {"ok": True}
    assert client.post("/api/ground/heartbeat", json={"version": "3.7", "uptime_s": 5}).json() == {"ok": True}
    status = client.get("/api/ground/status").json()
    assert status["online"] is True and status["stats"] is None

    # Integrations, and alerts actually going out.
    a = _integration("discord")
    _integration("email")
    assert len(client.get("/api/integrations").json()) == 2
    assert client.get("/api/settings/routing").json() == {"routing": {}}
    sent = []

    async def fake_send(integration, message):
        sent.append(integration.type)
        return True

    monkeypatch.setattr(main.tracker, "send_via_integration", fake_send)
    note = {"type": "10nm", "tail": "N172AK", "distance": 10.2, "altitude": 2500, "eta": 6, "speed": 95, "time": datetime.now()}
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [note]))
    assert sorted(sent) == ["discord", "email"]
    assert client.delete(f"/api/integrations/{a}").status_code == 200

    # The tracker still loads the user's aircraft and config.
    _shared.add(Aircraft(user_id=USER_ID, tail_number="N172AK", icao24="abc123"))
    _shared.commit()
    asyncio.run(main.tracker.update_user_aircraft(str(USER_ID), _shared))
    airspace = main.tracker.user_trackers[str(USER_ID)].config["airspace"]
    assert airspace["approach_runway_heading"] == 0 and airspace["approach_runway_headings"] is None


# ── tracker ──────────────────────────────────────────────────────────────────

def _track(lat_offset_nm, track):
    return {
        "icao24": "abc123", "callsign": "N172AK", "on_ground": False,
        "latitude": 33.2007 + lat_offset_nm / 60.0, "longitude": -97.1980,
        "baro_altitude": 2500, "track": track, "heading": track, "velocity": 95,
    }


def _load_tracker(headings, single=180):
    payload = dict(AIRPORT, approach_corridor_enabled=True, approach_runway_heading=single)
    if headings is not None:
        payload["approach_runway_headings"] = headings
    assert client.post("/api/airport/config", json=payload).status_code == 200
    _shared.add(Aircraft(user_id=USER_ID, tail_number="N172AK", icao24="abc123"))
    _shared.commit()
    asyncio.run(main.tracker.update_user_aircraft(str(USER_ID), _shared))
    return main.tracker.user_trackers[str(USER_ID)]


def test_tracker_single_heading_filters_as_before():
    ut = _load_tracker(None, single=180)
    assert ut.config["airspace"]["approach_runway_headings"] is None
    asyncio.run(ut.check_and_notify(_track(12, 0)))
    # Northbound (track 0) crossing the 10 nm ring is not on the 180 approach.
    assert asyncio.run(ut.check_and_notify(_track(10.5, 0))) == []


def test_tracker_heading_list_allows_either_runway():
    ut = _load_tracker([180, 0])
    assert ut.config["airspace"]["approach_runway_headings"] == [180.0, 0.0]
    asyncio.run(ut.check_and_notify(_track(12, 0)))
    fired = asyncio.run(ut.check_and_notify(_track(10.5, 0)))
    assert [n["type"] for n in fired] == ["10nm"]


def test_tracker_heading_list_still_rejects_a_crossing_track():
    ut = _load_tracker([180, 0])
    asyncio.run(ut.check_and_notify(_track(12, 90)))
    assert asyncio.run(ut.check_and_notify(_track(10.5, 90))) == []


def test_send_notifications_honours_routing(monkeypatch):
    discord, email = _integration("discord"), _integration("email")
    client.put("/api/settings/routing", json={"routing": {"10nm": [discord]}})
    sent = []

    async def fake_send(integration, message):
        sent.append(integration.type)
        return True

    monkeypatch.setattr(main.tracker, "send_via_integration", fake_send)
    base = {"tail": "N172AK", "distance": 10.2, "altitude": 2500, "eta": 6, "speed": 95, "time": datetime.now()}

    asyncio.run(main.tracker.send_notifications(str(USER_ID), [dict(base, type="10nm")]))
    assert sent == ["email"]

    sent.clear()
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [dict(base, type="landing", heading=180)]))
    assert sorted(sent) == ["discord", "email"]

    _shared.expire_all()
    logged = sorted((l.alert_type, l.integration_type) for l in _shared.query(NotificationLog).all())
    assert logged == [("10nm", "email"), ("landing", "discord"), ("landing", "email")]


def test_send_notifications_without_routing_reaches_every_channel(monkeypatch):
    _integration("discord"), _integration("email")
    sent = []

    async def fake_send(integration, message):
        sent.append(integration.type)
        return True

    monkeypatch.setattr(main.tracker, "send_via_integration", fake_send)
    note = {"type": "10nm", "tail": "N172AK", "distance": 10.2, "altitude": 2500, "eta": 6, "speed": 95, "time": datetime.now()}
    asyncio.run(main.tracker.send_notifications(str(USER_ID), [note]))
    assert sorted(sent) == ["discord", "email"]
