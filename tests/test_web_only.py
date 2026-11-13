"""The desktop apps are retired: signing in and signed-in requests are served
only when the website sent them. Receivers keep working with their device key.

Uses the app and throwaway database set up in test_api_integration.
"""
import pytest

import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

main = api.main
client = api.client
WEB = {"x-internal-secret": main.WEBHOOK_INTERNAL_SECRET}


@pytest.fixture
def real_auth():
    """Run the real sign-in checks instead of the stand-in user."""
    saved = dict(main.app.dependency_overrides)
    main.app.dependency_overrides.clear()
    yield {"Authorization": f"Bearer {main.create_access_token(str(api.USER_ID))}"}
    main.app.dependency_overrides.update(saved)


def _retired(res):
    return res.status_code == 410 and res.json()["detail"] == main.DESKTOP_RETIRED_MESSAGE


def test_desktop_cannot_sign_in():
    assert _retired(client.post("/api/auth/login", json={"email": "pilot@example.com", "password": "x"}))
    assert _retired(client.post("/api/auth/google-desktop", json={"email": "pilot@example.com", "token": "x"}))
    assert _retired(client.post("/api/activate", json={"license_key": "X", "email": "pilot@example.com"}))
    assert _retired(client.get("/api/app/version"))


def test_update_check_still_answers_the_website():
    assert client.get("/api/app/version", headers=WEB).status_code == 200


def test_desktop_token_is_refused(real_auth):
    assert _retired(client.get("/api/user/me", headers=real_auth))
    assert _retired(client.get("/api/aircraft/live", headers=real_auth))
    assert _retired(client.post("/api/auth/refresh", headers=real_auth))


def test_same_token_works_through_the_website(real_auth):
    res = client.get("/api/user/me", headers={**real_auth, **WEB})
    assert res.status_code == 200
    assert res.json()["email"] == "pilot@example.com"


def test_wrong_secret_is_refused(real_auth):
    assert _retired(client.get("/api/user/me", headers={**real_auth, "x-internal-secret": "nope"}))


def test_receiver_device_key_needs_no_website(real_auth):
    key = "ab" * 32
    user = api._shared.get(api.User, api.USER_ID)
    user.gs_device_key = key
    api._shared.commit()
    res = client.post("/api/ground/positions", json={"positions": {}}, headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 200


def test_ground_status_with_a_desktop_token_is_refused(real_auth):
    assert _retired(client.get("/api/ground/status", headers=real_auth))
    assert client.get("/api/ground/status", headers={**real_auth, **WEB}).status_code == 200
