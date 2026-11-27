"""The coverage-gap log and the retried adsb.lol request.

Unit tests drive coverage.observe with explicit times; the cycle tests drive
the real tracker with the fake adsb.lol from test_gs_and_adsb_together.
"""
import asyncio
from datetime import datetime, timedelta

import coverage
import test_gs_and_adsb_together as t
import tracker as tracker_mod
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

api = t.api
AIRSPACE = {"center_lat": t.LAT0, "center_lon": t.LON0, "field_elevation_ft_msl": 642}
T0 = datetime(2026, 11, 18, 12, 0, 0)


def _reset():
    coverage._last.clear()
    coverage._pending.clear()
    coverage.fetches.clear()


def _pos(nm, s, alt=2500, on_ground=False, source="adsb", kind=None):
    return coverage.position(source, t.LAT0 + nm / 60.0, t.LON0, T0 + timedelta(seconds=s),
                             alt_ft=alt, on_ground=on_ground, kind=kind)


def _see(nm, s, **k):
    coverage.observe("u1", "abc123", "N172AK", AIRSPACE, [_pos(nm, s, **k)], True, False, T0 + timedelta(seconds=s))


def _miss(s, adsb_ok=True, receiver_up=False):
    coverage.observe("u1", "abc123", "N172AK", AIRSPACE, [None], adsb_ok, receiver_up, T0 + timedelta(seconds=s))


# ── what counts as a gap ─────────────────────────────────────────────────────

def test_a_quiet_stretch_near_the_field_is_logged_with_where_and_how_low():
    _reset()
    _see(6, 0, alt=2142)
    _miss(30); _miss(60); _miss(90)
    _see(3, 120, alt=1442, kind="mlat")
    assert len(coverage._pending) == 1
    g = coverage._pending[0]
    assert g["outcome"] == "back" and g["seconds"] == 120
    assert round(g["start_nm"]) == 6 and g["start_agl"] == 1500
    assert round(g["end_nm"]) == 3 and g["end_agl"] == 800 and g["end_source"] == "mlat"
    assert g["missed_cycles"] == 3 and g["fetch_failures"] == 0 and g["receiver"] == "down"


def test_normal_polling_and_one_missed_poll_are_not_gaps():
    _reset()
    for i in range(6):
        _see(10 - i, i * 30)
    _miss(180)
    _see(3, 210)                       # 30 s since the last fix
    assert coverage._pending == []


def test_far_away_or_parked_aircraft_are_not_counted():
    _reset()
    _see(40, 0)                        # outside 25 nm
    _miss(30); _miss(60)
    _see(39, 300)
    _see(0.1, 330, on_ground=True)     # parked
    _miss(360); _miss(390)
    _see(0.1, 900, on_ground=True)
    assert coverage._pending == []


def test_a_stale_fix_repeated_by_adsb_is_not_news():
    _reset()
    _see(4, 0)
    # adsb.lol keeps handing back the same old fix, a fraction of a second apart.
    for s in (30, 60, 90):
        coverage.observe("u1", "abc123", "N172AK", AIRSPACE, [_pos(4, 0.4)], True, False, T0 + timedelta(seconds=s))
    _see(2, 120)
    assert len(coverage._pending) == 1 and coverage._pending[0]["missed_cycles"] == 3


def test_never_heard_again_is_logged_once_after_thirty_minutes():
    _reset()
    _see(1.2, 0, alt=942)              # 300 ft on short final, then nothing
    for s in range(30, 40 * 60, 30):
        _miss(s)
    assert len(coverage._pending) == 1
    g = coverage._pending[0]
    assert g["outcome"] == "not_seen_again" and g["ended_at"] is None and g["start_agl"] == 300
    _see(1, 50 * 60, on_ground=True)   # seen again much later: not logged twice
    assert len(coverage._pending) == 1


def test_failed_requests_and_the_receiver_are_counted():
    _reset()
    _see(5, 0)
    _miss(30, adsb_ok=False, receiver_up=True)
    _miss(60, adsb_ok=False, receiver_up=True)
    _miss(90, receiver_up=False)
    _see(4, 120)
    g = coverage._pending[0]
    assert g["fetch_failures"] == 2 and g["receiver"] == "part"


