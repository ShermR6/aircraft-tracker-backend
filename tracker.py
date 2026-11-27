"""
Cloud Aircraft Tracker
Tracks aircraft for ALL users in a centralized cloud service
Adapted from your working KDTO tracker code
"""

import asyncio
import os
import aiohttp
import html as _html
import re
import ipaddress
import socket
from urllib.parse import urlparse
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, asin
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from models import User, Aircraft, AirportConfig, AlertSetting, Integration, NotificationLog, Team, TeamChannel, TeamAlertSetting, TeamMember, TeamAirportConfig, AircraftClaim, EscalationConfig, AlertEscalation, TeamShift, TeamShiftMember, TeamDutyOverride, ExpectedArrival
from database import SessionLocal
import ground_state as _gs
import flight_trails
import flight_history
import coverage
import heard
from alert_rules import send_channels_for_tier, routed_integrations, approach_headings, heading_allowed
from models import read_optional_column
from plans import live_license, plan_tier, sms_over_limit, tracked_aircraft_limit


def _is_safe_public_url(url: str) -> bool:
    """SSRF guard for user-supplied webhook URLs. Requires http(s) and rejects
    any host that resolves to a private/loopback/link-local/reserved address
    (e.g. 169.254.169.254 cloud metadata, 10.x/192.168.x internal services)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


from functools import lru_cache

# Alerts: each ring is armed again once the aircraft is this far outside it.
REARM_OUTSIDE_NM = 3.0
REARM_OUTSIDE_FACTOR = 1.6
# Rings of this size and up fire only for an aircraft heading roughly at the
# field (within INBOUND_TRACK_DEG of the bearing to it). Smaller rings are
# crossed on base and final turns, which point elsewhere.
INBOUND_CHECK_FROM_NM = 5.0
INBOUND_TRACK_DEG = 75.0


def rearm_at(ring_nm: float) -> float:
    """How far out an aircraft must be seen before a ring can alert again:
    well outside it, so working an area near a ring, or a wide pattern,
    does not repeat its alert."""
    return max(ring_nm + REARM_OUTSIDE_NM, ring_nm * REARM_OUTSIDE_FACTOR)


def bearing_to(lat1, lon1, lat2, lon2) -> float:
    """Initial bearing in degrees from point 1 to point 2."""
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dl = radians(float(lon2) - float(lon1))
    x = sin(dl) * cos(p2)
    y = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
    from math import atan2, degrees
    return (degrees(atan2(x, y)) + 360) % 360
# ...and every ring is armed again after an aircraft has gone unseen this long.
REARM_AFTER_MINUTES = 30
# A landing or takeoff already logged within this window is not sent again.
# One minute, like the old per-alert cooldown: no pattern lap is that quick,
# so every lap still reports its landing and takeoff.
REPEAT_WINDOW_MINUTES = 1
# A landing is assumed for an aircraft lost from sight only when it was last
# seen this close and this low, and has then been missing for this many polls.
LOST_LANDING_NM = 2.0
LOST_LANDING_AGL_FT = 800
LOST_LANDING_POLLS = 3
# On the live map, an aircraft last heard this close and this low, and then
# not heard for this long, is shown as on the ground with no position: it has
# landed below coverage. Looser than the alert rule above, which has to be
# sure within 90 seconds; this one only decides what the map shows.
LANDED_UNSEEN_NM = 3.0
LANDED_UNSEEN_AGL_FT = 1200
LANDED_UNSEEN_SECONDS = 300
# Once the alert rule above has called the landing, the map agrees this soon.
LANDED_CALLED_SECONDS = 60
# An aircraft called on the ground this far from the field's centre, still
# this fast, was called from a fix on final (see get_live_aircraft).
OFF_FIELD_NM = 1.0
ON_FINAL_KT = 40
# adsb.lol decides landings and takeoffs while its fix is at most this old.
ADSB_FRESH_SECONDS = 20
# adsb.lol allows the backend 500 requests a day (x-ratelimit-limit
# "500, 500;w=86400", since 27 Nov 2026). FlyItalyADSB is asked every cycle;
# adsb.lol only as the backup (see CloudAircraftTracker.fetch_network), at
# most this many times in any 24 hours and once a minute.
ADSB_DAILY_BUDGET = 450
ADSB_BACKUP_EVERY_SECONDS = 60
# An aircraft is worth a backup request while it was heard airborne within
# this distance of its field this recently, and FlyItalyADSB has nothing
# newer than ADSB_FRESH_SECONDS for it.
BACKUP_NEAR_NM = 15
BACKUP_RECENT_SECONDS = 10 * 60
# How each adsb.lol request is tried: see fetch_adsb.
ADSB_TRY_SECONDS = 9
ADSB_BUDGET_SECONDS = 24
ADSB_MAX_TRIES = 3
ADSB_RETRY_PAUSE_SECONDS = 1

# FlyItalyADSB, the second network source (2026-11-24). Commercial use is
# allowed on every plan, Free included, with credit "FlyItalyADSB
# (flyitalyadsb.com), CC BY-SA 4.0" wherever the data is shown; not for
# safety of life, ATC or navigation. Up to 50 addresses per request, and a
# multi-address request counts as one. Free: 30 requests a minute, 300,000 a
# month. No key, no requests. Never tick the feeder perk for this key: it is
# for non-commercial use only.
FLYITALY_URL = "https://api.flyitalyadsb.com/v2/icao/"
FLYITALY_API_KEY = os.getenv("FLYITALY_API_KEY", "").strip()
FLYITALY_BATCH = 50
FLYITALY_TRY_SECONDS = 8


_last_refusal_log: Dict[str, float] = {}


def _seen_pos(ac: dict) -> float:
    v = ac.get('seen_pos') if isinstance(ac, dict) else None
    return float(v) if isinstance(v, (int, float)) else float('inf')


def _merge_sources(adsb: dict, fia: dict) -> dict:
    """One record per aircraft: whichever source heard its position most
    recently. Each record is marked with where it came from."""
    merged = {}
    for icao in set(adsb) | set(fia):
        a, f = adsb.get(icao), fia.get(icao)
        if a is not None and (f is None or _seen_pos(a) <= _seen_pos(f)):
            merged[icao] = dict(a, _src='adsb.lol')
        else:
            merged[icao] = dict(f, _src='FlyItalyADSB')
    return merged


def adsb_refused_for(headers) -> float:
    """After a 429, how long to leave adsb.lol alone: until its allowance
    resets (x-ratelimit-reset, seconds), at least a minute and at most a day."""
    try:
        reset = float((headers or {}).get("x-ratelimit-reset") or (headers or {}).get("Retry-After") or 300)
    except (TypeError, ValueError):
        reset = 300.0
    return min(max(reset, 60.0), 86400.0)


async def _log_refusal(response, source: str = "adsb.lol") -> None:
    """What a source says when it refuses (429, 403...), at most every 10
    minutes per source: its message and any rate-limit headers. Never raises."""
    import time as _time
    if _time.time() - _last_refusal_log.get(source, 0.0) < 600:
        return
    _last_refusal_log[source] = _time.time()
    try:
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() in ("retry-after", "server", "cf-ray", "via", "date")
                   or "ratelimit" in k.lower() or "rate-limit" in k.lower()}
        body = " ".join((await response.text())[:300].split())
        print(f"{source} refused HTTP {response.status}: headers {headers} body {body!r}")
    except Exception as e:
        print(f"{source} refused HTTP {response.status} (could not read why: {type(e).__name__})")


def _observe_coverage(owner, icao24, tail, airspace, adsb, ground_fix, adsb_ok, receiver_up, now):
    """Hands this cycle's positions to the coverage-gap log. Never raises."""
    try:
        positions = []
        if adsb is not None and isinstance(adsb.get('seen_pos'), (int, float)):
            alt = adsb.get('alt_baro')
            positions.append(coverage.position(
                'adsb', adsb.get('lat'), adsb.get('lon'), now - timedelta(seconds=float(adsb['seen_pos'])),
                alt_ft=alt if isinstance(alt, (int, float)) else None, on_ground=alt == 'ground',
                kind=str(adsb.get('type') or 'adsb')[:16]))
        if ground_fix is not None:
            try:
                at = datetime.fromisoformat(str(ground_fix.get('updated_at')).replace('Z', '')) if ground_fix.get('updated_at') else now
            except ValueError:
                at = now
            positions.append(coverage.position(
                'receiver', ground_fix.get('lat'), ground_fix.get('lon'), at,
                alt_ft=ground_fix.get('altitude'), on_ground=bool(ground_fix.get('on_ground')), kind='receiver'))
        coverage.observe(owner, icao24, tail, airspace, positions, adsb_ok, receiver_up, now)
    except Exception as e:
        print(f"Coverage check skipped for {tail}: {type(e).__name__}: {e}")


def _fresh_ground_fix(fix) -> Optional[dict]:
    """A position from the account's receiver, if it is usable and under 30 s
    old. The receiver resends its whole list every few seconds, so a fix that
    has not been refreshed is an aircraft it can no longer hear."""
    if not isinstance(fix, dict) or fix.get('lat') is None or fix.get('lon') is None:
        return None
    stamp = fix.get('updated_at')
    if stamp:
        try:
            age = (datetime.utcnow() - datetime.fromisoformat(str(stamp).replace('Z', ''))).total_seconds()
        except ValueError:
            return None
        if age > _gs.GROUND_POSITION_MAX_AGE_SECONDS:
            return None
    return fix


