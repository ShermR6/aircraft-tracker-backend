#!/usr/bin/env python3
"""
FinalPing Ground Station
Reads local ADS-B data from dump1090/readsb and pushes positions to the FinalPing cloud.

Setup:
  1. Create config.json in the same directory:
       { "token": "<your FinalPing API token>" }
     OR set the FINALPING_TOKEN environment variable.

  2. Install dependencies:
       pip3 install requests

  3. Run:
       python3 finalping_ground.py

  Dump1090 URL defaults to http://localhost:8080/data/aircraft.json
  Override with DUMP1090_URL environment variable.
"""

import json
import sys
import time
import os
import requests
from datetime import datetime
from math import radians, cos, sin, asin, sqrt, atan2, degrees

API_BASE = 'https://aircraft-tracker-backend-production.up.railway.app'
DUMP1090_URL = os.environ.get('DUMP1090_URL', 'http://localhost:8080/data/aircraft.json')
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

POLL_INTERVAL = 5        # seconds between position pushes
HEARTBEAT_INTERVAL = 60  # seconds between heartbeats
RANGE_PUSH_INTERVAL = 300  # seconds between SDR range updates


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
    token = os.environ.get('FINALPING_TOKEN')
    if token:
        return token
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f).get('token')
    return None


def api_get(endpoint, token):
    r = requests.get(
        f"{API_BASE}{endpoint}",
        headers={'Authorization': f'Bearer {token}'},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def api_post(endpoint, token, body):
    r = requests.post(
        f"{API_BASE}{endpoint}",
        json=body,
        headers={'Authorization': f'Bearer {token}'},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_dump1090():
    r = requests.get(DUMP1090_URL, timeout=5)
    r.raise_for_status()
    data = r.json()
    return data.get('aircraft', data.get('ac', []))


def run():
    token = load_token()
    if not token:
        log("[ERR] No token found. Create config.json with {\"token\": \"...\"} or set FINALPING_TOKEN.")
        sys.exit(1)

    log("Validating ground station access...")
    try:
        api_post('/api/ground/validate', token, {})
        log("[OK] Ground station access confirmed")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            log("[ERR] Ground station not enabled for this account.")
        else:
            log(f"[ERR] Validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"[ERR] Could not reach backend: {e}")
        sys.exit(1)

    log("Fetching config from backend...")
    try:
        config = api_get('/api/ground/config', token)
    except Exception as e:
        log(f"[ERR] Failed to fetch config: {e}")
        sys.exit(1)

    center_lat = float(config['lat'])
    center_lon = float(config['lon'])
    field_elevation = float(config.get('elevation_ft', 0))
    tracked = {
        a['icao24'].lower(): a['tail']
        for a in config.get('aircraft', [])
        if a.get('icao24')
    }

    log(f"[OK] Location: {center_lat:.4f}, {center_lon:.4f} | Elevation: {field_elevation:.0f}ft MSL")
    log(f"[OK] Tracking {len(tracked)} aircraft: {', '.join(tracked.values()) or 'none configured'}")
    log(f"[OK] Dump1090 URL: {DUMP1090_URL}")
    log("Ground station running...")

    # SDR range: 36 buckets, one per 10-degree bearing
    range_nm = [0.0] * 36
    last_heartbeat = 0.0
    last_range_push = 0.0

    while True:
        now = time.time()

        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                api_post('/api/ground/heartbeat', token, {})
                last_heartbeat = now
            except Exception as e:
                log(f"[WARN] Heartbeat failed: {e}")

        if now - last_range_push >= RANGE_PUSH_INTERVAL and any(v > 0 for v in range_nm):
            try:
                api_post('/api/ground/range', token, {'range_nm': range_nm})
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

        positions = {}
        for ac in aircraft_list:
            icao24 = ac.get('hex', '').lower()
            if icao24 not in tracked:
                continue

            lat = ac.get('lat')
            lon = ac.get('lon')
            if lat is None or lon is None:
                continue

            gs = ac.get('gs')
            alt_baro = ac.get('alt_baro')
            on_ground = (alt_baro == 'ground') or (gs is not None and gs < 50)
            altitude = field_elevation if on_ground else (float(alt_baro) if alt_baro and alt_baro != 'ground' else field_elevation)
            speed_kts = float(gs) if gs is not None else 0.0
            heading = float(ac.get('track') or 0)

            positions[icao24] = {
                'lat': lat,
                'lon': lon,
                'altitude': altitude,
                'speed': speed_kts,
                'heading': heading,
                'on_ground': on_ground,
                'updated_at': datetime.utcnow().isoformat(),
            }

            if not on_ground:
                dist = haversine_distance(center_lat, center_lon, lat, lon)
                bearing = haversine_bearing(center_lat, center_lon, lat, lon)
                bucket = int(bearing / 10) % 36
                if dist > range_nm[bucket]:
                    range_nm[bucket] = round(dist, 1)

        if positions:
            try:
                api_post('/api/ground/positions', token, {'positions': positions})
                parts = []
                for icao, p in positions.items():
                    tail = tracked[icao]
                    loc = "GND" if p['on_ground'] else f"{p['altitude']:.0f}ft"
                    parts.append(f"{tail} {loc}")
                log(f"[OK] {' | '.join(parts)}")
            except Exception as e:
                log(f"[WARN] Position push failed: {e}")
        else:
            log("No tracked aircraft in dump1090 feed")

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        log("Ground station stopped")
