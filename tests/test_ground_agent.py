import finalping_ground as g


def setup_function(_):
    g._msg_total = None
    g._sbs_lines = 0
    g._rate_mark = None


def test_rate_is_unknown_until_there_are_two_samples():
    g._note_messages({"messages": 1000})
    assert g.message_rate(100.0) is None
    g._note_messages({"messages": 1600})
    assert g.message_rate(160.0) == 10.0


def test_rate_survives_a_dump1090_restart():
    g._note_messages({"messages": 5000})
    g.message_rate(0.0)
    g._note_messages({"messages": 40})       # counter reset
    assert g.message_rate(60.0) is None
    g._note_messages({"messages": 640})
    assert g.message_rate(120.0) == 10.0


def test_rate_falls_back_to_sbs_lines():
    g._sbs_lines = 100
    assert g.message_rate(0.0) is None
    g._sbs_lines = 400
    assert g.message_rate(30.0) == 10.0


def test_rate_unknown_with_no_source():
    assert g.message_rate(0.0) is None
    assert g.message_rate(60.0) is None


def test_payload_without_a_counter_is_ignored():
    g._note_messages({"aircraft": []})
    g._note_messages(["not", "a", "dict"])
    g._note_messages({"messages": True})
    assert g._msg_total is None


def test_heartbeat_stats_shape():
    stats = g.heartbeat_stats(g.START_TIME + 125.7, 12)
    assert stats["version"] == g.VERSION
    assert stats["uptime_s"] == 125
    assert stats["aircraft_count"] == 12
    assert "msg_rate" in stats
    assert stats["feeding"] is None      # not asked yet


class _Reply:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code != 200:
            raise g.requests.HTTPError(response=self)

    def json(self):
        return self._body


MINE = "abcd1234-ef56-7890-aaaa-bbbbccccdddd"
SHOWN = "abcd1234-ef56-..."          # adsb.lol shows only the start of an id


def _feed_reset(monkeypatch=None, mine=MINE):
    g._feed.update(feeding=None, at=0.0, checked=0.0, why=None, traffic_at=None, mlat=None, mlat_peers=None)
    if monkeypatch is not None:
        monkeypatch.setattr(g, "feeder_id", lambda: mine)


def _me(*beast):
    return lambda *a, **k: _Reply({"clients": {"beast": list(beast), "mlat": []}})


def _client(uuid=SHOWN, rate=3.5):
    return {"uuid": uuid, "messages_per_second": rate, "kbps": 0.1}


def test_feeding_is_this_machines_own_feed_carrying_data(monkeypatch):
    _feed_reset(monkeypatch)
    monkeypatch.setattr(g.requests, "get", _me(_client()))
    assert g.check_feed(1000.0) is True
    stats = g.heartbeat_stats(g.START_TIME + 5, 0)
    assert stats["feeding"] is True and stats["feed_why"] is None
    monkeypatch.setattr(g.requests, "get", _me())
    assert g.check_feed(2000.0) is False
    assert g.heartbeat_stats(g.START_TIME + 5, 0)["feed_why"] == "not_connected"


def test_mlat_is_this_machines_own_mlat_client(monkeypatch):
    _feed_reset(monkeypatch)
    reply = {"clients": {"beast": [_client()], "mlat": [{"uuid": SHOWN, "peer_count": 7, "user": "x"}]}}
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _Reply(reply))
    g.check_feed(1000.0)
    stats = g.heartbeat_stats(g.START_TIME + 5, 0)
    assert stats["mlat"] is True and stats["mlat_peers"] == 7
    # someone else's MLAT client on the same network is not this machine's
    reply["clients"]["mlat"] = [{"uuid": "99999999-0000-...", "peer_count": 3}]
    g.check_feed(2000.0)
    stats = g.heartbeat_stats(g.START_TIME + 5, 0)
    assert stats["feeding"] is True and stats["mlat"] is False and stats["mlat_peers"] is None
    # adsb.lol not answering for hours: unknown, not "off"
    def down(*a, **k):
        raise OSError("no route")
    monkeypatch.setattr(g.requests, "get", down)
    g.check_feed(2000.0 + g.FEED_ANSWER_KEPT_S + 60)
    assert g.heartbeat_stats(g.START_TIME + 5, 0)["mlat"] is None