def _save_flight_trails() -> None:
    """Flight paths for the Live Map go to the database each cycle, so a
    restart keeps them. A failure here never stops tracking."""
    try:
        db = SessionLocal()
        try:
            flight_trails.save(db)
        finally:
            db.close()
    except Exception as e:
        print(f"Flight trails not saved: {type(e).__name__}: {e}")
    try:
        db = SessionLocal()
        try:
            flight_history.save(db)
        finally:
            db.close()
    except Exception as e:
        print(f"Flight history not saved: {type(e).__name__}: {e}")


def _aircraft_from_adsb(icao24: str, tail: str, ac: dict, airspace: dict) -> dict:
    """An adsb.lol aircraft in the shape check_and_notify takes, with the
    ground detection the tracker has always used for it."""
    alt_baro = ac.get('alt_baro')
    gs = ac.get('gs')
    seen_pos = ac.get('seen_pos')  # seconds since last position update

    # Ground detection: multiple signals
    is_on_ground = alt_baro == 'ground'

    if not is_on_ground and alt_baro is not None and alt_baro != 'ground':
        field_elev = float(airspace.get('field_elevation_ft_msl', 0) or 0)
        alt_agl = float(alt_baro) - field_elev if isinstance(alt_baro, (int, float)) else 999

        # On ground if: altitude within 150ft of field AND speed under 50kts (or no speed reported)
        if alt_agl < 150 and (gs is None or gs < 50):
            is_on_ground = True

        # On ground if: very close to airport, low altitude, and stale position data (>30s)
        ac_lat = ac.get('lat')
        ac_lon = ac.get('lon')
        if ac_lat and ac_lon and seen_pos is not None and seen_pos > 30:
            center_lat = float(airspace['center_lat'])
            center_lon = float(airspace['center_lon'])
            dlat = abs(float(ac_lat) - center_lat) * 60
            dlon = abs(float(ac_lon) - center_lon) * 60 * 0.85  # rough cos correction
            approx_dist = (dlat**2 + dlon**2) ** 0.5
            if approx_dist < 3 and alt_agl < 500:
                is_on_ground = True

    return {
        'icao24': icao24,
        'callsign': tail,
        'latitude': ac.get('lat'),
        'longitude': ac.get('lon'),
        'baro_altitude': alt_baro,
        'on_ground': is_on_ground,
        'velocity': gs,
        'heading': ac.get('track'),
        'source': 'adsb',
    }


def _aircraft_from_ground(icao24: str, tail: str, fix: dict) -> dict:
    """A receiver position in the shape check_and_notify takes. The receiver
    marks an aircraft on the ground when the decoder says so or when it is
    under 50 kt (the same speed adsb.lol fixes are judged by), and then reports
    field elevation instead of its altitude."""
    on_ground = bool(fix.get('on_ground'))
    heading = fix.get('heading')
    return {
        'icao24': icao24,
        'callsign': tail,
        'latitude': fix.get('lat'),
        'longitude': fix.get('lon'),
        'baro_altitude': None if on_ground else fix.get('altitude'),
        'on_ground': on_ground,
        'velocity': fix.get('speed'),
        # The receiver sends 0 when it has no track; that is not a heading.
        'heading': heading if heading else None,
        'source': 'ground',
    }
try:
    from timezonefinder import TimezoneFinder
    _tf = TimezoneFinder()
except Exception:
    _tf = None


@lru_cache(maxsize=1024)
def _tz_name_for(lat, lon):
    """IANA timezone name for a coordinate (cached). Used so quiet hours are
    evaluated in the location's local time rather than the server's UTC."""
    if _tf is None or lat is None or lon is None:
        return None
    try:
        return _tf.timezone_at(lat=float(lat), lng=float(lon))
    except Exception:
        return None


