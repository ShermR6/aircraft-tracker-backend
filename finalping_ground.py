#!/usr/bin/env python3
"""
FinalPing Ground Station
────────────────────────
Reads live ADS-B data from the decoder on this machine (readsb or dump1090)
and sends the positions of the account's aircraft to FinalPing. Signs in with
the account's device key: no email or password is stored here.

It also reports whether adsb.lol can see a feed from this receiver, which is
what the free plan for feeding is worked out from.

Data source priority: HTTP JSON API → aircraft.json on disk → SBS TCP stream (port 30003)

Setup, one command (the device key is on finalpingapp.com/app/ground-station):
  curl -fsSL https://finalpingapp.com/install.sh | sudo bash -s -- YOUR_DEVICE_KEY
Or by hand: put {"token": "YOUR_DEVICE_KEY"} in config.json next to this file.
"""

import json
import os
import socket
import sys
import threading
import time
import requests
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt, atan2, degrees

VERSION    = "3.9"
API_BASE   = "https://aircraft-tracker-backend-production.up.railway.app"
UPDATE_URL = "https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main"

DUMP1090_URL = os.environ.get("DUMP1090_URL", "http://localhost:8080/data/aircraft.json")
SBS_HOST     = os.environ.get("SBS_HOST", "localhost")
SBS_PORT     = int(os.environ.get("SBS_PORT", "30003"))
CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

POLL_INTERVAL      = 5
# While a tracked aircraft is being heard, positions go out every second, so
# the live map can follow it down to the runway and along the taxiways.
FAST_POLL_INTERVAL = 1
# A position dump1090 has not refreshed for this long is not sent: at one
# send a second it would otherwise go out as new, over and over.
MAX_POSITION_AGE_S = 10
# The per-send log line, at most this often (one a second would flood it).
POSITION_LOG_INTERVAL = 15
HEARTBEAT_INTERVAL = 60
RANGE_PUSH_INTERVAL = 300
# How often the account's airport and aircraft are fetched again, so an
# aircraft added in the app is listened for within minutes.
CONFIG_REFRESH_INTERVAL = 600
# ...and while the account has no airport yet (a receiver set up before the
# app was), so it starts working soon after one is added.
CONFIG_RETRY_NO_AIRPORT = 120
# An aircraft.json nothing has rewritten for this long belongs to a decoder
# that has stopped: its positions are not where anything is.
STALE_FILE_S = 60

# adsb.lol answers this with the feeds it has from the caller's own network.
ADSBLOL_ME_URL = "https://api.adsb.lol/0/me"
FEED_CHECK_INTERVAL = 600        # while feeding
FEED_RECHECK_INTERVAL = 120      # while not, so a new feed shows up quickly
# adsb.lol not answering is not the feed being down: the last answer stands
# for this long before it becomes "unknown".
FEED_ANSWER_KEPT_S = 3600
ADSBLOL_FEED_CONFIG = "/etc/default/adsblol"
# Consecutive heartbeat failures before we stop assuming it is the network
# and go re-check our credentials.
REVALIDATE_AFTER_FAILURES = 5
# A single bad decode can place an aircraft on the far side of the planet and
# stretch one bearing of the outline forever, so ignore implausible hits.
MAX_PLAUSIBLE_RANGE_NM = 300
RANGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "range_data.json")

DUMP1090_HTTP_URLS = [
    DUMP1090_URL,
    "http://localhost:8080/skyaware/data/aircraft.json",
    "http://localhost/skyaware/data/aircraft.json",
    "http://localhost:8888/data/aircraft.json",
    "http://127.0.0.1:8080/data/aircraft.json",
]

DUMP1090_FILE_PATHS = [
    "/run/dump1090-fa/aircraft.json",
    "/run/readsb/aircraft.json",
    "/run/dump1090/aircraft.json",
    # Written every second by the adsb.lol feed client from the same decoder.
    "/run/adsblol-feed/aircraft.json",
    "/var/run/dump1090-fa/aircraft.json",
    "/tmp/dump1090-fa/aircraft.json",
    "/home/pi/dump1090/aircraft.json",
]