def test_another_feeder_on_the_same_network_does_not_count(monkeypatch):
    _feed_reset(monkeypatch)
    monkeypatch.setattr(g.requests, "get", _me(_client(uuid="99999999-0000-..."), {"kbps": 1}, "junk"))
    assert g.check_feed(1000.0) is False
    assert g._feed["why"] == "not_connected"


def test_a_machine_with_no_feed_client_is_not_feeding(monkeypatch):
    _feed_reset(monkeypatch, mine=None)
    monkeypatch.setattr(g.requests, "get", _me(_client()))
    assert g.check_feed(1000.0) is False
    assert g._feed["why"] == "no_feed_client"
    assert g.heartbeat_stats(g.START_TIME + 5, 0)["feed_id"] is None


def test_a_connected_feed_with_no_radio_behind_it_is_not_feeding(monkeypatch):
    _feed_reset(monkeypatch)
    monkeypatch.setattr(g.requests, "get", _me(_client(rate=0)))
    assert g.check_feed(1000.0) is False
    assert g._feed["why"] == "no_data"
    monkeypatch.setattr(g.requests, "get", _me(_client(rate=True)))     # not a number
    assert g.check_feed(1100.0) is False


def test_a_quiet_spell_still_counts_but_not_for_ever(monkeypatch):
    _feed_reset(monkeypatch)
    monkeypatch.setattr(g.requests, "get", _me(_client(rate=2.0)))
    assert g.check_feed(1000.0) is True
    monkeypatch.setattr(g.requests, "get", _me(_client(rate=0)))
    assert g.check_feed(1000.0 + g.FEED_QUIET_ALLOWED_S - 60) is True
    assert g.check_feed(1000.0 + g.FEED_QUIET_ALLOWED_S + 60) is False
    assert g._feed["why"] == "no_data"


def test_the_receiver_says_which_machine_it_is_without_sending_the_serial(monkeypatch):
    files = {
        "/sys/firmware/devicetree/base/serial-number": "10000000abcdef01",
        "/sys/firmware/devicetree/base/model": "Raspberry Pi Zero 2 W Rev 1.0",
        g.ADSBLOL_UUID_FILE: MINE + "\n",
    }
    monkeypatch.setattr(g, "_read", lambda path: files.get(path, "").strip())
    monkeypatch.setattr(g, "_hardware", None)
    hw, model = g.hardware()
    assert model == "Raspberry Pi Zero 2 W Rev 1.0"
    assert len(hw) == 32 and "abcdef01" not in hw
    assert g.hardware() == (hw, model)                      # the same every time
    assert g.feeder_id() == MINE
    stats = g.heartbeat_stats(g.START_TIME + 5, 0)
    assert stats["hw"] == hw and stats["hw_model"] == model
    assert len(stats["feed_id"]) == 32 and MINE[:8] not in stats["feed_id"]

    # not a Pi: the system's machine id stands in
    files.pop("/sys/firmware/devicetree/base/serial-number")
    files.pop("/sys/firmware/devicetree/base/model")
    files["/etc/machine-id"] = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(g, "_hardware", None)
    other, other_model = g.hardware()
    assert other and other != hw and other_model
    # nothing to go on at all
    files.pop("/etc/machine-id")
    monkeypatch.setattr(g, "_hardware", None)
    assert g.hardware() == (None, None)
    monkeypatch.setattr(g, "_hardware", None)


