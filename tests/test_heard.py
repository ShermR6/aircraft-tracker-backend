"""Every aircraft heard and what it broadcasts on: the receiver's census, the
tracker's view of tracked aircraft, and the admin report."""
from datetime import datetime, timedelta

import heard
import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

client = api.client
db = api._shared
SECRET = {"x-internal-secret": api.main.WEBHOOK_INTERNAL_SECRET}


def test_link_from_decoder_labels():
    assert heard.link_of({"adsb_icao": 40}) == "1090"
    assert heard.link_of({"adsb_icao": 3, "mlat": 50}) == "1090"
    assert heard.link_of({"adsr_icao": 12}) == "978"
    assert heard.link_of({"mode_s": 30}) == "none"
    assert heard.link_of({"tisb_icao": 5, "mode_s": 2}) == "none"
    assert heard.link_of({"mlat": 9}) == "none"
    assert heard.link_of({"unknown": 4}) == "unknown"
    assert heard.link_of({}) == "unknown"


def test_us_registration_from_the_address():
    assert heard.n_number("a00001") == "N1"
    assert heard.n_number("A00002") == "N1A"
    assert heard.n_number("a835af") == "N628TS"
    assert heard.n_number("adf7c7") == "N99999"
    assert heard.n_number("c0ffee") is None          # Canada
    assert heard.n_number("~a835af") is None         # a non-ICAO track
    assert heard.n_number("zz") is None


def _rows():
    from models import HeardAircraft
    return {r.icao24: r for r in db.query(HeardAircraft).filter(HeardAircraft.owner == str(api.USER_ID)).all()}


def test_census_records_each_aircraft_and_adds_up_across_reports():
    first = {
        "a835af": {"types": {"adsb_icao": 50}, "flight": "N628TS  ", "min_alt": 3200, "min_nm": 4.4},
        "a00002": {"types": {"adsr_icao": 20}, "min_alt": 1500, "min_nm": 2.0},
        "a12345": {"types": {"mode_s": 7}},
        "zzzzzz": {"types": {"adsb_icao": 1}},                     # not an address: dropped
    }
    res = client.post("/api/ground/census", json={"aircraft": first})
    assert res.status_code == 200 and res.json() == {"ok": True, "aircraft": 3}
    rows = _rows()
    assert set(rows) == {"a835af", "a00002", "a12345"}
    assert rows["a835af"].link == "1090" and rows["a835af"].registration == "N628TS" and rows["a835af"].callsign == "N628TS"
    assert rows["a00002"].link == "978"
    assert rows["a12345"].link == "none"

    second = {"a835af": {"types": {"adsb_icao": 10}, "min_alt": 2100, "min_nm": 6.0, "ground": True}}
    client.post("/api/ground/census", json={"aircraft": second})
    db.expire_all()
    row = _rows()["a835af"]
    assert row.kinds == {"adsb_icao": 60} and row.reports == 2
    assert row.min_alt == 2100 and row.min_nm == 4.4 and row.ground is True


def test_census_rejects_a_bad_body():
    assert client.post("/api/ground/census", json={"aircraft": []}).status_code == 400
    assert client.post("/api/ground/census", json=[1, 2]).status_code == 400


def test_tracked_aircraft_are_recorded_from_adsb_lol():
    heard._network.clear()
    heard._last_flush = None
    for _ in range(3):
        heard.note_network("a835af", {"type": "adsr_icao", "alt_baro": 2500, "flight": "N628TS"})
    heard.note_network("a00001", {"alt_baro": 1000})          # no label: nothing to learn
    heard.flush(lambda: db)
    from models import HeardAircraft
    row = db.query(HeardAircraft).filter(HeardAircraft.owner == heard.NETWORK).one()
    assert row.icao24 == "a835af" and row.link == "978" and row.kinds == {"adsr_icao": 3}
    # Saved at most every few minutes.
    heard.note_network("a835af", {"type": "adsr_icao"})
    heard.flush(lambda: db)
    db.expire_all()
    assert db.query(HeardAircraft).filter(HeardAircraft.owner == heard.NETWORK).one().reports == 1


def test_report_groups_by_receiver_and_marks_tracked_aircraft():
    db.add(api.Aircraft(user_id=api.USER_ID, tail_number="N628TS", icao24="a835af"))
    db.commit()
    client.post("/api/ground/census", json={"aircraft": {
        "a835af": {"types": {"adsb_icao": 5}}, "a00002": {"types": {"adsr_icao": 5}}}})
    assert client.get("/api/internal/heard").status_code == 403
    body = client.get("/api/internal/heard?days=7", headers=SECRET).json()
    assert body["link_names"]["978"] == "978 MHz UAT"
    g = next(g for g in body["groups"] if g["owner"] == "receiver")
    assert g["name"] == "pilot@example.com"
    assert g["counts"] == {"1090": 1, "978": 1, "none": 0, "unknown": 0}
    by = {a["icao24"]: a for a in g["aircraft"]}
    assert by["a835af"]["tracked_as"] == "N628TS" and by["a00002"]["registration"] == "N1A"


def test_old_aircraft_are_left_out_of_the_report():
    heard.record(db, str(api.USER_ID), {"a00002": {"types": {"adsb_icao": 1}}}, now=datetime.utcnow() - timedelta(days=40))
    body = client.get("/api/internal/heard?days=30", headers=SECRET).json()
    assert body["groups"] == []


def test_the_aircraft_list_says_what_each_aircraft_broadcasts_on():
    for n, icao in enumerate(("a835af", "a00002", "a12345", "a00003")):
        db.add(api.Aircraft(user_id=api.USER_ID, tail_number=f"N{n}T", icao24=icao))
    db.commit()
    # adsb.lol heard one as a UAT rebroadcast; another account's receiver
    # heard the next one on 1090 and the third with no ADS-B out.
    heard.record(db, heard.NETWORK, {"a835af": {"types": {"adsr_icao": 9}}})
    heard.record(db, str(api.OTHER_ID), {"a00002": {"types": {"adsb_icao": 4, "mlat": 9}}, "a12345": {"types": {"mode_s": 3}}})
    got = {a["icao24"]: a["link"] for a in client.get("/api/aircraft").json()}
    assert got == {"a835af": "978", "a00002": "1090", "a12345": "none", "a00003": None}