# ── SBS stream state ──────────────────────────────────────────────────────────
_sbs_aircraft  = {}
_sbs_lock      = threading.Lock()
_sbs_connected = False

# ── Stats reported with the heartbeat ─────────────────────────────────────────
START_TIME  = time.time()
_msg_total  = None   # dump1090's running message counter, when the source has one
_sbs_lines  = 0      # lines read from the SBS stream, the fallback counter
_rate_mark  = None   # (time, counter) at the previous heartbeat


def _note_messages(data):
    """Remember dump1090's total message count from an aircraft.json payload."""
    global _msg_total
    m = data.get("messages") if isinstance(data, dict) else None
    if isinstance(m, (int, float)) and not isinstance(m, bool):
        _msg_total = float(m)


def message_rate(now):
    """Messages per second since the previous call, or None when unknown."""
    global _rate_mark
    if _msg_total is not None:
        counter = _msg_total
    elif _sbs_lines:
        counter = float(_sbs_lines)
    else:
        return None
    prev, _rate_mark = _rate_mark, (now, counter)
    if prev is None:
        return None
    dt, dn = now - prev[0], counter - prev[1]
    if dt <= 0 or dn < 0:      # clock step, or dump1090 restarted and reset its counter
        return None
    return round(dn / dt, 1)


# ── adsb.lol feed ─────────────────────────────────────────────────────────────
_feed = {"feeding": None, "at": 0.0, "checked": 0.0}


def check_feed(now):
    """Ask adsb.lol whether it has a feed from this network.

    True or False is adsb.lol's own answer. None means it could not be asked
    for over an hour, which the backend counts as neither.
    """
    _feed["checked"] = now
    try:
        r = requests.get(ADSBLOL_ME_URL, timeout=10)
        r.raise_for_status()
        clients = r.json().get("clients") or {}
        feeding = bool(clients.get("beast"))
        if feeding != _feed["feeding"]:
            log("[OK] adsb.lol is receiving this feed" if feeding
                else "[WARN] adsb.lol has no feed from this network yet")
        _feed["feeding"], _feed["at"] = feeding, now
    except Exception as e:
        if _feed["feeding"] is not None and now - _feed["at"] > FEED_ANSWER_KEPT_S:
            log(f"[WARN] Could not ask adsb.lol about the feed for an hour ({e})")
            _feed["feeding"] = None
    return _feed["feeding"]


def feed_check_due(now):
    wait = FEED_CHECK_INTERVAL if _feed["feeding"] else FEED_RECHECK_INTERVAL
    return now - _feed["checked"] >= wait


def heartbeat_stats(now, aircraft_count):
    return {
        "version": VERSION,
        "uptime_s": int(now - START_TIME),
        "msg_rate": message_rate(now),
        "aircraft_count": aircraft_count,
        "feeding": _feed["feeding"],
    }


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def haversine_distance(lat1, lon1, lat2, lon2):
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 3440.065 * 2 * asin(sqrt(a))


def haversine_bearing(lat1, lon1, lat2, lon2):
    lat1, lat2 = radians(lat1), radians(lat2)
    dlon = radians(lon2 - lon1)
    x = sin(dlon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon)
    return (degrees(atan2(x, y)) + 360) % 360


def load_token():
    token = os.environ.get("FINALPING_TOKEN")
    if token:
        return token
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f).get("token")
    return None


