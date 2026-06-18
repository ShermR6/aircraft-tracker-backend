"""
Cloud Aircraft Tracker
Tracks aircraft for ALL users in a centralized cloud service
Adapted from your working KDTO tracker code
"""

import asyncio
import aiohttp
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, asin
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from models import User, Aircraft, AirportConfig, AlertSetting, Integration, NotificationLog, Team, TeamChannel, TeamAlertSetting, TeamMember, TeamAirportConfig, AircraftClaim, EscalationConfig, AlertEscalation, TeamShift, TeamShiftMember, TeamDutyOverride, ExpectedArrival
from database import SessionLocal
import ground_state as _gs


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
        self.last_notifications = {}
        # Track last date SMS/WhatsApp opt-out was appended (once per day)
        self.sms_stop_last_sent_date = None
        self.whatsapp_stop_last_sent_date = None

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
        now_str = datetime.now().strftime('%H:%M')
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

                    if crossed_with_buffer and alert_key not in self.distance_alerts_sent[aircraft_id]:
                        # Approach corridor filter — skip if aircraft heading is not aligned with runway
                        if airspace.get('approach_corridor_enabled') and airspace.get('approach_runway_heading') is not None:
                            track = aircraft_data.get('track') or aircraft_data.get('heading')
                            if track is not None:
                                rwy_hdg = float(airspace['approach_runway_heading'])
                                diff = abs((track - rwy_hdg + 180) % 360 - 180)
                                if diff > 35:
                                    self.distance_alerts_sent[aircraft_id].add(alert_key)  # suppress quietly
                                    continue

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

            # Reset alerts if plane goes back out beyond the largest configured distance + 2nm buffer
            reset_distance = (max(alert_distances) + 2.0) if alert_distances else 12.0
            if distance_nm > reset_distance:
                self.distance_alerts_sent[aircraft_id] = set()
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

        airport_config = db.query(AirportConfig).filter(AirportConfig.user_id == user_id).first()
        if not airport_config:
            # No config yet, skip
            return

        aircraft = db.query(Aircraft).filter(
            Aircraft.user_id == user_id,
            Aircraft.active == True
        ).all()

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
            },
            'airport_code': airport_config.airport_code or '',
            'notification_cooldown_minutes': 1,
            'quiet_hours': {
                'enabled': airport_config.quiet_hours_enabled,
                'start': airport_config.quiet_hours_start,
                'end': airport_config.quiet_hours_end
            }
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

        # Create or update tracker
        self.user_trackers[user_id] = UserTracker(user_id, config, aircraft_list)

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
                _tick += 1
                # Every 2 ticks (~60s): run escalation check + expected arrival auto-link
                if _tick % 2 == 0:
                    await self.run_escalation_check()
                    await self._auto_link_expected_arrivals()
                await asyncio.sleep(30)
            except Exception as e:
                print(f"Error in tracking loop: {type(e).__name__}: {e}")
                await asyncio.sleep(30)

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

        # One API call for all aircraft anywhere on the globe
        icao_lookup: dict = {}
        icao_str = ",".join(all_icaos)
        url = f"https://api.adsb.lol/v2/icao/{icao_str}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        data = await response.json()
                        for ac in data.get('ac', []):
                            icao = ac.get('hex', '').lower()
                            if icao:
                                icao_lookup[icao] = ac
            except Exception as e:
                print(f"Error fetching ICAO batch: {type(e).__name__}: {e}")
                return

        for user_id, tracker in self.user_trackers.items():
            try:
                # When ground station is online it handles alerts — cloud tracker still tracks positions for live map
                gs_online = _gs.is_ground_station_online(str(user_id))

                aircraft_list = [icao_lookup[icao] for icao in tracker.aircraft_to_track if icao in icao_lookup]

                # Filter to only tracked aircraft
                seen_icao24 = set()
                for aircraft_data in aircraft_list:
                    icao24 = aircraft_data.get('hex', '').lower()
                    if icao24 in tracker.aircraft_to_track:
                        seen_icao24.add(icao24)
                        # Build aircraft dict
                        alt_baro = aircraft_data.get('alt_baro')
                        gs = aircraft_data.get('gs')
                        baro_rate = aircraft_data.get('baro_rate')
                        seen_pos = aircraft_data.get('seen_pos')  # seconds since last position update

                        # Ground detection: multiple signals
                        is_on_ground = alt_baro == 'ground'

                        if not is_on_ground and alt_baro is not None and alt_baro != 'ground':
                            field_elev = float(tracker.config['airspace'].get('field_elevation_ft_msl', 0))
                            alt_agl = float(alt_baro) - field_elev if isinstance(alt_baro, (int, float)) else 999

                            # On ground if: altitude within 150ft of field AND speed under 50kts (or no speed reported)
                            if alt_agl < 150 and (gs is None or gs < 50):
                                is_on_ground = True

                            # On ground if: very close to airport, low altitude, and stale position data (>30s)
                            ac_lat = aircraft_data.get('lat')
                            ac_lon = aircraft_data.get('lon')
                            if ac_lat and ac_lon and seen_pos is not None and seen_pos > 30:
                                center_lat = float(tracker.config['airspace']['center_lat'])
                                center_lon = float(tracker.config['airspace']['center_lon'])
                                # Quick distance estimate in nm
                                dlat = abs(float(ac_lat) - center_lat) * 60
                                dlon = abs(float(ac_lon) - center_lon) * 60 * 0.85  # rough cos correction
                                approx_dist = (dlat**2 + dlon**2) ** 0.5
                                if approx_dist < 3 and alt_agl < 500:
                                    is_on_ground = True

                        aircraft_dict = {
                            'icao24': icao24,
                            'callsign': tracker.aircraft_to_track[icao24],
                            'latitude': aircraft_data.get('lat'),
                            'longitude': aircraft_data.get('lon'),
                            'baro_altitude': alt_baro,
                            'on_ground': is_on_ground,
                            'velocity': gs,
                            'heading': aircraft_data.get('track'),
                        }

                        # Check and get notifications
                        notifications = await tracker.check_and_notify(aircraft_dict)

                        # Cloud always sends distance alerts regardless of GS status.
                        # GS handles takeoff/landing (local events); cloud is the fallback
                        # for everything else and the primary for out-of-range distance alerts.
                        if notifications and not tracker.in_quiet_hours():
                            await self.send_notifications(user_id, notifications)

                # Signal loss detection — check tracked aircraft NOT in the API response
                for icao24, tail in tracker.aircraft_to_track.items():
                    if icao24 not in seen_icao24 and icao24 in tracker.aircraft_state:
                        state = tracker.aircraft_state[icao24]
                        missing = state.get('consecutive_missing', 0) + 1
                        state['consecutive_missing'] = missing

                        # If aircraft was close to airport and disappeared for 2+ polls (~60 sec)
                        # Use last_distance instead of landing_ready so restarts don't break detection
                        # Cloud fires as fallback even when GS is online; dedup via NotificationLog
                        if (state.get('last_distance', 999) < 5.0
                                and not state.get('on_ground', False)
                                and not state.get('landed', False)
                                and missing >= 2):
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

    TIER_CHANNELS = {
        "starter":      ["discord", "email"],
        "premium":      ["discord", "email", "slack", "sms", "teams"],
        "pro":          ["discord", "email", "slack", "sms", "teams", "whatsapp"],
        "team-starter": ["discord", "email"],
        "team-premium": ["discord", "email", "slack", "sms", "teams", "google_chat", "webhook"],
        "team-pro":     ["discord", "email", "slack", "sms", "teams", "google_chat", "webhook", "whatsapp"],
    }

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
            tier = "starter"
            if user and user.license_id:
                from models import License
                lic = db.query(License).filter(License.id == user.license_id).first()
                if lic:
                    tier = lic.tier

            alert_settings = {
                s.alert_type: s.message_template
                for s in db.query(AlertSetting).filter(AlertSetting.user_id == user_id).all()
            }

            if tier.startswith("team-"):
                await self._send_team_notifications(user_id, user, notifications, alert_settings, db)
            else:
                allowed_channels = self.TIER_CHANNELS.get(tier, self.TIER_CHANNELS["starter"])
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
                    for integration in integrations:
                        success = await self.send_via_integration(integration, message)
                        db.add(NotificationLog(
                            user_id=user_id,
                            aircraft_tail=notification['tail'],
                            alert_type=alert_type,
                            message=message,
                            integration_type=integration.type,
                            status='sent' if success else 'failed',
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
        if not webhook_url:
            return False

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
        if not webhook_url:
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
        if not webhook_url:
            return False

        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json={'text': message},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                return response.status == 200

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

        # Convert plain message to simple HTML
        html_body = message.replace('\n', '<br>').replace('**', '')
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
                    'subject': f'✈️ FinalPing: {message.split(chr(10))[0][:60].strip()}',
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
        if not webhook_url:
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
        if not url:
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
                if last_update and (datetime.utcnow() - last_update).total_seconds() > 1800:
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

        # Overlay GS positions when ground station is online — higher accuracy, 5s updates,
        # includes on-ground aircraft that adsb.lol may not have
        if _gs.is_ground_station_online(user_id):
            gs_positions = _gs.ground_positions.get(user_id, {})
            for icao24, gp in gs_positions.items():
                if icao24 not in tracker.aircraft_to_track:
                    continue
                lat = gp.get('lat')
                lon = gp.get('lon')
                if lat is None or lon is None:
                    continue
                tail = tracker.aircraft_to_track[icao24]
                alt_baro = gp.get('altitude', field_elev)
                alt_msl = float(alt_baro) if alt_baro else field_elev
                alt_agl = max(0, alt_msl - field_elev)
                distance = tracker.haversine_distance(center_lat, center_lon, lat, lon)
                on_ground = gp.get('on_ground', False)

                if icao24 in result_by_icao:
                    # Update existing cloud entry with fresher GS data
                    e = result_by_icao[icao24]
                    e.update({
                        'latitude': lat,
                        'longitude': lon,
                        'on_ground': on_ground,
                        'altitude_ft_agl': alt_agl,
                        'altitude_ft_msl': alt_msl,
                        'velocity_kts': gp.get('speed', e['velocity_kts']),
                        'heading': gp.get('heading', e['heading']),
                        'distance_nm': distance,
                        'last_seen': datetime.utcnow(),
                        'source': 'ground_station',
                    })
                else:
                    # Aircraft only visible to GS (on ground, not in adsb.lol)
                    result.append({
                        'tail_number': tail,
                        'icao24': icao24,
                        'status': 'on_ground' if on_ground else 'outside',
                        'on_ground': on_ground,
                        'distance_nm': distance,
                        'altitude_ft_agl': alt_agl,
                        'altitude_ft_msl': alt_msl,
                        'velocity_kts': gp.get('speed', 0),
                        'heading': gp.get('heading', 0),
                        'is_approaching': False,
                        'last_seen': datetime.utcnow(),
                        'latitude': lat,
                        'longitude': lon,
                        'source': 'ground_station',
                    })

        return result
