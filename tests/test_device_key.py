"""The Ground Station device key, treated like an API key: any signed-in
account can get one (no plan needed), it is only sent when asked for, and
replacing it locks out the old one.

Uses the app and throwaway database set up in test_api_integration.
"""
import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

main = api.main
client = api.client
db = api._shared
USER_ID = api.USER_ID


def _user():
    db.expire_all()
    return db.get(api.User, USER_ID)


def _clear_key():
    user = _user()
    user.gs_device_key = None
    db.commit()


def test_key_is_made_on_first_ask_and_hidden_unless_revealed():
    _clear_key()
    res = client.get("/api/ground/key")
    assert res.status_code == 200
    body = res.json()
    assert body["key"] is None
    key = _user().gs_device_key
    assert key and len(key) == 64
    assert body["hint"] == key[-4:]

    shown = client.get("/api/ground/key?reveal=true").json()
    assert shown["key"] == key
    # asking again does not make a new one
    assert client.get("/api/ground/key").json()["hint"] == key[-4:]
    assert _user().gs_device_key == key


def test_accounts_without_a_plan_can_get_their_key():
    api._drop_plan()
    user = _user()
    user.ground_station_enabled = False
    db.commit()
    res = client.get("/api/ground/key?reveal=true")
    assert res.status_code == 200
    assert res.json()["key"]
    assert res.json()["ground_station_enabled"] is False


def test_replacing_the_key_locks_out_the_old_one():
    old = client.get("/api/ground/key?reveal=true").json()["key"]
    res = client.post("/api/ground/key/rotate")
    assert res.status_code == 200
    new = res.json()["key"]
    assert new and new != old and len(new) == 64
    assert _user().gs_device_key == new

    # The receiver's own sign-in, without the test override, is what matters.
    session = api._shared
    try:
        main._authenticate_device_key(old, session)
        raise AssertionError("old key still accepted")
    except main.HTTPException as e:
        assert e.status_code == 401
    assert main._authenticate_device_key(new, session).id == USER_ID


def test_status_sends_only_a_hint():
    key = client.get("/api/ground/key?reveal=true").json()["key"]
    body = client.get("/api/ground/status").json()
    assert "gs_device_key" not in body
    assert body["gs_key_hint"] == key[-4:]