def api_get(endpoint, token):
    r = requests.get(f"{API_BASE}{endpoint}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def api_post(endpoint, token, body):
    r = requests.post(f"{API_BASE}{endpoint}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


# ── Auto-updater ──────────────────────────────────────────────────────────────

def _version_tuple(v):
    """(3, 6) from "3.6". None if it is not a version at all."""
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (ValueError, AttributeError):
        return None


def check_for_update():
    """Pull a newer agent, but never replace a working one with rubbish.

    The previous version wrote the download straight to disk and re-executed
    it without ever looking at the HTTP status. On 2026-07-30 one transient
    404 from the CDN was enough: the literal body "404: Not Found" was written
    over this file, os.execv restarted into it, and the station died with a
    SyntaxError on line 1. systemd dutifully restarted it every 10 seconds,
    each attempt failing identically, and because it never got far enough to
    send a heartbeat the backend just showed Offline. It stayed that way for
    three months.

    Three independent guards now. Any one of them would have caught it.
    """
    try:
        r = requests.get(f"{UPDATE_URL}/version.txt", timeout=5)
        if r.status_code != 200:
            return
        latest = r.text.strip()
        # A version is short and dotted. An error page, a captive-portal
        # redirect or a 404 body is not a version, so do not act on one.
        if not latest or len(latest) > 12 or not all(c.isdigit() or c == "." for c in latest):
            log(f"[UPDATE] Ignoring unexpected version payload ({latest[:40]!r})")
            return
        # Only ever move forward. Comparing for inequality meant a stale or
        # rolled-back version.txt downgraded every station that had already
        # updated -- observed live: a 3.6 agent "updated" itself to 3.5
        # because the published version file still said 3.5.
        latest_t, current_t = _version_tuple(latest), _version_tuple(VERSION)
        if latest_t is None or current_t is None or latest_t <= current_t:
            return

        log(f"[UPDATE] v{VERSION} -> v{latest} - downloading...")
        r2 = requests.get(f"{UPDATE_URL}/finalping_ground.py", timeout=30)

        # Guard 1: it must actually have been served.
        if r2.status_code != 200:
            log(f"[UPDATE] Download returned HTTP {r2.status_code} - keeping v{VERSION}")
            return
        body = r2.content.decode("utf-8", "replace")
        # Guard 2: it must look like this program.
        if len(body) < 4000 or "def run(" not in body:
            log(f"[UPDATE] Download is not the agent ({len(body)} bytes) - keeping v{VERSION}")
            return
        # Guard 3: it must parse, so we never exec into a SyntaxError.
        try:
            compile(body, "finalping_ground.py", "exec")
        except SyntaxError as e:
            log(f"[UPDATE] Download will not compile ({e}) - keeping v{VERSION}")
            return

        script_path = os.path.abspath(__file__)
        tmp_path = script_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp_path, script_path)
        log(f"[UPDATE] Updated to v{latest} - restarting...")
        os.execv(sys.executable, [sys.executable, script_path] + sys.argv[1:])
    except Exception as e:
        log(f"[UPDATE] Check failed: {e}")


# ── SBS reader (background thread) ───────────────────────────────────────────

def _parse_sbs_line(line):
    if not line.startswith("MSG"):
        return
    parts = line.split(",")
    if len(parts) < 16:
        return
    msg_type = parts[1].strip()
    hex_id   = parts[4].strip().lower()
    if not hex_id:
        return

    def field(i):
        return parts[i].strip() if i < len(parts) and parts[i].strip() else None

    with _sbs_lock:
        if hex_id not in _sbs_aircraft:
            _sbs_aircraft[hex_id] = {"hex": hex_id}
        ac = _sbs_aircraft[hex_id]
        ac["last_seen"] = time.time()

        try:
            if msg_type == "1":
                cs = field(10)
                if cs: ac["flight"] = cs

            elif msg_type == "2":
                if field(14) and field(15):
                    ac["lat"] = float(parts[14]); ac["lon"] = float(parts[15])
                    ac["pos_time"] = time.time()
                if field(12): ac["gs"]  = float(parts[12])
                if field(13): ac["track"] = float(parts[13])
                ac["alt_baro"] = "ground"
                ac["on_ground"] = True

            elif msg_type == "3":
                if field(11): ac["alt_baro"] = int(parts[11])
                if field(14) and field(15):
                    ac["lat"] = float(parts[14]); ac["lon"] = float(parts[15])
                    ac["pos_time"] = time.time()
                on_g = field(21) if len(parts) > 21 else None
                ac["on_ground"] = (on_g == "-1")

            elif msg_type == "4":
                if field(12): ac["gs"]    = float(parts[12])
                if field(13): ac["track"] = float(parts[13])
        except (ValueError, IndexError):
            pass


def _sbs_reader_thread():
    global _sbs_connected, _sbs_lines
    while True:
        try:
            with socket.create_connection((SBS_HOST, SBS_PORT), timeout=10) as sock:
                _sbs_connected = True
                log(f"[OK] SBS stream connected on port {SBS_PORT}")
                sock.settimeout(30)
                buf = ""
                while True:
                    chunk = sock.recv(4096).decode("utf-8", errors="replace")
                    if not chunk:
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        _sbs_lines += 1
                        _parse_sbs_line(line.strip())
        except Exception:
            pass
        _sbs_connected = False
        time.sleep(5)


def fetch_dump1090():
    for url in DUMP1090_HTTP_URLS:
        try:
            r = requests.get(url, timeout=3)
            r.raise_for_status()
            data = r.json()
            _note_messages(data)
            return data.get("aircraft", data.get("ac", []))
        except Exception:
            continue

    for path in DUMP1090_FILE_PATHS:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            stamp = data.get("now") if isinstance(data, dict) else None
            if isinstance(stamp, (int, float)) and time.time() - stamp > STALE_FILE_S:
                continue
            _note_messages(data)
            return data.get("aircraft", data.get("ac", []))

    if _sbs_connected:
        now = time.time()
        cutoff = now - 30
        out = []
        with _sbs_lock:
            for ac in _sbs_aircraft.values():
                if ac.get("last_seen", 0) <= cutoff:
                    continue
                a = dict(ac)
                # Any message keeps an aircraft listed, but only a position
                # message moves it: report how old the position is, as the
                # JSON output does, so a stale one is not sent as new.
                if a.get("pos_time"):
                    a["seen_pos"] = now - a["pos_time"]
                out.append(a)
        return out

    raise RuntimeError("dump1090 not reachable via HTTP, filesystem, or SBS stream")


# ── Main ──────────────────────────────────────────────────────────────────────

def _feed_client_location():
    """The location given to the adsb.lol feed client, when it was given a real one."""
    try:
        found = {}
        with open(ADSBLOL_FEED_CONFIG, encoding="utf-8") as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name in ("LATITUDE", "LONGITUDE"):
                    found[name] = float(value.strip().strip('"'))
        lat, lon = found["LATITUDE"], found["LONGITUDE"]
        if (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon
    except Exception:
        pass
    return None


def load_receiver_location(airport_lat, airport_lon):
    """Where the antenna is, which is not necessarily the airport.

    Range is a property of the receiver, so binning it around the field being
    watched shifts the whole outline by the distance between the two and
    distorts the shape. A station at home watching a field 7 nm away is the
    normal case, not a misconfiguration.

    Falls back to the location the adsb.lol feed client was given, then to the
    airport so an unconfigured station still produces something, but says so
    loudly, because the result is subtly wrong. (None, None, False) when there
    is nothing to go on at all: the account has no airport yet either.
    """
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        lat, lon = cfg.get("receiver_lat"), cfg.get("receiver_lon")
        if lat is not None and lon is not None:
            lat, lon = float(lat), float(lon)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon, True
    except Exception:
        pass
    fed = _feed_client_location()
    if fed:
        return fed[0], fed[1], True
    return airport_lat, airport_lon, False


def load_range():
    """Reception range accumulates over days, so it has to outlive a restart.

    Previously range_nm was initialised to zeros on every start, which meant a
    reboot, an update or a crash silently discarded however many weeks of
    coverage had been built up.
    """
    try:
        with open(RANGE_FILE, encoding="utf-8") as f:
            saved = json.load(f).get("range_nm")
        if isinstance(saved, list) and len(saved) == 36:
            return [float(v or 0.0) for v in saved]
    except Exception:
        pass
    return [0.0] * 36


def is_airborne(ac):
    """Treat an aircraft as airborne unless it says otherwise.

    Surface reports and very slow targets are excluded: they sit essentially on
    top of the receiver and say nothing about range.
    """
    gs_kt = ac.get("gs")
    return not (ac.get("on_ground", False)
                or ac.get("alt_baro") == "ground"
                or (gs_kt is not None and gs_kt < 50))


def bin_range(range_nm, aircraft_list, center_lat, center_lon):
    """Record the furthest airborne contact per 10-degree bearing, in place.

    Every aircraft with a position counts. The binning used to live below the
    `tracked` filter inside run(), so the outline only ever recorded where the
    handful of tracked aircraft happened to fly -- which is a map of their
    routes, not of what the antenna can receive.
    """
    for ac in aircraft_list:
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None or not is_airborne(ac):
            continue
        d = haversine_distance(center_lat, center_lon, lat, lon)
        # A single bad decode can place an aircraft on the far side of the
        # planet and stretch that bearing forever.
        if not (0 < d < MAX_PLAUSIBLE_RANGE_NM):
            continue
        bucket = int(haversine_bearing(center_lat, center_lon, lat, lon) / 10) % 36
        if d > range_nm[bucket]:
            range_nm[bucket] = round(d, 1)
    return range_nm


def save_range(range_nm):
    try:
        tmp = RANGE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"range_nm": range_nm,
                       "updated_at": datetime.utcnow().isoformat()}, f)
        os.replace(tmp, RANGE_FILE)
    except Exception as e:
        log(f"[WARN] Could not save range data: {e}")


