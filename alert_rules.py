"""Pure rules for who gets which alert.

Shared by main.py and tracker.py. Deliberately imports nothing from the app
(no database, no FastAPI) so it can be tested on its own, and so neither
module has to import the other to agree on what a plan includes.
"""

import re
from typing import Iterable, List, Optional

# ── Plan channels ────────────────────────────────────────────────────────────

# Channels a plan may CONNECT. main.TIER_LIMITS[tier]["channels"] points here.
PLAN_CHANNELS = {
    "starter":      ["discord", "email"],
    "premium":      ["discord", "email", "slack", "teams", "google_chat"],
    "pro":          ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"],
    "team-starter": ["discord", "email"],
    "team-premium": ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"],
    "team-pro":     ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"],
    # Internal, never sold.
    "owner":        ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"],
}

# The tracker used to keep a second, private list of channels it would deliver
# to. The two disagreed: a Pro user could connect Telegram, a webhook or Google
# Chat and the tracker silently never sent to them, and "owner" was missing so
# it fell back to starter. Delivery now follows PLAN_CHANNELS, plus the few
# channels only the old list had, so nobody stops receiving an alert they get
# today.
_LEGACY_SEND_EXTRAS = {
    "premium":  ["sms"],
    "pro":      ["whatsapp"],
    "team-pro": ["whatsapp"],
}


def send_channels_for_tier(tier: str) -> List[str]:
    """Channel types the tracker will deliver to for this plan."""
    base = PLAN_CHANNELS.get(tier, PLAN_CHANNELS["starter"])
    return base + [c for c in _LEGACY_SEND_EXTRAS.get(tier, []) if c not in base]


# ── Per-event routing ────────────────────────────────────────────────────────

_ALERT_TYPE_RE = re.compile(r"^(\d+(\.\d+)?nm|landing|takeoff)$")


def normalize_alert_type(alert_type) -> str:
    """ "10.0nm" -> "10nm". "2.5nm" and anything that is not a ring stay as is."""
    s = str(alert_type)
    if s.endswith("nm"):
        try:
            f = float(s[:-2])
        except ValueError:
            return s
        return f"{int(f)}nm" if f == int(f) else f"{f}nm"
    return s


def routed_integrations(integrations: Iterable, routing, alert_type) -> list:
    """Drop the integrations the user switched off for this alert type.

    `routing` is {"<alert_type>": [integration_id, ...]}. Anything malformed is
    treated as "no routing": a bad setting must never stop an alert going out.
    """
    integrations = list(integrations)
    if not isinstance(routing, dict) or not routing:
        return integrations
    off = routing.get(normalize_alert_type(alert_type))
    if off is None:
        off = routing.get(str(alert_type))
    if not isinstance(off, list) or not off:
        return integrations
    off_ids = {str(i) for i in off}
    return [i for i in integrations if str(i.id) not in off_ids]


def clean_routing(routing, valid_integration_ids: Iterable) -> dict:
    """Sanitise a routing map sent by a client: known alert types only, the
    caller's own integration ids only, no duplicates, no empty lists."""
    if not isinstance(routing, dict):
        return {}
    valid = {str(i) for i in valid_integration_ids}
    out = {}
    for key, ids in routing.items():
        if not isinstance(key, str) or not _ALERT_TYPE_RE.match(key) or not isinstance(ids, list):
            continue
        kept = []
        for i in ids:
            s = str(i)
            if s in valid and s not in kept:
                kept.append(s)
        if kept:
            out[normalize_alert_type(key)] = kept
    return out


def prune_routing(routing, valid_alert_types=None, remove_integration_id=None) -> dict:
    """Routing with a deleted integration and/or removed rings taken out."""
    if not isinstance(routing, dict):
        return {}
    out = {}
    for key, ids in routing.items():
        if valid_alert_types is not None and key not in valid_alert_types:
            continue
        if not isinstance(ids, list):
            continue
        kept = [str(i) for i in ids if remove_integration_id is None or str(i) != str(remove_integration_id)]
        if kept:
            out[key] = kept
    return out


# ── Approach filter ──────────────────────────────────────────────────────────

MAX_APPROACH_HEADINGS = 8


def clean_headings(value) -> Optional[List[float]]:
    """A list of runway headings (degrees true) from a client, or None."""
    if not isinstance(value, list):
        return None
    out: List[float] = []
    for v in value:
        if isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f or f < 0 or f > 360:   # f != f is NaN
            continue
        f = f % 360
        if f not in out:
            out.append(f)
    return out[:MAX_APPROACH_HEADINGS] or None


def reconcile_headings(payload: dict, current) -> Optional[List[float]]:
    """What approach_runway_headings should be after an airport-config save.

    The web app sends the list and, for older readers, the first entry as
    approach_runway_heading. The desktop app only knows the single heading: it
    either builds a payload without the list, or echoes back a config it
    fetched (list included) with the single heading changed. In both desktop
    cases the single heading must win, so the list is cleared.
    """
    if "approach_runway_headings" in payload:
        cleaned = clean_headings(payload.get("approach_runway_headings"))
        single = payload.get("approach_runway_heading")
        if cleaned and single is not None:
            try:
                if float(single) % 360 != cleaned[0]:
                    return None
            except (TypeError, ValueError):
                pass
        return cleaned
    if "approach_runway_heading" in payload:
        return None
    return current


def approach_headings(airspace: dict) -> List[float]:
    """Headings the approach filter accepts: the list when there is one,
    otherwise the single legacy heading, otherwise nothing."""
    headings = airspace.get("approach_runway_headings")
    if isinstance(headings, list) and headings:
        return [float(h) for h in headings]
    single = airspace.get("approach_runway_heading")
    return [float(single)] if single is not None else []


def heading_allowed(track, headings: Iterable[float], tolerance: float = 35.0) -> bool:
    """True when the aircraft's track is within `tolerance` of any heading."""
    t = float(track)
    return any(abs((t - h + 180) % 360 - 180) <= tolerance for h in headings)


# ── Receiver stats ───────────────────────────────────────────────────────────

def clean_gs_stats(body) -> Optional[dict]:
    """The stats a ground agent reported with its heartbeat, whitelisted and
    typed. None when there is nothing usable, which is what agents up to 3.6
    send, so their heartbeat leaves any stored stats alone."""
    if not isinstance(body, dict):
        return None
    out = {}
    version = body.get("version")
    if isinstance(version, str) and 0 < len(version) <= 12:
        out["version"] = version
    for key, is_float, cap in (("uptime_s", False, 10**9), ("msg_rate", True, 10**6), ("aircraft_count", False, 10**5)):
        x = body.get(key)
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            continue
        if x != x or x < 0 or x > cap:
            continue
        out[key] = round(float(x), 1) if is_float else int(x)
    return out or None