class UserTracker:
    """Tracks aircraft for a single user"""

    def __init__(self, user_id: str, config: dict, aircraft_list: List[dict]):
        self.user_id = user_id
        self.config = config
        self.aircraft_to_track = {a['icao24']: a['tail_number'] for a in aircraft_list if a.get('icao24')}
        self.aircraft_alert_distances = {a['icao24']: a.get('alert_distances') for a in aircraft_list if a.get('icao24')}

        # State tracking
        self.aircraft_state = {}
        self.distance_alerts_sent = {}
        # Rings crossed while the approach filter said "not lined up yet". They
        # fire later, once the aircraft lines up while still inside the ring.
        self.pending_alerts = {}
        self.last_notifications = {}
        # Track last date SMS/WhatsApp opt-out was appended (once per day)
        self.sms_stop_last_sent_date = None
        self.whatsapp_stop_last_sent_date = None

    def adopt_state(self, old: "UserTracker"):
        """Carry alert state over from the tracker this one replaces.

        The tracker is rebuilt whenever an aircraft or the airport settings are
        saved. Starting from nothing lost the previous distance of every
        aircraft in flight, so a ring crossed in that cycle never fired, and
        forgot which rings had fired, so they could fire twice.
        """
        keep = set(self.aircraft_to_track)
        for name in ("aircraft_state", "distance_alerts_sent", "pending_alerts", "last_notifications"):
            old_map = getattr(old, name, {}) or {}
            if name == "last_notifications":
                setattr(self, name, {k: v for k, v in old_map.items() if k.split("_", 1)[0] in keep})
            else:
                setattr(self, name, {k: v for k, v in old_map.items() if k in keep})
        self.sms_stop_last_sent_date = old.sms_stop_last_sent_date
        self.whatsapp_stop_last_sent_date = old.whatsapp_stop_last_sent_date

    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points in nautical miles"""
        lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        nm = 3440.065 * c
        return nm

    def in_quiet_hours(self) -> bool:
        qh = self.config.get('quiet_hours', {})
        if not qh.get('enabled', False):
            return False
        start_str = qh.get('start', '23:00')
        end_str = qh.get('end', '06:00')
        tz_name = self.config.get('timezone')
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                from datetime import timezone as _utc
                now_str = datetime.now(_utc.utc).astimezone(ZoneInfo(tz_name)).strftime('%H:%M')
            except Exception:
                now_str = datetime.utcnow().strftime('%H:%M')
        else:
            now_str = datetime.utcnow().strftime('%H:%M')
        if start_str <= end_str:
            return start_str <= now_str <= end_str
        else:
            # Overnight span e.g. 23:00 → 06:00
            return now_str >= start_str or now_str <= end_str

    def should_notify(self, event_type: str, aircraft_id: str) -> bool:
        """Check if enough time has passed since last notification (cooldown)"""
        cooldown_minutes = self.config.get('notification_cooldown_minutes', 1)
        key = f"{aircraft_id}_{event_type}"

        if key in self.last_notifications:
            time_since_last = datetime.now() - self.last_notifications[key]
            if time_since_last < timedelta(minutes=cooldown_minutes):
                return False

        self.last_notifications[key] = datetime.now()
        return True

    async def check_and_notify(self, aircraft_data: dict) -> List[dict]:
        """
        Check aircraft state and determine which notifications to send
        Returns list of notifications to send
        """
        notifications = []

        aircraft_id = aircraft_data['icao24']
        callsign = aircraft_data['callsign']
        on_ground = aircraft_data['on_ground']

        # Calculate distance
        if aircraft_data['latitude'] is None or aircraft_data['longitude'] is None:
            return notifications

        airspace = self.config['airspace']
        distance_nm = self.haversine_distance(
            airspace['center_lat'], airspace['center_lon'],
            aircraft_data['latitude'], aircraft_data['longitude']
        )

        in_horizontal = distance_nm <= float(airspace['radius_nm'])

        # Check altitude — adsb.lol returns alt_baro already in feet
        altitude_msl_ft_raw = aircraft_data['baro_altitude']
        field_elev = float(airspace['field_elevation_ft_msl']) if airspace['field_elevation_ft_msl'] else 0
        if on_ground or altitude_msl_ft_raw is None:
            altitude_agl_ft = 0
            altitude_msl_ft = field_elev
            in_vertical = on_ground
        else:
            altitude_msl_ft = float(altitude_msl_ft_raw)
            altitude_agl_ft = max(0, altitude_msl_ft - field_elev)
            in_vertical = airspace['floor_ft_agl'] <= altitude_agl_ft <= airspace['ceiling_ft_agl']

        in_airspace = in_horizontal and in_vertical

        # Not seen for a long time (out of coverage, parked, switched off): this
        # is a new flight as far as alerts go, so every ring is armed again.
        last_update = self.aircraft_state.get(aircraft_id, {}).get('last_update')
        if last_update is not None and datetime.now() - last_update > timedelta(minutes=REARM_AFTER_MINUTES):
            self.distance_alerts_sent[aircraft_id] = set()
            self.pending_alerts[aircraft_id] = set()

        # Get previous state
        was_in_airspace = self.aircraft_state.get(aircraft_id, {}).get('in_airspace', False)
        was_on_ground = self.aircraft_state.get(aircraft_id, {}).get('on_ground', None)

        # Distance alerts (approaching only) - SEQUENTIAL ZONE CROSSING
        if not on_ground:
            per_aircraft = self.aircraft_alert_distances.get(aircraft_id)
            alert_distances = sorted(
                per_aircraft if per_aircraft else self.config['airspace'].get('alert_distances_nm', [10.0, 5.0, 2.0]),
                reverse=True
            )

            if aircraft_id not in self.distance_alerts_sent:
                self.distance_alerts_sent[aircraft_id] = set()
            pending = self.pending_alerts.setdefault(aircraft_id, set())

            prev_distance = self.aircraft_state.get(aircraft_id, {}).get('last_distance', None)
            max_distance = self.aircraft_state.get(aircraft_id, {}).get('max_distance', None)

            # Track the maximum (farthest) distance
            if max_distance is None or distance_nm > max_distance:
                max_distance = distance_nm

            # Normalize distance float to consistent key e.g. 10.0 -> "10nm", 2.5 -> "2.5nm"
            def dist_key(d):
                return f"{int(d) if d == int(d) else d}nm"

            # Smallest configured distance triggers landing detection
            min_distance = min(alert_distances) if alert_distances else 2.0

            ALERT_BUFFER_NM = 0.9
            if prev_distance is not None:
                for alert_distance in alert_distances:
                    alert_key = dist_key(alert_distance)

                    # Fire when crossing INTO this alert's zone (threshold extended by buffer).
                    # Each alert only fires for its own crossing — inner alerts don't fire for outer zones.
                    crossed_with_buffer = (
                        prev_distance > (alert_distance + ALERT_BUFFER_NM) and
                        distance_nm <= (alert_distance + ALERT_BUFFER_NM)
                    )
                    # A ring crossed earlier but held back by the approach
                    # filter, and the aircraft is still inside it.
                    still_waiting = alert_key in pending and distance_nm <= (alert_distance + ALERT_BUFFER_NM)

                    if (crossed_with_buffer or still_waiting) and alert_key not in self.distance_alerts_sent[aircraft_id]:
                        # Bigger rings only for an aircraft heading at the field:
                        # crossing one while working an area sideways to it is
                        # not an arrival. Held, like the approach filter below,
                        # so it fires if the aircraft turns in while inside.
                        track = aircraft_data.get('heading')
                        if alert_distance >= INBOUND_CHECK_FROM_NM and track is not None:
                            to_field = bearing_to(aircraft_data['latitude'], aircraft_data['longitude'],
                                                  airspace['center_lat'], airspace['center_lon'])
                            if abs((float(track) - to_field + 180) % 360 - 180) > INBOUND_TRACK_DEG:
                                pending.add(alert_key)
                                continue

                        # Approach corridor filter: not lined up with a runway
                        # yet. Hold the alert rather than drop it, so it fires
                        # if the aircraft lines up while still inside the ring.
                        rwy_headings = approach_headings(airspace) if airspace.get('approach_corridor_enabled') else []
                        if rwy_headings:
                            track = aircraft_data.get('track') or aircraft_data.get('heading')
                            if track is not None and not heading_allowed(track, rwy_headings):
                                pending.add(alert_key)
                                continue
                        pending.discard(alert_key)

                        # Send the distance alert
                        if self.should_notify(f'distance_{alert_distance}', aircraft_id):
                            speed_kts = aircraft_data.get('velocity')
                            if speed_kts and speed_kts > 30:
                                eta_minutes = max(1, int((distance_nm / speed_kts) * 60)) + 2
                            else:
                                eta_minutes = max(1, int(distance_nm / 1.5)) + 2
                            notifications.append({
                                'type': alert_key,
                                'tail': callsign,
                                'distance': distance_nm,
                                'altitude': altitude_msl_ft,
                                'eta': eta_minutes,
                                'speed': speed_kts or 0,
                                'time': datetime.now()
                            })
                            self.distance_alerts_sent[aircraft_id].add(alert_key)

                        # Mark as ready for landing detection once smallest distance is crossed sequentially
                        if alert_distance == min_distance:
                            larger_distances = [d for d in alert_distances if d > min_distance]
                            all_crossed = all(dist_key(d) in self.distance_alerts_sent[aircraft_id] for d in larger_distances)
                            if larger_distances and all_crossed:
                                self.aircraft_state.setdefault(aircraft_id, {})['landing_ready'] = True

            # Each ring is armed again once the aircraft is seen 2 nm outside it.
            # Only re-arming them all beyond the largest ring missed every
            # alert on the next arrival after a local flight that stayed
            # inside it (out to 12 nm and back, with a 15 nm ring).
            for d in alert_distances:
                if distance_nm > rearm_at(d):
                    self.distance_alerts_sent[aircraft_id].discard(dist_key(d))
                    pending.discard(dist_key(d))

            reset_distance = rearm_at(max(alert_distances)) if alert_distances else 12.0
            if distance_nm > reset_distance:
                if aircraft_id in self.aircraft_state:
                    self.aircraft_state[aircraft_id]['max_distance'] = distance_nm
                    self.aircraft_state[aircraft_id]['landed'] = False

            if aircraft_id not in self.aircraft_state:
                self.aircraft_state[aircraft_id] = {}
            self.aircraft_state[aircraft_id]['last_distance'] = distance_nm
            self.aircraft_state[aircraft_id]['max_distance'] = max_distance


        # Takeoff: was on ground, now airborne.
        # Guard against stale on_ground flags from the API — if the flag flips while
        # the aircraft is already well above field elevation, it's not a real takeoff event.
        if was_on_ground is True and on_ground is False and altitude_agl_ft <= 1500:
            # A departure is not an arrival: the rings it took off inside stay
            # quiet until it has been well outside them again. Without this, a
            # pattern or a turn back after take-off "crossed 2 nm".
            rings = self.aircraft_alert_distances.get(aircraft_id) or airspace.get('alert_distances_nm', [10.0, 5.0, 2.0])
            sent = self.distance_alerts_sent.setdefault(aircraft_id, set())
            for d in rings:
                d = float(d)
                if distance_nm <= d + 0.9:
                    sent.add(f"{int(d) if d == int(d) else d}nm")
                    self.pending_alerts.get(aircraft_id, set()).discard(f"{int(d) if d == int(d) else d}nm")
            if self.should_notify('takeoff', aircraft_id):
                notifications.append({
                    'type': 'takeoff',
                    'tail': callsign,
                    'distance': distance_nm,
                    'altitude': altitude_msl_ft,
                    'speed': aircraft_data.get('velocity') or 0,
                    'time': datetime.now(),
                })
            self.aircraft_state[aircraft_id]['landed'] = False

        # Landing: was airborne, now on ground, within 15nm
        if was_on_ground is False and on_ground and distance_nm < 15.0:
            if self.should_notify('landing', aircraft_id):
                notifications.append({
                    'type': 'landing',
                    'tail': callsign,
                    'distance': distance_nm,
                    'altitude': altitude_msl_ft,
                    'heading': aircraft_data.get('heading') or aircraft_data.get('track') or 0,
                    'time': datetime.now(),
                })
                if aircraft_id not in self.aircraft_state:
                    self.aircraft_state[aircraft_id] = {}
                self.aircraft_state[aircraft_id]['landed'] = True

        # Update state
        if aircraft_id not in self.aircraft_state:
            self.aircraft_state[aircraft_id] = {}

        self.aircraft_state[aircraft_id].update({
            'in_airspace': in_airspace,
            'on_ground': on_ground,
            'last_update': datetime.now(),
            'consecutive_missing': 0,
            'latitude': aircraft_data['latitude'],
            'longitude': aircraft_data['longitude'],
            'altitude_agl': altitude_agl_ft,
            'altitude_msl': altitude_msl_ft,
            'velocity': aircraft_data.get('velocity'),
            'heading': aircraft_data.get('heading'),
        })

        return notifications


class CloudAircraftTracker:
    """
    Global aircraft tracker that tracks for ALL users
    Runs 24/7 in the cloud
    """

    def __init__(self):
        self.user_trackers: Dict[str, UserTracker] = {}
        self.running = False
        self.task = None
        self.sms_stop_last_sent_date = None
        self.whatsapp_stop_last_sent_date = None
        self._discord_last_sent: Dict[str, float] = {}  # webhook_url → last send timestamp

    async def start(self):
        """Start the global tracker"""
        self.running = True
        self.task = asyncio.create_task(self.tracking_loop())

    async def stop(self):
        """Stop the global tracker"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def remove_user(self, user_id: str):
        """Remove a user from active tracking (e.g. after subscription cancellation)."""
        self.user_trackers.pop(user_id, None)

    async def update_user_aircraft(self, user_id: str, db: Session):
        """Update tracked aircraft for a user"""
        # Get user configuration
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        # No live plan, no tracking: a free or lapsed account is not polled for
        # and gets no alerts until it starts a trial or subscribes.
        if live_license(db, user) is None:
            self.user_trackers.pop(user_id, None)
            return

        airport_config = db.query(AirportConfig).filter(AirportConfig.user_id == user_id).first()
        if not airport_config:
            # No config yet, skip
            return

        # Read before anything else is loaded: on an un-migrated database this
        # rolls the session back.
        runway_headings = read_optional_column(db, airport_config, "approach_runway_headings")

        aircraft = db.query(Aircraft).filter(
            Aircraft.user_id == user_id,
            Aircraft.active == True
        ).order_by(Aircraft.created_at, Aircraft.id).all()
        # The free feeder plan follows its first 5; the rest are paused.
        cap = tracked_aircraft_limit(plan_tier(db, user))
        if cap is not None:
            aircraft = aircraft[:cap]

        if not aircraft:
            # No aircraft to track, remove tracker
            if user_id in self.user_trackers:
                del self.user_trackers[user_id]
            return

        # Build config dict
        config = {
            'airspace': {
                'center_lat': airport_config.latitude,
                'center_lon': airport_config.longitude,
                'field_elevation_ft_msl': airport_config.elevation_ft_msl,
                'radius_nm': airport_config.radius_nm,
                'floor_ft_agl': airport_config.floor_ft_agl,
                'ceiling_ft_agl': airport_config.ceiling_ft_agl,
                'query_radius_nm': airport_config.query_radius_nm,
                'alert_distances_nm': [float(d) for d in airport_config.alert_distances_nm],
                'approach_corridor_enabled': airport_config.approach_corridor_enabled or False,
                'approach_runway_heading': airport_config.approach_runway_heading,
                'approach_runway_headings': runway_headings,
            },
            'airport_code': airport_config.airport_code or '',
            'notification_cooldown_minutes': 1,
            'quiet_hours': {
                'enabled': airport_config.quiet_hours_enabled,
                'start': airport_config.quiet_hours_start,
                'end': airport_config.quiet_hours_end
            },
            'timezone': getattr(airport_config, 'timezone', None) or _tz_name_for(airport_config.latitude, airport_config.longitude),
        }

        aircraft_list = [
            {
                'tail_number': a.tail_number,
                'icao24': a.icao24,
                'friendly_name': a.friendly_name,
                'alert_distances': [float(d) for d in a.alert_distances] if a.alert_distances else None
            }
            for a in aircraft
        ]

        # Create or update tracker, keeping what the old one knew about flights
        # in progress.
        fresh = UserTracker(user_id, config, aircraft_list)
        old = self.user_trackers.get(user_id)
        if old is not None:
            fresh.adopt_state(old)
        self.user_trackers[user_id] = fresh

    async def update_team_aircraft(self, team_id: str, db: Session):
        """Update tracked aircraft for a team (reads TeamAirportConfig + TeamAircraft)."""
        from models import Team, TeamAirportConfig, TeamAircraft
        import uuid as _uuid
        try:
            team_uuid = _uuid.UUID(team_id)
        except ValueError:
            return

        config_row = db.query(TeamAirportConfig).filter(
            TeamAirportConfig.team_id == team_uuid, TeamAirportConfig.is_active == True
        ).first()
        if not config_row:
            config_row = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team_uuid).first()
        if not config_row:
            return

        aircraft_rows = db.query(TeamAircraft).filter(
            TeamAircraft.team_id == team_uuid,
            TeamAircraft.active == True
        ).all()

        tracker_key = f"team:{team_id}"

        if not aircraft_rows:
            self.user_trackers.pop(tracker_key, None)
            return

        config = {
            'airspace': {
                'center_lat': config_row.latitude,
                'center_lon': config_row.longitude,
                'field_elevation_ft_msl': config_row.elevation_ft_msl,
                'radius_nm': config_row.radius_nm,
                'floor_ft_agl': config_row.floor_ft_agl,
                'ceiling_ft_agl': config_row.ceiling_ft_agl,
                'query_radius_nm': config_row.query_radius_nm,
                'alert_distances_nm': [float(d) for d in config_row.alert_distances_nm],
                'approach_corridor_enabled': config_row.approach_corridor_enabled or False,
                'approach_runway_heading': config_row.approach_runway_heading,
            },
            'airport_code': config_row.airport_code or '',
            'notification_cooldown_minutes': 1,
            'quiet_hours': {
                'enabled': config_row.quiet_hours_enabled,
                'start': config_row.quiet_hours_start,
                'end': config_row.quiet_hours_end,
            }
        }

        aircraft_list = [
            {
                'tail_number': a.tail_number,
                'icao24': a.icao24,
                'friendly_name': a.friendly_name,
                'alert_distances': [float(d) for d in a.alert_distances] if a.alert_distances else None,
            }
            for a in aircraft_rows
        ]

        self.user_trackers[tracker_key] = UserTracker(tracker_key, config, aircraft_list)

    async def tracking_loop(self):
        """Main tracking loop - runs every 30 seconds"""
        _tick = 0
        while self.running:
            try:
                await self.track_all_users()
                _save_flight_trails()
                coverage.flush(SessionLocal)
                heard.flush(SessionLocal)
                _tick += 1
                # Every 2 ticks (~60s): run escalation check + expected arrival auto-link
                if _tick % 2 == 0:
                    await self.run_escalation_check()
                    await self._auto_link_expected_arrivals()
                await asyncio.sleep(30)
            except Exception as e:
                print(f"Error in tracking loop: {type(e).__name__}: {e}")
                await asyncio.sleep(30)

    async def fetch_adsb(self, icaos: List[str]):
        """adsb.lol's latest for these aircraft: ({icao24: aircraft}, ok).

        Each try gets ADSB_TRY_SECONDS and a failed one is tried again, all
        inside ADSB_BUDGET_SECONDS. One 30 s try used to be the only one, and
        about 1 request in 11 timed out: a whole cycle with no network data
        for anybody. HTTP 4xx is not retried (asking again will not help, and
        hammering after a 403 or 429 is how a host gets blocked)."""
        url = f"https://api.adsb.lol/v2/icao/{','.join(icaos)}"
        clock = asyncio.get_running_loop().time
        # After a refusal, not asked again until its allowance resets.
        if clock() < getattr(self, "_adsb_next_ok", 0.0):
            return {}, False
        started = clock()
        attempts, error = 0, None
        async with aiohttp.ClientSession() as session:
            while attempts < ADSB_MAX_TRIES:
                left = ADSB_BUDGET_SECONDS - (clock() - started)
                if left < 3:
                    break
                attempts += 1
                try:
                    timeout = aiohttp.ClientTimeout(total=min(ADSB_TRY_SECONDS, left), sock_connect=5)
                    async with session.get(url, timeout=timeout) as response:
                        headers = getattr(response, "headers", None) or {}
                        if response.status == 429:
                            self._adsb_next_ok = clock() + adsb_refused_for(headers)
                        if response.status == 200:
                            data = await response.json()
                            lookup = {}
                            for ac in data.get('ac') or []:
                                icao = (ac.get('hex') or '').lower()
                                if icao:
                                    lookup[icao] = ac
                            coverage.record_fetch(True, attempts, clock() - started, error, len(icaos))
                            if attempts > 1:
                                print(f"adsb.lol answered on try {attempts} after {error}")
                            return lookup, True
                        error = f"HTTP {response.status}"
                        if 400 <= response.status < 500:
                            await _log_refusal(response)
                            break
                except asyncio.TimeoutError:
                    error = "timeout"
                except Exception as e:
                    error = type(e).__name__
                if attempts < ADSB_MAX_TRIES:
                    await asyncio.sleep(ADSB_RETRY_PAUSE_SECONDS)
        coverage.record_fetch(False, attempts, clock() - started, error, len(icaos))
        print(f"adsb.lol failed ({error}, {attempts} tries) for {len(icaos)} aircraft; using ground stations only this cycle")
        return {}, False

    async def fetch_flyitaly(self, icaos: List[str]):
        """FlyItalyADSB's latest for these aircraft: ({icao24: aircraft}, ok).
        Same readsb format as adsb.lol. One try per batch of 50; after a 429
        it waits as long as Retry-After says before asking again."""
        if not FLYITALY_API_KEY or not icaos:
            return {}, False
        clock = asyncio.get_running_loop().time
        if clock() < getattr(self, "_flyitaly_pause_until", 0.0):
            return {}, False
        started = clock()
        lookup, error, ok = {}, None, True
        async with aiohttp.ClientSession(headers={"X-Api-Key": FLYITALY_API_KEY}) as session:
            for i in range(0, len(icaos), FLYITALY_BATCH):
                url = FLYITALY_URL + ",".join(icaos[i:i + FLYITALY_BATCH])
                try:
                    timeout = aiohttp.ClientTimeout(total=FLYITALY_TRY_SECONDS, sock_connect=5)
                    async with session.get(url, timeout=timeout) as response:
                        if response.status == 200:
                            data = await response.json()
                            for ac in data.get('ac') or []:
                                icao = (ac.get('hex') or '').lower()
                                if icao:
                                    lookup[icao] = ac
                            continue
                        error, ok = f"HTTP {response.status}", False
                        if response.status == 429:
                            # Pause first, so a header that cannot be read still pauses.
                            self._flyitaly_pause_until = clock() + 60.0
                            try:
                                wait = float(response.headers.get("Retry-After", "60"))
                                self._flyitaly_pause_until = clock() + min(max(wait, 30.0), 3600.0)
                            except (AttributeError, TypeError, ValueError):
                                pass
                        if 400 <= response.status < 500:
                            await _log_refusal(response, "FlyItalyADSB")
                            break
                except asyncio.TimeoutError:
                    error, ok = "timeout", False
                except Exception as e:
                    error, ok = type(e).__name__, False
        coverage.record_fetch(ok, 1, clock() - started, error, len(icaos), source="FlyItalyADSB")
        if not ok:
            print(f"FlyItalyADSB failed ({error}) for {len(icaos)} aircraft")
        return lookup, ok

    async def fetch_network(self, icaos: List[str]):
        """The network's positions, merged per aircraft by the fresher one.
        Returns (lookup, any source answered).

        FlyItalyADSB is asked every cycle. adsb.lol, which allows us 500
        requests a day, is the backup: asked for every aircraft when
        FlyItalyADSB fails, and otherwise only for aircraft that were just
        airborne near their field and that FlyItalyADSB has lost (the ones a
        landing or an approach alert depends on). Without a FlyItalyADSB key,
        adsb.lol is the only source and is asked every cycle as before."""
        if not FLYITALY_API_KEY:
            return await self.fetch_adsb(icaos)
        fia, fia_ok = await self.fetch_flyitaly(icaos)
        if not fia_ok:
            want = list(icaos)
        else:
            want = [i for i in icaos if self._needs_backup(i, fia.get(i))]
        adsb, adsb_ok = {}, False
        if want and self._adsb_backup_allowed():
            adsb, adsb_ok = await self.fetch_adsb(want)
        return _merge_sources(adsb, fia), adsb_ok or fia_ok

    def _needs_backup(self, icao24: str, fia_ac: Optional[dict]) -> bool:
        """FlyItalyADSB has no fresh position for an aircraft that some
        account heard airborne near its field in the last few minutes."""
        seen = (fia_ac or {}).get('seen_pos')
        if isinstance(seen, (int, float)) and seen <= ADSB_FRESH_SECONDS:
            return False
        now = datetime.now()
        for t in list(self.user_trackers.values()):
            st = t.aircraft_state.get(icao24)
            if not st or st.get('on_ground') or st.get('landed'):
                continue
            last = st.get('last_update')
            if not isinstance(last, datetime) or (now - last).total_seconds() > BACKUP_RECENT_SECONDS:
                continue
            if (st.get('last_distance') if st.get('last_distance') is not None else 999) <= BACKUP_NEAR_NM:
                return True
        return False

    def _adsb_backup_allowed(self) -> bool:
        """Within adsb.lol's daily budget and at most once a minute. Counts
        the request it allows."""
        import time as _time
        now = _time.monotonic()
        sent = getattr(self, "_adsb_sent", None)
        if sent is None:
            sent = self._adsb_sent = []
        while sent and now - sent[0] > 86400:
            sent.pop(0)
        if len(sent) >= ADSB_DAILY_BUDGET or (sent and now - sent[-1] < ADSB_BACKUP_EVERY_SECONDS):
            return False
        sent.append(now)
        return True

    async def track_all_users(self):
        """Track aircraft for all active users using a single ICAO-based API call"""
        if not self.user_trackers:
            return

        # Collect all unique ICAOs across every user
        all_icaos: set = set()
        for tracker in self.user_trackers.values():
            all_icaos.update(tracker.aircraft_to_track.keys())

        if not all_icaos:
            return

        # adsb.lol and FlyItalyADSB together, merged by freshness. A failure
        # of both no longer ends the cycle: ground stations can still cover
        # their own aircraft without them.
        icao_lookup, adsb_ok = await self.fetch_network(sorted(all_icaos))
        cycle_at = datetime.utcnow()

        # Iterate a snapshot — request handlers mutate user_trackers across the
        # awaits below, which would otherwise raise "dictionary changed size
        # during iteration" and drop the whole polling cycle.
        for user_id, tracker in list(self.user_trackers.items()):
            try:
                # The account's own receiver, when it has heard an aircraft in
                # the last 30 seconds, is preferred: it hears the aircraft down
                # to the runway, where public coverage often drops out. Out of
                # its range, adsb.lol covers the aircraft.
                gs_positions, gs_at = _gs.fresh_ground_positions(str(user_id))

                seen_icao24 = set()
                for icao24 in list(tracker.aircraft_to_track):
                    tail = tracker.aircraft_to_track[icao24]
                    ground_fix = _fresh_ground_fix(gs_positions.get(icao24))
                    adsb = icao_lookup.get(icao24)
                    _observe_coverage(str(user_id), icao24, tail, tracker.config['airspace'],
                                      adsb, ground_fix, adsb_ok, gs_at is not None, cycle_at)
                    if adsb is not None:
                        heard.note_network(icao24, adsb)
                    if ground_fix is None and adsb is None:
                        continue
                    seen_icao24.add(icao24)

                    from_adsb = _aircraft_from_adsb(icao24, tail, adsb, tracker.config['airspace']) if adsb is not None else None
                    # The flight so far, for the Live Map's trail. The
                    # receiver's own fixes are added as they arrive.
                    if from_adsb is not None and isinstance(adsb.get('seen_pos'), (int, float)) and adsb['seen_pos'] <= 60:
                        heard_at = datetime.utcnow() - timedelta(seconds=float(adsb['seen_pos']))
                        flight_trails.record(
                            str(user_id), icao24, from_adsb['latitude'], from_adsb['longitude'], from_adsb['on_ground'],
                            heard_at,
                        )
                        alt = adsb.get('alt_baro')
                        flight_history.record(
                            str(user_id), icao24, tail, heard_at, from_adsb['latitude'], from_adsb['longitude'],
                            alt if isinstance(alt, (int, float)) else None, adsb.get('gs'), adsb.get('track'),
                            from_adsb['on_ground'], adsb.get('_src', 'adsb.lol'))
                    if ground_fix is None:
                        aircraft_dict = from_adsb
                    else:
                        aircraft_dict = _aircraft_from_ground(icao24, tail, ground_fix)
                        # Landings and takeoffs follow adsb.lol whenever it has a
                        # recent fix, exactly as before the receiver was used
                        # here: that is what reports each lap of pattern work.
                        # The receiver decides only when adsb.lol has lost the
                        # aircraft (low, close to the field).
                        seen_pos = adsb.get('seen_pos') if adsb is not None else None
                        if from_adsb is not None and seen_pos is not None and seen_pos <= ADSB_FRESH_SECONDS:
                            aircraft_dict['on_ground'] = from_adsb['on_ground']
                            aircraft_dict['baro_altitude'] = from_adsb['baro_altitude']

                    notifications = await tracker.check_and_notify(aircraft_dict)
                    notifications = self._drop_recent_repeats(user_id, notifications)
                    if notifications and not tracker.in_quiet_hours():
                        await self.send_notifications(user_id, notifications)

                # Signal loss detection — tracked aircraft neither source has.
                # Skipped when adsb.lol could not be asked: every aircraft would
                # look lost, and one last seen close in would "land".
                for icao24, tail in tracker.aircraft_to_track.items():
                    if not adsb_ok:
                        break
                    if icao24 not in seen_icao24 and icao24 in tracker.aircraft_state:
                        state = tracker.aircraft_state[icao24]
                        missing = state.get('consecutive_missing', 0) + 1
                        state['consecutive_missing'] = missing

                        # Lost from sight on short final: last seen inside 2 nm and low,
                        # then missing for 3 polls (~90 s), so it has landed below
                        # coverage. It used to be "inside 5 nm, any height, 2 polls",
                        # which called a landing whenever an aircraft 3-5 nm out at
                        # pattern height dropped out of coverage for a minute.
                        if (state.get('last_distance', 999) < LOST_LANDING_NM
                                and (state.get('altitude_agl') if state.get('altitude_agl') is not None else 9999) <= LOST_LANDING_AGL_FT
                                and not state.get('on_ground', False)
                                and not state.get('landed', False)
                                and missing >= LOST_LANDING_POLLS):
                            # Check NotificationLog to avoid double-firing if GS already sent
                            from models import NotificationLog
                            _db = SessionLocal()
                            try:
                                recent = _db.query(NotificationLog).filter(
                                    NotificationLog.user_id == user_id,
                                    NotificationLog.aircraft_tail == tail,
                                    NotificationLog.alert_type == 'landing',
                                    NotificationLog.sent_at >= datetime.utcnow() - timedelta(minutes=5),
                                ).first()
                            finally:
                                _db.close()
                            if tracker.should_notify('landing', icao24) and not tracker.in_quiet_hours() and not recent:
                                notifications = [{
                                    'type': 'landing',
                                    'tail': tail,
                                    'distance': state.get('last_distance', 0),
                                    'altitude': state.get('altitude_msl', 0),
                                    'time': datetime.now()
                                }]
                                await self.send_notifications(user_id, notifications)
                                state['landed'] = True

            except Exception as e:
                import traceback
                print(f"Error tracking for user {user_id}: {e}")
                traceback.print_exc()

    def _drop_recent_repeats(self, user_id: str, notifications: List[dict]) -> List[dict]:
        """Landings and takeoffs already logged for this aircraft in the last few
        minutes, by any path (a restart, the signal-loss fallback, an older
        receiver that reported them itself), are not sent again."""
        events = [n for n in notifications if n.get('type') in ('landing', 'takeoff')]
        if not events:
            return notifications
        _db = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(minutes=REPEAT_WINDOW_MINUTES)
            drop = set()
            for n in events:
                hit = _db.query(NotificationLog).filter(
                    NotificationLog.user_id == user_id,
                    NotificationLog.aircraft_tail == n.get('tail'),
                    NotificationLog.alert_type == n['type'],
                    NotificationLog.sent_at >= since,
                ).first()
                if hit:
                    drop.add(id(n))
        except Exception as e:
            print(f"Repeat check failed, sending anyway: {type(e).__name__}: {e}")
            return notifications
        finally:
            _db.close()
        return [n for n in notifications if id(n) not in drop]

    async def send_notifications(self, user_id: str, notifications: List[dict]):
        """Send notifications via configured integrations"""
        db = SessionLocal()
        try:
            # Team tracker keys are prefixed with "team:" — route directly to team notification path
            if user_id.startswith("team:"):
                team_id = user_id[5:]
                await self._send_team_notifications_by_id(team_id, notifications, db)
                db.commit()
                return

            user = db.query(User).filter(User.id == user_id).first()
            # Per-event routing: integrations the user switched off per alert type.
            routing = read_optional_column(db, user, "routing")
            # "free" (no live licence) has no channels, so a plan that lapsed
            # since this account was loaded stops alerting here.
            tier = plan_tier(db, user)

            alert_settings = {
                s.alert_type: s.message_template
                for s in db.query(AlertSetting).filter(AlertSetting.user_id == user_id).all()
            }

            if tier.startswith("team-"):
                await self._send_team_notifications(user_id, user, notifications, alert_settings, db)
            else:
                allowed_channels = send_channels_for_tier(tier)
                integrations = [
                    i for i in db.query(Integration).filter(
                        Integration.user_id == user_id,
                        Integration.enabled == True
                    ).all()
                    if i.type in allowed_channels
                ]
                for notification in notifications:
                    tracker = self.user_trackers.get(user_id)
                    if tracker:
                        notification['airport'] = tracker.config.get('airport_code', '')
                    alert_type = notification['type']
                    template = alert_settings.get(alert_type, self.get_default_template(alert_type))
                    message = self.format_message(template, notification)
                    for integration in routed_integrations(integrations, routing, alert_type):
                        # Texts cost money per message: each account has a monthly allowance.
                        capped = integration.type == 'sms' and sms_over_limit(db, user.id)
                        success = False if capped else await self.send_via_integration(integration, message)
                        db.add(NotificationLog(
                            user_id=user_id,
                            aircraft_tail=notification['tail'],
                            alert_type=alert_type,
                            message=message,
                            integration_type=integration.type,
                            status='limit' if capped else ('sent' if success else 'failed'),
                            sent_at=datetime.utcnow()
                        ))

            db.commit()
        finally:
            db.close()

    async def _send_team_notifications_by_id(self, team_id: str, notifications: List[dict], db):
        """Fan out team tracker alerts to on-duty member channels, with on-duty filtering and escalation."""
        import uuid as _uuid
        from models import Team, TeamChannel, TeamAlertSetting, TeamMember, AircraftClaim, EscalationConfig, AlertEscalation
        try:
            team_uuid = _uuid.UUID(team_id)
        except ValueError:
            return
        team = db.query(Team).filter(Team.id == team_uuid).first()
        if not team:
            return
        await self._dispatch_team_alerts(team, f"team:{team_id}", notifications, db)

    async def _dispatch_team_alerts(self, team, tracker_key: str, notifications: List[dict], db):
        """Core team alert dispatch: on-duty filtering, claim status, escalation trigger."""
        from models import TeamChannel, TeamAlertSetting, TeamMember, User as UserM, AircraftClaim, EscalationConfig, AlertEscalation, License
        from datetime import datetime, timedelta

        all_channels = db.query(TeamChannel).filter(TeamChannel.team_id == team.id, TeamChannel.enabled == True).all()
        routing = team.routing or {}

        alert_settings = {
            s.alert_type: s.message_template
            for s in db.query(TeamAlertSetting).filter(TeamAlertSetting.team_id == team.id).all()
        }

        # Determine on-duty channels: channels owned by on-duty members
        on_duty_user_ids = set()
        for m in team.members:
            if self._check_on_duty(str(team.id), str(m.user_id), db):
                on_duty_user_ids.add(str(m.user_id))

        tracker = self.user_trackers.get(tracker_key)
        now = datetime.utcnow()

        for notification in notifications:
            if tracker:
                notification['airport'] = tracker.config.get('airport_code', '')

            alert_type = notification['type']
            icao24 = notification.get('icao24', '').lower()
            tail = notification.get('tail', '')

            # Append claim status to message
            claim_suffix = ""
            if icao24:
                active_claim = db.query(AircraftClaim).filter(
                    AircraftClaim.team_id == team.id,
                    AircraftClaim.icao24 == icao24,
                    AircraftClaim.released_at.is_(None),
                    AircraftClaim.expires_at > now,
                ).first()
                if active_claim:
                    claimer = db.query(UserM).filter(UserM.id == active_claim.claimed_by_user_id).first()
                    name = claimer.email.split("@")[0] if claimer else "a team member"
                    claim_suffix = f"\n✋ Claimed by {name}"

            template = alert_settings.get(alert_type, self.get_default_template(alert_type))
            message = self.format_message(template, notification) + claim_suffix

            disabled_ids = set(routing.get(alert_type, []))
            eligible_channels = [c for c in all_channels if str(c.id) not in disabled_ids]

            # Filter to on-duty channels; fallback to all if nobody on duty
            if on_duty_user_ids:
                # On-duty members only — we match channels by team (all channels broadcast to all members)
                # Since team channels are shared, we always send to all eligible channels when someone is on duty
                channels_to_notify = eligible_channels
            else:
                channels_to_notify = eligible_channels  # fallback: everyone

            for channel in channels_to_notify:
                class _W:
                    def __init__(self, c):
                        self.type = c.integration_type
                        self.config = c.config
                        self.id = c.id
                success = await self.send_via_integration(_W(channel), message)
                log_entry = NotificationLog(
                    user_id=str(team.id),
                    aircraft_tail=tail,
                    alert_type=alert_type,
                    message=message,
                    integration_type=channel.integration_type,
                    status='sent' if success else 'failed',
                    sent_at=now,
                )
                db.add(log_entry)

            # Trigger escalation if enabled
            esc_config = db.query(EscalationConfig).filter(EscalationConfig.team_id == team.id, EscalationConfig.enabled == True).first()
            if esc_config:
                db.add(AlertEscalation(
                    team_id=team.id,
                    aircraft_tail=tail,
                    alert_type=alert_type,
                    original_fired_at=now,
                    escalation_level=0,
                    escalated_at=now,
                ))

    def _check_on_duty(self, team_id: str, user_id: str, db) -> bool:
        """Check if a user is currently on duty (shift schedule + overrides)."""
        from models import TeamDutyOverride, TeamShift, TeamShiftMember
        import uuid as _uuid
        now = datetime.utcnow()
        try:
            team_uuid = _uuid.UUID(team_id)
            user_uuid = _uuid.UUID(user_id)
        except ValueError:
            return False
        override = db.query(TeamDutyOverride).filter(
            TeamDutyOverride.team_id == team_uuid,
            TeamDutyOverride.user_id == user_uuid,
        ).order_by(TeamDutyOverride.created_at.desc()).first()
        if override:
            if override.override_until is None or override.override_until > now:
                return override.on_duty
        # Check shift schedule
        weekday = now.weekday()
        shifts = db.query(TeamShift).filter(TeamShift.team_id == team_uuid).all()
        for shift in shifts:
            if weekday not in (shift.days_of_week or []):
                continue
            try:
                import pytz
                tz = pytz.timezone(shift.timezone or "UTC")
                local_now = now.replace(tzinfo=pytz.utc).astimezone(tz)
                local_time = local_now.strftime("%H:%M")
                if shift.start_time <= local_time <= shift.end_time:
                    assigned = db.query(TeamShiftMember).filter(
                        TeamShiftMember.shift_id == shift.id,
                        TeamShiftMember.user_id == user_uuid,
                    ).first()
                    if assigned:
                        return True
            except Exception:
                pass
        return False

    async def _auto_link_expected_arrivals(self):
        """Auto-link expected arrivals to ADS-B when tail matches within 2hr of expected_at."""
        from models import ExpectedArrival, TeamAirportConfig
        import uuid as _uuid
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            window_start = now - timedelta(hours=1)
            window_end = now + timedelta(hours=2)
            pending = db.query(ExpectedArrival).filter(
                ExpectedArrival.status == "pending",
                ExpectedArrival.expected_at >= window_start,
                ExpectedArrival.expected_at <= window_end,
                ExpectedArrival.linked_icao24.is_(None),
            ).all()
            for arrival in pending:
                tracker_key = f"team:{arrival.team_id}"
                ut = self.user_trackers.get(tracker_key)
                if not ut:
                    continue
                # Look for a tracked aircraft whose tail matches the expected arrival
                for icao24, tail in ut.aircraft_to_track.items():
                    if tail.upper() == (arrival.tail_number or "").upper():
                        state = ut.aircraft_state.get(icao24, {})
                        if state:
                            arrival.linked_icao24 = icao24
                            if state.get('on_ground'):
                                arrival.status = "arrived"
                            break
            db.commit()
        except Exception as e:
            print(f"Expected arrival auto-link error: {e}")
        finally:
            db.close()

    async def run_escalation_check(self):
        """Check for unacked team alerts and fire escalation notifications."""
        from models import EscalationConfig, AlertEscalation, Team, TeamChannel, TeamMember, User as UserM, NotificationLog, License
        import uuid as _uuid
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            configs = db.query(EscalationConfig).filter(EscalationConfig.enabled == True).all()
            for cfg in configs:
                team = db.query(Team).filter(Team.id == cfg.team_id).first()
                if not team:
                    continue
                # Find pending escalations for this team (level=0 means just fired, not yet escalated)
                pending = db.query(AlertEscalation).filter(
                    AlertEscalation.team_id == cfg.team_id,
                    AlertEscalation.acked_at.is_(None),
                ).all()
                for esc in pending:
                    age_minutes = (now - esc.original_fired_at).total_seconds() / 60
                    if esc.escalation_level == 0 and age_minutes >= cfg.first_escalation_minutes:
                        # Fire level 1 escalation
                        await self._fire_escalation(team, esc, 1, cfg.first_escalation_target, db)
                        esc.escalation_level = 1
                        esc.escalated_at = now
                    elif esc.escalation_level == 1 and age_minutes >= cfg.second_escalation_minutes:
                        # Fire level 2 escalation
                        await self._fire_escalation(team, esc, 2, cfg.second_escalation_target, db)
                        esc.escalation_level = 2
                        esc.escalated_at = now
            db.commit()
        except Exception as e:
            print(f"Escalation check error: {e}")
        finally:
            db.close()

    async def _fire_escalation(self, team, esc, level: int, target: str, db):
        """Send escalation notification to target."""
        from models import TeamChannel, TeamMember, User as UserM
        msg = (
            f"🚨 ESCALATION L{level}: {esc.aircraft_tail} — {esc.alert_type} alert unacknowledged "
            f"for {int((datetime.utcnow() - esc.original_fired_at).total_seconds() / 60)} minutes."
        )
        channels = db.query(TeamChannel).filter(TeamChannel.team_id == team.id, TeamChannel.enabled == True).all()
        if target == "owner":
            owner_member = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.role == "owner").first()
            if owner_member:
                # Only send to SMS channel of owner if they have one
                pass  # For now, send to all channels (owner doesn't have a separate channel)
        # Send to all channels (simplest correct behavior for now)
        for ch in channels:
            class _W:
                def __init__(self, c):
                    self.type = c.integration_type
                    self.config = c.config
                    self.id = c.id
            await self.send_via_integration(_W(ch), msg)

    async def _send_team_notifications(self, user_id, user, notifications, alert_settings, db):
        """Fan out alerts to all team channels, filtered by per-distance routing rules."""
        from models import Team, TeamChannel

        if not user or not user.license_id:
            return
        team = db.query(Team).filter(Team.license_id == user.license_id).first()
        if not team:
            return

        await self._dispatch_team_alerts(team, user_id, notifications, db)

    def get_default_template(self, alert_type: str) -> str:
        """Get default message template"""
        # Normalize alert_type — strip .0 from floats like "10.0nm" -> "10nm"
        normalized = alert_type
        if 'nm' in alert_type:
            num = alert_type.replace('nm', '')
            try:
                f = float(num)
                normalized = f'{int(f)}nm' if f == int(f) else f'{f}nm'
            except ValueError:
                pass

        templates = {
            'landing': '✅ **{tail_number}** has landed at **{airport}** – {runway}',
            'takeoff': '🛫 **{tail_number}** – takeoff from **{airport}**\nAlt {altitude}ft MSL, {speed}kts',
        }
        if normalized in templates:
            return templates[normalized]
        # Distance alerts
        if 'nm' in normalized:
            return f'**{{tail_number}}** – **{normalized}** from **{{airport}}**\nETA ~{{eta}}min, Alt {{altitude}}ft MSL, {{speed}}kts'
        return f'**{{tail_number}}** – **{normalized}** from **{{airport}}**\nAlt {{altitude}}ft MSL'

    def format_message(self, template: str, notification: dict) -> str:
        """Format message from template"""
        tail = notification.get('tail', 'N/A')
        # Use the alert threshold distance (e.g. "2nm" -> "2") instead of actual distance
        alert_type = notification.get('type', '')
        if 'nm' in alert_type:
            threshold = alert_type.replace('nm', '')
        else:
            threshold = f"{notification.get('distance', 0):.1f}"
        hdg = notification.get('heading')
        if hdg is not None:
            rwy_num = round(float(hdg) / 10) % 36
            if rwy_num == 0:
                rwy_num = 36
            runway = f"Runway {rwy_num:02d}"
        else:
            runway = ""

        try:
            return template.format(
                tail=tail,
                tail_number=tail,
                distance=threshold,
                altitude=f"{notification.get('altitude', 0):.0f}",
                eta=notification.get('eta', 'N/A'),
                speed=f"{notification.get('speed', 0):.0f}",
                time=notification.get('time', datetime.now()).strftime('%H:%M'),
                airport=notification.get('airport', ''),
                runway=runway,
            )
        except (KeyError, ValueError):
            return self.format_message(self.get_default_template(alert_type), notification)

    async def send_via_integration(self, integration: Integration, message: str) -> bool:
        """Send notification via specific integration"""
        try:
            if integration.type == 'discord':
                return await self.send_discord(integration.config, message)
            elif integration.type == 'slack':
                return await self.send_slack(integration.config, message)
            elif integration.type == 'teams':
                return await self.send_teams(integration.config, message)
            elif integration.type == 'email':
                return await self.send_email(integration.config, message)
            elif integration.type == 'sms':
                return await self.send_sms(integration.config, message)
            elif integration.type == 'whatsapp':
                return await self.send_whatsapp(integration.config, message)
            elif integration.type == 'google_chat':
                return await self.send_google_chat(integration.config, message)
            elif integration.type == 'telegram':
                return await self.send_telegram(integration.config, message)
            elif integration.type == 'webhook':
                return await self.send_webhook(integration.config, message)
            else:
                return False
        except Exception as e:
            print(f"Error sending via {integration.type}: {e}")
            return False

    async def send_discord(self, config: dict, message: str) -> bool:
        """Send Discord webhook"""
        webhook_url = config.get('webhook_url')
        if not webhook_url or not _is_safe_public_url(webhook_url):
            return False

        # Enforce 5-second cooldown per webhook so Discord sends a push for each alert
        now = asyncio.get_event_loop().time()
        last = self._discord_last_sent.get(webhook_url, 0)
        wait = 5.0 - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._discord_last_sent[webhook_url] = asyncio.get_event_loop().time()

        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json={'content': message},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status != 204:
                    body = await response.text()
                    print(f"Discord webhook failed: HTTP {response.status} — {body[:200]}")
                return response.status == 204

    async def send_slack(self, config: dict, message: str) -> bool:
        """Send Slack webhook"""
        webhook_url = config.get('webhook_url')
        if not webhook_url or not _is_safe_public_url(webhook_url):
            return False

        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json={'text': message},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return response.status == 200

    async def send_teams(self, config: dict, message: str) -> bool:
        """Send Microsoft Teams webhook"""
        webhook_url = config.get('webhook_url')
        if not webhook_url or not _is_safe_public_url(webhook_url):
            return False

        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json={'text': message},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return response.status == 200

    @staticmethod
    def email_html_body(message: str) -> str:
        """The alert text as email HTML. The text is the user's own wording, so
        it is escaped first: a template can't put links or markup into an
        email sent from FinalPing's address. **bold** stays bold."""
        safe = _html.escape(message)
        safe = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe).replace('**', '')
        return safe.replace('\n', '<br>')

    async def send_email(self, config: dict, message: str) -> bool:
        """Send email notification via Resend"""
        import os
        to_email = config.get('to_email')
        if not to_email:
            return False

        resend_api_key = os.environ.get('RESEND_API_KEY')
        if not resend_api_key:
            print("RESEND_API_KEY not set")
            return False

        html_body = self.email_html_body(message)
        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
            <div style="background: #0f1117; padding: 20px; border-radius: 8px;">
                <h2 style="color: #38bdf8; margin: 0 0 16px 0;">✈️ FinalPing Alert</h2>
                <div style="color: #f9fafb; font-size: 15px; line-height: 1.6;">
                    {html_body}
                </div>
                <hr style="border-color: #2d3748; margin: 16px 0;">
                <p style="color: #6b7280; font-size: 12px; margin: 0;">
                    Sent by <a href="https://finalpingapp.com" style="color: #38bdf8;">FinalPing</a> &mdash;
                    <a href="https://finalpingapp.com/account" style="color: #38bdf8;">Manage notifications</a>
                </p>
            </div>
        </div>
        """

        async with aiohttp.ClientSession() as session:
            async with session.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {resend_api_key}',
                    'Content-Type': 'application/json',
                },
                json={
                    'from': 'FinalPing <noreply@finalpingapp.com>',
                    'to': [to_email],
                    'subject': f'✈️ FinalPing: {message.split(chr(10))[0].replace("**", "")[:60].strip()}',
                    'html': html,
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    return True
                else:
                    error = await response.text()
                    print(f"Resend error: {error}")
                    return False

    async def send_sms(self, config: dict, message: str) -> bool:
        """Send SMS via Twilio"""
        import os
        to_phone = config.get('to_phone')
        if not to_phone:
            return False

        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        from_phone = os.environ.get('TWILIO_PHONE_NUMBER')

        if not account_sid or not auth_token or not from_phone:
            print("Twilio credentials not set")
            return False

        # Strip markdown bold formatting for SMS
        plain_message = message.replace('**', '')

        # Append opt-out text only on the first SMS of each day
        today = datetime.now().date()
        if self.sms_stop_last_sent_date != today:
            plain_message = f"{plain_message}\n\nReply STOP to unsubscribe."
            self.sms_stop_last_sent_date = today

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
                auth=aiohttp.BasicAuth(account_sid, auth_token),
                data={
                    'From': from_phone,
                    'To': to_phone,
                    'Body': plain_message,
                }
            ) as response:
                if response.status == 201:
                    return True
                else:
                    error = await response.text()
                    print(f"Twilio SMS error: {error}")
                    return False

    async def send_whatsapp(self, config: dict, message: str) -> bool:
        """Send WhatsApp message via Twilio"""
        import os
        to_phone = config.get('to_phone')
        if not to_phone:
            return False

        account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        from_phone = os.environ.get('TWILIO_WHATSAPP_NUMBER')

        if not account_sid or not auth_token or not from_phone:
            print("Twilio WhatsApp credentials not set")
            return False

        plain_message = message.replace('**', '')

        # Append opt-out text only on the first WhatsApp message of each day
        today = datetime.now().date()
        if self.whatsapp_stop_last_sent_date != today:
            plain_message = f"{plain_message}\n\nReply STOP to unsubscribe."
            self.whatsapp_stop_last_sent_date = today

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
                auth=aiohttp.BasicAuth(account_sid, auth_token),
                data={
                    'From': f'whatsapp:{from_phone}',
                    'To': f'whatsapp:{to_phone}',
                    'Body': plain_message,
                }
            ) as response:
                if response.status == 201:
                    return True
                else:
                    error = await response.text()
                    print(f"Twilio WhatsApp error: {error}")
                    return False

    async def send_google_chat(self, config: dict, message: str) -> bool:
        """Send Google Chat webhook"""
        webhook_url = config.get('webhook_url')
        if not webhook_url or not _is_safe_public_url(webhook_url):
            return False
        plain = message.replace('**', '*')
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json={'text': plain},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return response.status == 200

    async def send_telegram(self, config: dict, message: str) -> bool:
        """Send Telegram message via Bot API"""
        bot_token = config.get('bot_token')
        chat_id = config.get('chat_id')
        if not bot_token or not chat_id:
            return False
        plain = message.replace('**', '')
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'https://api.telegram.org/bot{bot_token}/sendMessage',
                json={'chat_id': chat_id, 'text': plain},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return response.status == 200

    async def send_webhook(self, config: dict, message: str) -> bool:
        """Send generic webhook POST"""
        url = config.get('webhook_url') or config.get('url')
        if not url or not _is_safe_public_url(url):
            return False
        headers = {'Content-Type': 'application/json'}
        secret = config.get('secret')
        if secret:
            headers['X-FinalPing-Secret'] = secret
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={'message': message, 'source': 'finalpingapp'},
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return 200 <= response.status < 300

    async def send_test_notification(self, integration: Integration) -> bool:
        """Send test notification"""
        if integration.type == 'email':
            test_message = f"Test Notification\nYour email integration is working!"
        elif integration.type in ('sms', 'whatsapp'):
            test_message = f"FinalPing Test: Your {integration.type.upper()} integration is working!"
        else:
            test_message = f"🧪 **Test Notification**\nYour {integration.type} integration is working! ✅"
        return await self.send_via_integration(integration, test_message)

    async def get_live_aircraft(self, user_id: str) -> List[dict]:
        """Get current aircraft data for a user, preferring GS positions when online."""
        tracker = self.user_trackers.get(user_id)
        if not tracker:
            return []

        field_elev = float(tracker.config['airspace'].get('field_elevation_ft_msl', 0))
        center_lat = float(tracker.config['airspace']['center_lat'])
        center_lon = float(tracker.config['airspace']['center_lon'])

        result = []
        result_by_icao = {}
        for icao24, tail in tracker.aircraft_to_track.items():
            state = tracker.aircraft_state.get(icao24, {})
            if state:
                last_update = state.get('last_update')
                # last_update is local time (datetime.now()), like every tracker stamp.
                if last_update and (datetime.now() - last_update).total_seconds() > 1800:
                    continue  # stale — not seen in 30+ min, show as Not Detected
                entry = {
                    'tail_number': tail,
                    'icao24': icao24,
                    'status': 'in_airspace' if state.get('in_airspace') else 'outside',
                    'on_ground': state.get('on_ground', False),
                    'distance_nm': state.get('last_distance', 0),
                    'altitude_ft_agl': state.get('altitude_agl', 0),
                    'altitude_ft_msl': state.get('altitude_msl', 0),
                    'velocity_kts': state.get('velocity', 0),
                    'heading': state.get('heading', 0),
                    'is_approaching': state.get('last_distance', 0) < state.get('max_distance', 999),
                    'last_seen': state.get('last_update', datetime.utcnow()),
                    'latitude': state.get('latitude'),
                    'longitude': state.get('longitude'),
                    'source': 'cloud',
                }
                result.append(entry)
                result_by_icao[icao24] = entry

        # The receiver's positions: for each aircraft, its last fix from the
        # account's own receiver is used whenever it is newer than the network
        # position. Keeping the last fix (not just the latest batch) holds an
        # aircraft where it was last heard through a gap on the runway or a
        # taxiway, instead of jumping back to an older network position out
        # on final.
        now = datetime.utcnow()
        for icao24, (gp, gp_at) in list(_gs.ground_last_fix.get(user_id, {}).items()):
            if icao24 not in tracker.aircraft_to_track:
                continue
            lat, lon = gp.get('lat'), gp.get('lon')
            if lat is None or lon is None or (now - gp_at).total_seconds() > _gs.LAST_FIX_MAX_AGE_SECONDS:
                continue
            existing = result_by_icao.get(icao24)
            cloud_at = existing.get('last_seen') if existing else None
            # The tracker stamps network fixes with local time (datetime.now());
            # receiver fixes are UTC. Compare both in UTC.
            if isinstance(cloud_at, datetime) and cloud_at + (datetime.utcnow() - datetime.now()) > gp_at:
                continue   # the network has heard it more recently
            tail = tracker.aircraft_to_track[icao24]
            alt_baro = gp.get('altitude', field_elev)
            alt_msl = float(alt_baro) if alt_baro else field_elev
            alt_agl = max(0, alt_msl - field_elev)
            distance = tracker.haversine_distance(center_lat, center_lon, lat, lon)
            on_ground = gp.get('on_ground', False)
            fields = {
                'latitude': lat,
                'longitude': lon,
                'on_ground': on_ground,
                'altitude_ft_agl': alt_agl,
                'altitude_ft_msl': alt_msl,
                'distance_nm': distance,
                'last_seen': gp_at,
                'source': 'ground_station',
            }
            if existing:
                existing.update(fields)
                existing['velocity_kts'] = gp.get('speed', existing['velocity_kts'])
                existing['heading'] = gp.get('heading', existing['heading'])
            else:
                # Only the receiver has it (on the ground, below the network)
                result.append({
                    'tail_number': tail,
                    'icao24': icao24,
                    'status': 'on_ground' if on_ground else 'outside',
                    'velocity_kts': gp.get('speed', 0),
                    'heading': gp.get('heading', 0),
                    'is_approaching': False,
                    **fields,
                })

        # Lost from sight low and close to the airport: it has landed below
        # coverage. Without this the map keeps it hanging on final, where it
        # was last heard, for half an hour.
        for entry in result:
            seen = entry.get('last_seen')
            if not isinstance(seen, datetime):
                continue
            # Receiver fixes are stamped in UTC, the tracker's in local time.
            unheard = ((now if entry.get('source') == 'ground_station' else datetime.now()) - seen).total_seconds()
            agl = entry.get('altitude_ft_agl')
            distance = entry.get('distance_nm')
            if entry.get('on_ground'):
                # Called on the ground from a fix out on final (low, stale and
                # still at approach speed), then not heard again: it landed
                # below coverage, not where that fix put it. N584SF on 25 Nov
                # sat 1.4 nm south of the field at 71 kt for 18 minutes.
                speed = entry.get('velocity_kts')
                # From the fix itself: the stored distance stops updating once
                # an aircraft is called on the ground.
                lat, lon = entry.get('latitude'), entry.get('longitude')
                fix_nm = tracker.haversine_distance(center_lat, center_lon, lat, lon)                     if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else None
                on_final = (
                    fix_nm is not None and OFF_FIELD_NM < fix_nm <= LANDED_UNSEEN_NM
                    and isinstance(speed, (int, float)) and speed >= ON_FINAL_KT
                )
                if not (on_final and unheard >= LANDED_CALLED_SECONDS):
                    continue
                entry.update({
                    'status': 'on_ground', 'is_approaching': False, 'velocity_kts': 0,
                    'altitude_ft_agl': 0, 'altitude_ft_msl': field_elev,
                    'latitude': None, 'longitude': None,
                })
                continue
            low_and_close = (
                isinstance(agl, (int, float)) and agl <= LANDED_UNSEEN_AGL_FT
                and isinstance(distance, (int, float)) and distance <= LANDED_UNSEEN_NM
            )
            called = bool(tracker.aircraft_state.get(entry['icao24'], {}).get('landed'))
            if (low_and_close and unheard >= LANDED_UNSEEN_SECONDS) or (called and unheard >= LANDED_CALLED_SECONDS):
                entry.update({
                    'status': 'on_ground',
                    'on_ground': True,
                    'is_approaching': False,
                    'velocity_kts': 0,
                    'altitude_ft_agl': 0,
                    'altitude_ft_msl': field_elev,
                    # Where on the airport it is now is not known.
                    'latitude': None,
                    'longitude': None,
                })

        return result