def _is_auth_error(exc):
    """True when the backend actively rejected our credentials, as opposed to
    the network being down."""
    resp = getattr(exc, "response", None)
    return resp is not None and resp.status_code in (401, 403)


def wait_for_valid_token():
    """Block until the backend accepts our token, returning it.

    Retries with backoff rather than exiting: exiting made the service manager
    restart us every few seconds (a log flood on the backend). The token is
    re-read from disk every attempt, so repairing config.json is enough to
    recover, and check_for_update() runs each cycle so a fixed agent still
    lands while we wait.
    """
    backoff = 30
    while True:
        token = load_token()
        if token:
            try:
                api_post("/api/ground/validate", token, {})
                log("[OK] Ground station access confirmed")
                return token
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else None
                if code == 401:
                    log(f"[ERR] This device key was not accepted. If the key was replaced in the app, "
                        f"put the new one in config.json. Retrying in {backoff}s...")
                else:
                    log(f"[ERR] Validation failed (HTTP {code}). Retrying in {backoff}s...")
            except Exception as e:
                log(f"[ERR] Could not reach backend: {e}. Retrying in {backoff}s...")
        else:
            log(f"[ERR] No token found. Run setup or set FINALPING_TOKEN. Retrying in {backoff}s...")
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)
        check_for_update()


