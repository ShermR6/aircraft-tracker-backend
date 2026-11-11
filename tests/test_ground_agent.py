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