def test_the_feed_is_asked_about_in_the_background(monkeypatch):
    _feed_reset(monkeypatch)
    g._feed.pop("busy", None)
    monkeypatch.setattr(g.requests, "get", _me(_client()))
    assert g.feed_check_due(5000.0)
    t = g.start_feed_check(5000.0)
    t.join(5)
    assert g._feed["feeding"] is True and not g._feed["busy"]
    assert not g.feed_check_due(5000.0 + 30)                 # asked a moment ago
    # one question at a time
    g._feed["busy"] = True
    assert g.start_feed_check(9000.0) is None and not g.feed_check_due(99999.0)
    g._feed["busy"] = False


def test_adsblol_not_answering_keeps_the_last_answer_for_a_while(monkeypatch):
    _feed_reset(monkeypatch)
    monkeypatch.setattr(g.requests, "get", _me(_client()))
    assert g.check_feed(1000.0) is True

    def down(*a, **k):
        raise g.requests.ConnectionError("no route")

    monkeypatch.setattr(g.requests, "get", down)
    assert g.check_feed(1000.0 + g.FEED_ANSWER_KEPT_S - 60) is True
    assert g.check_feed(1000.0 + g.FEED_ANSWER_KEPT_S + 1) is None
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _Reply({}, status=503))
    assert g.check_feed(9000.0) is None


def test_feed_is_rechecked_sooner_while_it_is_down():
    _feed_reset()
    g._feed.update(feeding=True, checked=1000.0)
    assert not g.feed_check_due(1000.0 + g.FEED_RECHECK_INTERVAL + 1)
    assert g.feed_check_due(1000.0 + g.FEED_CHECK_INTERVAL)
    g._feed.update(feeding=False, checked=1000.0)
    assert g.feed_check_due(1000.0 + g.FEED_RECHECK_INTERVAL)


def test_an_account_without_an_airport_is_not_an_error(monkeypatch):
    def no_airport(endpoint, token):
        raise g.requests.HTTPError(response=_Reply({}, status=404))

    monkeypatch.setattr(g, "api_get", no_airport)
    assert g.fetch_config("k") is None

    monkeypatch.setattr(g, "api_get", lambda e, t: {
        "lat": 33.2, "lon": -97.2, "elevation_ft": None,
        "aircraft": [{"tail": "N1", "icao24": "A1B2C3"}, {"tail": "N2", "icao24": ""}],
    })
    assert g.fetch_config("k") == {"lat": 33.2, "lon": -97.2, "elevation": 0.0, "tracked": {"a1b2c3": "N1"}}


def test_a_stale_aircraft_file_is_not_used(monkeypatch, tmp_path):
    import json
    import time

    def refused(*a, **k):
        raise g.requests.ConnectionError("refused")

    monkeypatch.setattr(g.requests, "get", refused)
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    broken = tmp_path / "broken.json"
    old.write_text(json.dumps({"now": time.time() - 600, "aircraft": [{"hex": "dead00"}]}))
    new.write_text(json.dumps({"now": time.time(), "messages": 5, "aircraft": [{"hex": "a1b2c3"}]}))
    broken.write_text("{ not json")
    monkeypatch.setattr(g, "DUMP1090_FILE_PATHS", [str(broken), str(old), str(new)])
    assert g.fetch_dump1090() == [{"hex": "a1b2c3"}]


def test_receiver_location_falls_back_to_the_feed_clients(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "CONFIG_FILE", str(tmp_path / "missing.json"))
    conf = tmp_path / "adsblol"
    conf.write_text('INPUT="127.0.0.1:30005"\nLATITUDE="33.09"\nLONGITUDE="-97.25"\nUSER="0"\n')
    monkeypatch.setattr(g, "ADSBLOL_FEED_CONFIG", str(conf))
    assert g.load_receiver_location(None, None) == (33.09, -97.25, True)
    conf.write_text('LATITUDE="0"\nLONGITUDE="0"\n')
    assert g.load_receiver_location(None, None) == (None, None, False)
    assert g.load_receiver_location(33.2, -97.2) == (33.2, -97.2, False)
