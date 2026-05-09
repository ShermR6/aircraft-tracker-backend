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

from models import User, Aircraft, AirportConfig, AlertSetting, Integration, NotificationLog
from database import SessionLocal


class UserTracker:
    """Tracks aircraft for a single user"""

    def __init__(self, user_id: str, config: dict, aircraft_list: List[dict]):
        self.user_id = user_id
        self.config = config
        self.aircraft_to_track = {a['icao24']: a['tail_number'] for a in aircraft_list if a.get('icao24')}

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
            alert_distances = sorted(self.config['airspace'].get('alert_distances_nm', [10.0, 5.0, 2.0]), reverse=True)

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

            if max_distance is not None and prev_distance is not None:
                for alert_distance in alert_distances:
                    alert_key = dist_key(alert_distance)

                    was_beyond_boundary = max_distance > alert_distance
                    crossed_boundary = (prev_distance > alert_distance and distance_nm <= alert_distance)

                    if crossed_boundary and was_beyond_boundary and alert_key not in self.distance_alerts_sent[aircraft_id]:
                        # Send the distance alert
                        if self.should_notify(f'distance_{alert_distance}', aircraft_id):
                            speed_kts = aircraft_data.get('velocity')
                            if speed_kts and speed_kts > 30:
                                eta_minutes = max(1, int((distance_nm / speed_kts) * 60))
                            else:
                                eta_minutes = max(1, int(distance_nm / 1.5))
                            notifications.append({
                                'type': alert_key,
                                'tail': callsign,
                                'distance': distance_nm,
                                'altitude': altitude_msl_ft,
                                'eta': eta_minutes,
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

            if aircraft_id not in self.aircraft_state:
                self.aircraft_state[aircraft_id] = {}
            self.aircraft_state[aircraft_id]['last_distance'] = distance_nm
            self.aircraft_state[aircraft_id]['max_distance'] = max_distance

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
                'alert_distances_nm': [float(d) for d in airport_config.alert_distances_nm]
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
                'friendly_name': a.friendly_name
            }
            for a in aircraft
        ]

        # Create or update tracker
        self.user_trackers[user_id] = UserTracker(user_id, config, aircraft_list)

    async def tracking_loop(self):
        """Main tracking loop - runs every 10 seconds"""
        while self.running:
            try:
                await self.track_all_users()
                await asyncio.sleep(10)  # 10-second polling
            except Exception as e:
                print(f"Error in tracking loop: {e}")
                await asyncio.sleep(10)

    async def track_all_users(self):
        """Track aircraft for all active users"""
        if not self.user_trackers:
            return

        # Group users by approximate location (rounded to 0.5 deg ~30nm) to share API calls
        location_groups: dict = {}
        location_params: dict = {}
        for user_id, tracker in self.user_trackers.items():
            cfg = tracker.config['airspace']
            lat_bucket = round(float(cfg['center_lat']) * 2) / 2
            lon_bucket = round(float(cfg['center_lon']) * 2) / 2
            key = (lat_bucket, lon_bucket)
            if key not in location_groups:
                location_groups[key] = []
                location_params[key] = {'lat': cfg['center_lat'], 'lon': cfg['center_lon'], 'radius': float(cfg['query_radius_nm'])}
            else:
                location_params[key]['radius'] = max(location_params[key]['radius'], float(cfg['query_radius_nm']))
            location_groups[key].append(user_id)

        async with aiohttp.ClientSession() as session:
            # Fetch once per location bucket, share results across users in that bucket
            location_aircraft: dict = {}
            for key, params in location_params.items():
                url = f"https://api.adsb.lol/v2/lat/{params['lat']}/lon/{params['lon']}/dist/{params['radius']}"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            location_aircraft[key] = data.get('ac', [])
                except Exception as e:
                    print(f"Error fetching location bucket {key}: {e}")

            for user_id, tracker in self.user_trackers.items():
                try:
                    cfg = tracker.config['airspace']
                    lat_bucket = round(float(cfg['center_lat']) * 2) / 2
                    lon_bucket = round(float(cfg['center_lon']) * 2) / 2
                    key = (lat_bucket, lon_bucket)
                    aircraft_list = location_aircraft.get(key, [])

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

                                # On ground if: altitude within 150ft of field AND ground speed under 30kts
                                if alt_agl < 150 and gs is not None and gs < 30:
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

                            # Send notifications (skip during quiet hours)
                            if notifications and not tracker.in_quiet_hours():
                                await self.send_notifications(user_id, notifications)

                    # Signal loss detection — check tracked aircraft NOT in the API response
                    for icao24, tail in tracker.aircraft_to_track.items():
                        if icao24 not in seen_icao24 and icao24 in tracker.aircraft_state:
                            state = tracker.aircraft_state[icao24]
                            missing = state.get('consecutive_missing', 0) + 1
                            state['consecutive_missing'] = missing

                            # If aircraft was ready for landing and disappeared for 3+ polls (~30 sec)
                            if (state.get('landing_ready', False)
                                    and not state.get('landed', False)
                                    and missing >= 3):
                                if tracker.should_notify('landing', icao24) and not tracker.in_quiet_hours():
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
        "team-premium": ["discord", "email", "slack", "sms", "teams"],
        "team-pro":     ["discord", "email", "slack", "sms", "teams", "whatsapp"],
    }

    async def send_notifications(self, user_id: str, notifications: List[dict]):
        """Send notifications via configured integrations"""
        db = SessionLocal()
        try:
            # Get user's tier to enforce channel restrictions
            user = db.query(User).filter(User.id == user_id).first()
            tier = "starter"
            if user and user.license_id:
                from models import License
                lic = db.query(License).filter(License.id == user.license_id).first()
                if lic:
                    tier = lic.tier
            allowed_channels = self.TIER_CHANNELS.get(tier, self.TIER_CHANNELS["starter"])

            # Get user's integrations (filter by tier-allowed channels)
            integrations = [
                i for i in db.query(Integration).filter(
                    Integration.user_id == user_id,
                    Integration.enabled == True
                ).all()
                if i.type in allowed_channels
            ]

            # Get alert settings to get custom message templates
            alert_settings = {
                s.alert_type: s.message_template
                for s in db.query(AlertSetting).filter(AlertSetting.user_id == user_id).all()
            }

            for notification in notifications:
                # Add airport code from tracker config
                tracker = self.user_trackers.get(user_id)
                if tracker:
                    notification['airport'] = tracker.config.get('airport_code', '')

                # Build message from template
                alert_type = notification['type']
                template = alert_settings.get(alert_type, self.get_default_template(alert_type))
                message = self.format_message(template, notification)

                # Send via each integration
                for integration in integrations:
                    success = await self.send_via_integration(integration, message)

                    # Log notification
                    log = NotificationLog(
                        user_id=user_id,
                        aircraft_tail=notification['tail'],
                        alert_type=alert_type,
                        message=message,
                        integration_type=integration.type,
                        status='sent' if success else 'failed',
                        sent_at=datetime.utcnow()
                    )
                    db.add(log)

            db.commit()
        finally:
            db.close()

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
            'landing': '✅ **{tail_number}** has landed at **{airport}**'
        }
        # All distance alerts use the same format
        if normalized != 'landing':
            return f'**{{tail_number}}** – **{normalized}** from **{{airport}}**\nETA ~{{eta}}min, Alt {{altitude}}ft MSL'
        return templates.get(normalized, f'**{{tail_number}}** – **{normalized}** from **{{airport}}**\nAlt {{altitude}}ft MSL')

    def format_message(self, template: str, notification: dict) -> str:
        """Format message from template"""
        tail = notification.get('tail', 'N/A')
        # Use the alert threshold distance (e.g. "2nm" -> "2") instead of actual distance
        alert_type = notification.get('type', '')
        if 'nm' in alert_type:
            threshold = alert_type.replace('nm', '')
        else:
            threshold = f"{notification.get('distance', 0):.1f}"
        return template.format(
            tail=tail,
            tail_number=tail,
            distance=threshold,
            altitude=f"{notification.get('altitude', 0):.0f}",
            eta=notification.get('eta', 'N/A'),
            time=notification.get('time', datetime.now()).strftime('%H:%M'),
            airport=notification.get('airport', ''),
        )

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
                    'subject': f'✈️ FinalPing: {message[:60].strip()}',
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
        """Get current aircraft data for a user"""
        tracker = self.user_trackers.get(user_id)
        if not tracker:
            return []

        # Return current state
        result = []
        for icao24, tail in tracker.aircraft_to_track.items():
            state = tracker.aircraft_state.get(icao24, {})
            if state:
                result.append({
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
                })

        return result
