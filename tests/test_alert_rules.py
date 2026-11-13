from types import SimpleNamespace

import alert_rules as r


def _ints(*ids):
    return [SimpleNamespace(id=i, type="discord") for i in ids]


# ── plan channels ────────────────────────────────────────────────────────────

def test_pro_sends_to_everything_it_can_connect():
    for ch in ("telegram", "webhook", "google_chat", "sms"):
        assert ch in r.send_channels_for_tier("pro")


def test_owner_is_not_treated_as_starter():
    assert "sms" in r.send_channels_for_tier("owner")


def test_nobody_loses_a_channel_the_tracker_used_to_send():
    legacy = {
        "starter": ["discord", "email"],
        "premium": ["discord", "email", "slack", "sms", "teams"],
        "pro": ["discord", "email", "slack", "sms", "teams", "whatsapp"],
    }
    for tier, channels in legacy.items():
        for ch in channels:
            assert ch in r.send_channels_for_tier(tier), (tier, ch)


def test_no_plan_gets_no_channels():
    assert r.send_channels_for_tier("free") == []


def test_the_three_old_plan_names_are_one_plan():
    assert r.send_channels_for_tier("starter") == r.ALL_CHANNELS
    assert r.PLAN_CHANNELS["premium"] == r.ALL_CHANNELS
    assert r.PLAN_CHANNELS["pro"] == r.ALL_CHANNELS


def test_unknown_personal_tier_gets_the_plan():
    # e.g. "starter-yearly" on a licence sold before tier names were normalised
    assert r.send_channels_for_tier("starter-yearly") == r.ALL_CHANNELS


# ── routing ──────────────────────────────────────────────────────────────────

def test_no_routing_sends_to_everyone():
    ints = _ints("a", "b")
    assert r.routed_integrations(ints, None, "10nm") == ints
    assert r.routed_integrations(ints, {}, "10nm") == ints


def test_routing_drops_only_the_listed_integration_for_that_event():
    ints = _ints("a", "b")
    routing = {"10nm": ["a"]}
    assert [i.id for i in r.routed_integrations(ints, routing, "10nm")] == ["b"]
    assert [i.id for i in r.routed_integrations(ints, routing, "landing")] == ["a", "b"]


def test_routing_matches_float_style_ring_keys():
    ints = _ints("a")
    assert r.routed_integrations(ints, {"10nm": ["a"]}, "10.0nm") == []


def test_malformed_routing_never_blocks_alerts():
    ints = _ints("a")
    assert r.routed_integrations(ints, "nonsense", "10nm") == ints
    assert r.routed_integrations(ints, {"10nm": "a"}, "10nm") == ints


def test_clean_routing_keeps_only_own_ids_and_valid_keys():
    cleaned = r.clean_routing(
        {"10.0nm": ["a", "zzz", "a"], "landing": [], "bogus": ["a"], 5: ["a"], "takeoff": ["b"]},
        ["a", "b"],
    )
    assert cleaned == {"10nm": ["a"], "takeoff": ["b"]}


def test_clean_routing_rejects_non_dict():
    assert r.clean_routing(["a"], ["a"]) == {}


def test_prune_routing_removes_an_integration_and_dead_rings():
    routing = {"10nm": ["a", "b"], "5nm": ["a"], "landing": ["a"]}
    assert r.prune_routing(routing, remove_integration_id="a") == {"10nm": ["b"]}
    assert r.prune_routing(routing, valid_alert_types={"10nm", "landing", "takeoff"}) == {
        "10nm": ["a", "b"], "landing": ["a"],
    }


# ── headings ─────────────────────────────────────────────────────────────────

def test_single_heading_behaves_as_before():
    airspace = {"approach_runway_heading": 180.0}
    hs = r.approach_headings(airspace)
    assert hs == [180.0]
    assert r.heading_allowed(182, hs)
    assert r.heading_allowed(215, hs)
    assert not r.heading_allowed(216, hs)


def test_list_of_headings_allows_any_of_them():
    hs = r.approach_headings({"approach_runway_heading": 180.0, "approach_runway_headings": [180.0, 0.0]})
    assert hs == [180.0, 0.0]
    assert r.heading_allowed(355, hs)
    assert r.heading_allowed(170, hs)
    assert not r.heading_allowed(90, hs)


def test_no_heading_configured_means_no_filter_list():
    assert r.approach_headings({}) == []
    assert r.approach_headings({"approach_runway_headings": []}) == []


def test_clean_headings():
    assert r.clean_headings([180, "0", 360, 999, "x", True, 180]) == [180.0, 0.0]
    assert r.clean_headings([]) is None
    assert r.clean_headings("180") is None


def test_reconcile_web_payload_keeps_list():
    assert r.reconcile_headings(
        {"approach_runway_headings": [180, 0], "approach_runway_heading": 180}, None
    ) == [180.0, 0.0]


def test_reconcile_desktop_payload_clears_list():
    # Desktop builds its own payload: single heading only.
    assert r.reconcile_headings({"approach_runway_heading": 0}, [180.0, 0.0]) is None
    # Desktop echoes a fetched config back but changed the single heading.
    assert r.reconcile_headings(
        {"approach_runway_headings": [180.0, 0.0], "approach_runway_heading": 0}, [180.0, 0.0]
    ) is None


def test_reconcile_untouched_payload_keeps_current():
    assert r.reconcile_headings({"alert_distances_nm": [10]}, [180.0]) == [180.0]
    # Desktop echoing the fetched config unchanged keeps the web app's list.
    assert r.reconcile_headings(
        {"approach_runway_headings": [180.0, 0.0], "approach_runway_heading": 180.0}, [180.0, 0.0]
    ) == [180.0, 0.0]


# ── receiver stats ───────────────────────────────────────────────────────────

def test_old_agents_send_nothing_and_change_nothing():
    assert r.clean_gs_stats({}) is None
    assert r.clean_gs_stats(None) is None
    assert r.clean_gs_stats("x") is None


def test_stats_are_whitelisted_and_typed():
    assert r.clean_gs_stats({
        "version": "3.7", "uptime_s": 3600.9, "msg_rate": 84.26, "aircraft_count": 12,
        "evil": "<script>", "extra": 1,
    }) == {"version": "3.7", "uptime_s": 3600, "msg_rate": 84.3, "aircraft_count": 12}


def test_stats_drop_bad_values_individually():
    assert r.clean_gs_stats({
        "version": "x" * 50, "uptime_s": -1, "msg_rate": None, "aircraft_count": True,
    }) is None
    assert r.clean_gs_stats({"uptime_s": 5, "msg_rate": float("nan")}) == {"uptime_s": 5}