def test_the_newest_source_wins():
    _reset()
    _see(5, 0)
    coverage.observe("u1", "abc123", "N172AK", AIRSPACE,
                     [_pos(4, 100), _pos(3.5, 115, source="receiver", kind="receiver")], True, True, T0 + timedelta(seconds=120))
    g = coverage._pending[0]
    assert g["seconds"] == 115 and g["end_source"] == "receiver"


# ── through the real tracker cycle ───────────────────────────────────────────

def test_cycle_logs_a_gap_and_saves_it(monkeypatch):
    _reset()
    t._setup(monkeypatch, rings=(10, 2))
    t.adsb_at(6); t.cycle()
    key = (t.UID, t.ICAO)
    assert key in coverage._last
    t.adsb_gone(); t.cycle(); t.cycle()
    coverage._last[key]["at"] -= timedelta(seconds=90)    # pretend the polls were 30 s apart
    t.adsb_at(3); t.cycle()
    assert len(coverage._pending) == 1
    coverage.flush(lambda: t.db)
    from models import CoverageGap
    row = t.db.query(CoverageGap).one()
    assert row.tail == t.TAIL and row.outcome == "back" and row.missed_cycles == 2

    res = api.client.get("/api/internal/coverage", headers={"x-internal-secret": api.main.WEBHOOK_INTERNAL_SECRET})
    assert res.status_code == 200
    body = res.json()
    assert body["total"]["gaps"] == 1
    assert body["by_cause"] == {"network lost it": {"gaps": 1, "minutes": round(row.seconds / 60, 1)}}
    assert body["recent"][0]["tail"] == t.TAIL
    (pt,) = body["points"]
    assert pt["tail"] == t.TAIL and pt["cause"] == "network lost it" and pt["end_lat"] is not None
    assert round(pt["lat"], 2) == round(t._north(6), 2)
    assert body["fetch"]["requests"] == 4 and body["fetch"]["failed"] == 0


def test_coverage_report_needs_the_secret():
    assert api.client.get("/api/internal/coverage").status_code == 403


# ── retrying adsb.lol ────────────────────────────────────────────────────────

class _Flaky(t._Session):
    """Times out (or errors) the first `bad` tries, then answers."""
    def __init__(self, *a, **k):
        pass

    def get(self, url, timeout=None):
        RETRY["tries"] += 1
        RETRY["timeouts"].append(timeout.total if timeout else None)
        if RETRY["tries"] <= RETRY["bad"]:
            if RETRY["status"]:
                return t._Resp(RETRY["status"], {})
            raise asyncio.TimeoutError()
        return t._Resp(200, {"ac": [{"hex": "ABC123", "lat": 1, "lon": 2, "seen_pos": 1}]})


RETRY = {}


def _flaky(monkeypatch, bad, status=None):
    _reset()
    RETRY.update(tries=0, bad=bad, status=status, timeouts=[])
    monkeypatch.setattr(tracker_mod.aiohttp, "ClientSession", _Flaky)
    monkeypatch.setattr(tracker_mod, "ADSB_RETRY_PAUSE_SECONDS", 0)
    return asyncio.run(api.main.tracker.fetch_adsb(["abc123"]))


def test_a_timeout_is_retried_and_answers(monkeypatch):
    lookup, ok = _flaky(monkeypatch, bad=1)
    assert ok and "abc123" in lookup and RETRY["tries"] == 2
    assert all(x <= tracker_mod.ADSB_TRY_SECONDS for x in RETRY["timeouts"])
    f = coverage.fetches[-1]
    assert f["ok"] and f["attempts"] == 2 and f["error"] == "timeout"
    assert coverage.fetch_summary()["ok_after_retry"] == 1


def test_three_failures_give_up(monkeypatch):
    lookup, ok = _flaky(monkeypatch, bad=5, status=503)
    assert not ok and lookup == {} and RETRY["tries"] == tracker_mod.ADSB_MAX_TRIES
    assert coverage.fetch_summary()["errors"] == {"HTTP 503": 1}


def test_a_4xx_is_not_retried(monkeypatch):
    lookup, ok = _flaky(monkeypatch, bad=5, status=403)
    assert not ok and RETRY["tries"] == 1


# ── FlyItalyADSB as a second source ──────────────────────────────────────────

