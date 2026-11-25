"""Saved locations: which one is active, switching between them, editing, no duplicates."""
import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

client = api.client

KDTO = {"name": "Denton Enterprise Airport", "airport_code": "KDTO", "latitude": "33.2018", "longitude": "-97.198", "elevation_ft_msl": 643}
KDFW = {"name": "Dallas Fort Worth International Airport", "airport_code": "KDFW", "latitude": "32.8968", "longitude": "-97.038", "elevation_ft_msl": 607}


def _home(code="KDTO"):
    body = dict(api.AIRPORT, airport_code=code)
    assert client.post("/api/airport/config", json=body).status_code == 200


def _active():
    return {loc["airport_code"]: loc["is_active"] for loc in client.get("/api/locations").json()}


def test_a_new_location_is_not_active_when_the_home_airport_is_another():
    _home("KDTO")
    client.post("/api/locations", json=KDFW)
    assert _active() == {"KDFW": False}          # it used to say Active


def test_only_the_home_airport_is_active_and_switching_moves_it():
    _home("KDTO")
    client.post("/api/locations", json=KDTO)
    client.post("/api/locations", json=KDFW)
    assert _active() == {"KDTO": True, "KDFW": False}
    dfw = next(loc for loc in client.get("/api/locations").json() if loc["airport_code"] == "KDFW")
    assert client.post(f"/api/locations/{dfw['id']}/activate").status_code == 200
    assert _active() == {"KDTO": False, "KDFW": True}
    cfg = client.get("/api/airport/config").json()
    assert cfg["airport_code"] == "KDFW" and cfg["airport_name"] == KDFW["name"]
    # Changing the home airport directly moves "active" too.
    _home("KDTO")
    assert _active() == {"KDTO": True, "KDFW": False}


def test_the_same_airport_is_not_saved_twice():
    _home("KDTO")
    a = client.post("/api/locations", json=KDFW).json()
    b = client.post("/api/locations", json=dict(KDFW, airport_code="kdfw")).json()
    assert a["id"] == b["id"] and len(client.get("/api/locations").json()) == 1


def test_a_saved_location_can_be_changed_to_another_airport():
    _home("KDTO")
    loc = client.post("/api/locations", json=KDFW).json()
    res = client.put(f"/api/locations/{loc['id']}", json=dict(KDTO, name="Denton"))
    assert res.status_code == 200
    (only,) = client.get("/api/locations").json()
    assert only["airport_code"] == "KDTO" and only["name"] == "Denton" and only["is_active"] is True