def fetch_config(token):
    """The account's airport and aircraft, or None while it has no airport.

    A receiver can be set up before the app is: it then feeds and reports in,
    and picks the airport up once one is added.
    """
    try:
        config = api_get("/api/ground/config", token)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    return {
        "lat": float(config["lat"]),
        "lon": float(config["lon"]),
        "elevation": float(config.get("elevation_ft") or 0),
        "tracked": {
            a["icao24"].lower(): a["tail"]
            for a in config.get("aircraft", []) if a.get("icao24")
        },
    }


def run():
    check_for_update()

    log("=" * 56)
    log(f"  FinalPing Ground Station v{VERSION}")
    log("=" * 56)

    log("Validating ground station access...")
    token = wait_for_valid_token()

    log("Fetching config from backend...")
    try:
        config = fetch_config(token)
    except Exception as e:
        log(f"[ERR] Failed to fetch config: {e}")
        sys.exit(1)

    center_lat      = config["lat"] if config else None
    center_lon      = config["lon"] if config else None
    field_elevation = config["elevation"] if config else 0.0
    tracked         = config["tracked"] if config else {}

    recv_lat, recv_lon, recv_configured = load_receiver_location(center_lat, center_lon)

    if config:
        log(f"[OK] Airport: {center_lat:.4f}, {center_lon:.4f} | Elevation: {field_elevation:.0f}ft MSL")
    else:
        log("[WARN] This account has no airport yet. Feeding and reporting in; "
            "add an airport and aircraft in the FinalPing app to start tracking.")
    if recv_configured and config:
        gap = haversine_distance(center_lat, center_lon, recv_lat, recv_lon)
        log(f"[OK] Receiver: {recv_lat:.4f}, {recv_lon:.4f} ({gap:.1f} nm from the airport)")
    elif recv_configured:
        log(f"[OK] Receiver: {recv_lat:.4f}, {recv_lon:.4f}")
    elif config:
        log("[WARN] No receiver_lat/receiver_lon in config.json - measuring range "
            "from the airport instead. If the antenna is somewhere else, the "
            "range outline will be offset by that distance.")
    log(f"[OK] Tracking {len(tracked)} aircraft: {', '.join(tracked.values()) or 'none configured'}")
    check_feed(time.time())

    t = threading.Thread(target=_sbs_reader_thread, daemon=True)
    t.start()

    source_label = None
    for u in DUMP1090_HTTP_URLS:
        try:
            if requests.get(u, timeout=2).status_code == 200:
                source_label = u
                break
        except Exception:
            continue
    if not source_label:
        source_label = next((p for p in DUMP1090_FILE_PATHS if os.path.exists(p)), None)
    if not source_label:
        source_label = f"SBS stream port {SBS_PORT}"
    log(f"[OK] Dump1090 source: {source_label}")
    log("Ground station running...")

    range_nm          = load_range()
    last_heartbeat    = 0.0
    heartbeat_failures = 0
    last_range_push   = 0.0
    last_update_check = time.time()
    last_config_refresh = time.time()
    last_aircraft_count = None
    last_position_log = 0.0

    while True:
        now = time.time()

        if now - last_update_check >= 3600:
            check_for_update()
            last_update_check = now

        if now - last_config_refresh >= (CONFIG_REFRESH_INTERVAL if config else CONFIG_RETRY_NO_AIRPORT):
            try:
                fresh = fetch_config(token)
                if fresh:
                    if not config:
                        log(f"[OK] Airport set: {fresh['lat']:.4f}, {fresh['lon']:.4f}")
                    if set(fresh["tracked"]) != set(tracked):
                        log(f"[OK] Now tracking {len(fresh['tracked'])} aircraft: "
                            f"{', '.join(fresh['tracked'].values()) or 'none configured'}")
                    config = fresh
                    center_lat, center_lon = config["lat"], config["lon"]
                    field_elevation, tracked = config["elevation"], config["tracked"]
                    if not recv_configured:
                        recv_lat, recv_lon = center_lat, center_lon
            except Exception as e:
                log(f"[WARN] Config refresh failed: {e}")
            last_config_refresh = now

        if feed_check_due(now):
            check_feed(now)

        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                # Stats ride along with the heartbeat. They must never be the
                # reason a heartbeat is not sent.
                try:
                    hb_body = heartbeat_stats(now, last_aircraft_count)
                except Exception:
                    hb_body = {}
                api_post("/api/ground/heartbeat", token, hb_body)
                last_heartbeat = now
                heartbeat_failures = 0
            except Exception as e:
                heartbeat_failures += 1
                log(f"[WARN] Heartbeat failed ({heartbeat_failures}): {e}")
                # Validation only ever ran at startup, so a token that expired
                # or was rotated while we were running produced this warning
                # once a minute forever: the Pi stayed up, dump1090 kept
                # streaming, and nothing ever reached the backend again. The
                # station just went quiet one day and only a reboot brought it
                # back. A rejected token will go on being rejected, so stop
                # guessing and re-validate — which also re-reads config.json,
                # so a repaired key recovers on its own.
                if _is_auth_error(e) or heartbeat_failures >= REVALIDATE_AFTER_FAILURES:
                    log("[WARN] Re-validating ground station access...")
                    token = wait_for_valid_token()
                    heartbeat_failures = 0
                    last_heartbeat = 0.0

        if recv_lat is not None and now - last_range_push >= RANGE_PUSH_INTERVAL and any(v > 0 for v in range_nm):
            try:
                api_post("/api/ground/range", token, {
                    "range_nm": range_nm,
                    "receiver_lat": recv_lat,
                    "receiver_lon": recv_lon,
                })
                save_range(range_nm)
                last_range_push = now
                log(f"[OK] Range updated (max {max(range_nm):.0f}nm)")
            except Exception as e:
                log(f"[WARN] Range push failed: {e}")

        try:
            aircraft_list = fetch_dump1090()
        except Exception as e:
            log(f"[WARN] dump1090 read failed: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        # Reception range is a property of the antenna, so it is measured from
        # every aircraft we can hear -- not just the tracked ones.
        if recv_lat is not None:
            bin_range(range_nm, aircraft_list, recv_lat, recv_lon)
        last_aircraft_count = sum(1 for ac in aircraft_list if ac.get("lat") is not None and ac.get("lon") is not None)

        positions = {}
        for ac in aircraft_list:
            icao24 = ac.get("hex", "").lower()
            lat = ac.get("lat")
            lon = ac.get("lon")

            if icao24 not in tracked:
                continue
            if lat is None or lon is None:
                continue

            # How old dump1090's position is: it keeps the last one for a
            # minute after the aircraft goes quiet.
            seen_pos = ac.get("seen_pos")
            age = float(seen_pos) if isinstance(seen_pos, (int, float)) else 0.0
            if age > MAX_POSITION_AGE_S:
                continue

            gs       = ac.get("gs")
            alt_baro = ac.get("alt_baro")
            decoder_ground = bool(ac.get("on_ground", False)) or (alt_baro == "ground")
            on_ground = decoder_ground or (gs is not None and gs < 50)
            altitude  = field_elevation if on_ground else (float(alt_baro) if alt_baro and alt_baro != "ground" else field_elevation)
            speed_kts = float(gs) if gs is not None else 0.0
            heading   = float(ac.get("track") or 0)

            positions[icao24] = {
                "lat": lat, "lon": lon,
                "altitude": altitude,
                "speed": speed_kts,
                "heading": heading,
                "on_ground": on_ground,
                # 3.8+: the decoder's own ground flag (not the speed guess)
                # and the altitude as reported, for the backend to use.
                "decoder_ground": decoder_ground,
                "alt_baro": alt_baro if isinstance(alt_baro, (int, float)) else None,
                "updated_at": (datetime.utcnow() - timedelta(seconds=age)).isoformat(),
            }

        quiet_log = now - last_position_log < POSITION_LOG_INTERVAL
        if positions:
            try:
                api_post("/api/ground/positions", token, {"positions": positions})
                if not quiet_log:
                    parts = []
                    for icao, p in positions.items():
                        tail = tracked[icao]
                        loc  = "GND" if p["on_ground"] else f"{p['altitude']:.0f}ft"
                        parts.append(f"{tail} {loc}")
                    log(f"[OK] {' | '.join(parts)}")
                    last_position_log = now
            except Exception as e:
                log(f"[WARN] Position push failed: {e}")
        elif not quiet_log:
            log("No tracked aircraft in dump1090 feed")
            last_position_log = now

        time.sleep(FAST_POLL_INTERVAL if positions else POLL_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("Ground station stopped")