def test_sources_merge_by_the_fresher_position():
    adsb = {"a1": {"hex": "a1", "seen_pos": 12.0}, "a2": {"hex": "a2", "seen_pos": 1.0}, "a3": {"hex": "a3"}}
    fia = {"a1": {"hex": "a1", "seen_pos": 2.0}, "a2": {"hex": "a2", "seen_pos": 5.0}, "a4": {"hex": "a4", "seen_pos": 3.0}}
    m = tracker_mod._merge_sources(adsb, fia)
    assert {k: v["_src"] for k, v in m.items()} == {
        "a1": "FlyItalyADSB", "a2": "adsb.lol", "a3": "adsb.lol", "a4": "FlyItalyADSB"}


class _Fia(t._Session):
    """FlyItalyADSB stand-in: answers per batch, notes each URL."""
    def __init__(self, *a, **k):
        FIA["headers"] = k.get("headers")

    def get(self, url, timeout=None):
        FIA["urls"].append(url)
        if FIA["status"] != 200:
            return t._Resp(FIA["status"], {})
        hexes = url.rsplit("/", 1)[1].split(",")
        return t._Resp(200, {"ac": [{"hex": h, "lat": 1, "lon": 2, "seen_pos": 1} for h in hexes]})


FIA = {}


def _fia(monkeypatch, n, status=200, key="fia_test"):
    _reset()
    FIA.update(urls=[], status=status, headers=None)
    monkeypatch.setattr(tracker_mod, "FLYITALY_API_KEY", key)
    monkeypatch.setattr(tracker_mod.aiohttp, "ClientSession", _Fia)
    api.main.tracker._flyitaly_pause_until = 0.0
    return asyncio.run(api.main.tracker.fetch_flyitaly([f"a{i:05x}" for i in range(n)]))


def test_flyitaly_asks_in_batches_of_fifty_with_the_key(monkeypatch):
    lookup, ok = _fia(monkeypatch, 120)
    assert ok and len(lookup) == 120 and len(FIA["urls"]) == 3
    assert FIA["headers"] == {"X-Api-Key": "fia_test"}
    assert coverage.fetch_summary(source="FlyItalyADSB")["requests"] == 1
    assert coverage.fetch_summary(source="adsb.lol")["requests"] == 0


def test_flyitaly_without_a_key_asks_nothing(monkeypatch):
    lookup, ok = _fia(monkeypatch, 5, key="")
    assert (lookup, ok) == ({}, False) and FIA["urls"] == []


def test_flyitaly_backs_off_after_429(monkeypatch):
    lookup, ok = _fia(monkeypatch, 5, status=429)
    assert not ok and len(FIA["urls"]) == 1
    asyncio.run(api.main.tracker.fetch_flyitaly(["a00001"]))     # still paused
    assert len(FIA["urls"]) == 1


def test_cycle_uses_flyitaly_when_adsb_lol_refuses(monkeypatch):
    t._setup(monkeypatch, rings=(10, 2))
    sent = []

    async def fake_send(user_id, notifications):
        sent.extend(n["type"] for n in notifications)

    monkeypatch.setattr(api.main.tracker, "send_notifications", fake_send)
    t.ADSB["fail"] = True                      # adsb.lol answers 503

    where = {"nm": 12}

    async def fia(icaos):
        return {t.ICAO: {"hex": t.ICAO, "lat": t._north(where["nm"]), "lon": t.LON0, "alt_baro": 2500,
                         "gs": 110, "track": 180, "seen_pos": 1}}, True

    monkeypatch.setattr(api.main.tracker, "fetch_flyitaly", fia)
    monkeypatch.setattr(tracker_mod, "FLYITALY_API_KEY", "fia_test")
    t.cycle()                                  # first seen outside the ring
    where["nm"] = 9
    t.cycle()                                  # crosses 10 nm, on FlyItalyADSB alone
    assert sent == ["10nm"]


# ── adsb.lol's daily allowance ───────────────────────────────────────────────

def test_a_refusal_waits_for_the_allowance_to_reset():
    assert tracker_mod.adsb_refused_for({"x-ratelimit-reset": "20810"}) == 20810
    assert tracker_mod.adsb_refused_for({"x-ratelimit-reset": "5"}) == 60
    assert tracker_mod.adsb_refused_for({}) == 300


