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


def _feed_reset():
    g._feed.update(feeding=None, at=0.0, checked=0.0)


def test_feeding_is_what_adsblol_says(monkeypatch):
    _feed_reset()
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _Reply({"clients": {"beast": [{"kbps": 1}], "mlat": []}}))
    assert g.check_feed(1000.0) is True
    assert g.heartbeat_stats(g.START_TIME + 5, 0)["feeding"] is True
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _Reply({"clients": {"beast": [], "mlat": []}}))
    assert g.check_feed(2000.0) is False


def test_adsblol_not_answering_keeps_the_last_answer_for_an_hour(monkeypatch):
    _feed_reset()
    monkeypatch.setattr(g.requests, "get", lambda *a, **k: _Reply({"clients": {"beast": [{}]}}))
    assert g.check_feed(1000.0) is True

    def down(*a, **k):
        raise g.requests.ConnectionError("no route")

    monkeypatch.setattr(g.requests, "get", down)
    assert g.check_feed(1000.0 + 1800) is True
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