class _Limited(t._Session):
    """adsb.lol with a daily allowance: answers once, then refuses."""
    def __init__(self, *a, **k):
        pass

    def get(self, url, timeout=None):
        LIM["tries"] += 1
        r = t._Resp(LIM["status"], {"ac": []})
        r.headers = {"x-ratelimit-limit": "500, 500;w=86400", "x-ratelimit-reset": "3600"}
        return r


LIM = {}


def test_adsb_is_not_asked_again_until_its_allowance_allows(monkeypatch):
    _reset()
    monkeypatch.setattr(tracker_mod.aiohttp, "ClientSession", _Limited)
    trk = api.main.tracker
    try:
        trk._adsb_next_ok = 0.0
        LIM.update(tries=0, status=429)
        assert asyncio.run(trk.fetch_adsb(["abc123"]))[1] is False
        asyncio.run(trk.fetch_adsb(["abc123"]))                              # refused: wait for the reset
        assert LIM["tries"] == 1
    finally:
        trk._adsb_next_ok = 0.0


# ── FlyItalyADSB first, adsb.lol as the backup ───────────────────────────────

def _network(monkeypatch, fia_answer, fia_ok=True):
    """Drives fetch_network with stand-ins; returns the adsb.lol asks."""
    t._setup(monkeypatch, rings=(10, 2))
    trk = api.main.tracker
    trk._adsb_sent = []
    asked = []

    async def fia(icaos):
        return fia_answer, fia_ok

    async def adsb(icaos):
        asked.append(list(icaos))
        return {i: {"hex": i, "seen_pos": 1} for i in icaos}, True

    monkeypatch.setattr(tracker_mod, "FLYITALY_API_KEY", "fia_test")
    monkeypatch.setattr(trk, "fetch_flyitaly", fia)
    monkeypatch.setattr(trk, "fetch_adsb", adsb)
    return trk, asked


def _state(trk, **st):
    from datetime import datetime as _dt
    trk.user_trackers[t.UID].aircraft_state[t.ICAO] = dict(
        {"last_update": _dt.now(), "last_distance": 4.0, "on_ground": False, "landed": False}, **st)


def test_adsb_lol_is_not_asked_while_flyitaly_has_the_aircraft(monkeypatch):
    trk, asked = _network(monkeypatch, {t.ICAO: {"hex": t.ICAO, "seen_pos": 2}})
    _state(trk)
    lookup, ok = asyncio.run(trk.fetch_network([t.ICAO]))
    assert ok and asked == [] and lookup[t.ICAO]["_src"] == "FlyItalyADSB"


def test_adsb_lol_backs_up_an_aircraft_flyitaly_lost_near_the_field(monkeypatch):
    trk, asked = _network(monkeypatch, {"other1": {"hex": "other1", "seen_pos": 1}})
    _state(trk, last_distance=1.5)
    lookup, ok = asyncio.run(trk.fetch_network([t.ICAO, "other1"]))
    assert asked == [[t.ICAO]] and lookup[t.ICAO]["_src"] == "adsb.lol"


def test_a_parked_or_far_aircraft_is_not_worth_a_backup_request(monkeypatch):
    trk, asked = _network(monkeypatch, {})
    _state(trk, on_ground=True)
    asyncio.run(trk.fetch_network([t.ICAO]))
    _state(trk, last_distance=40.0)
    asyncio.run(trk.fetch_network([t.ICAO]))
    assert asked == []


def test_when_flyitaly_fails_adsb_lol_is_asked_once_a_minute(monkeypatch):
    trk, asked = _network(monkeypatch, {}, fia_ok=False)
    assert asyncio.run(trk.fetch_network([t.ICAO]))[1] is True
    asyncio.run(trk.fetch_network([t.ICAO]))
    assert asked == [[t.ICAO]]


def test_adsb_lol_stays_within_its_daily_budget(monkeypatch):
    trk, asked = _network(monkeypatch, {}, fia_ok=False)
    import time as _time
    trk._adsb_sent = [_time.monotonic() - 3600] * tracker_mod.ADSB_DAILY_BUDGET
    asyncio.run(trk.fetch_network([t.ICAO]))
    assert asked == []
