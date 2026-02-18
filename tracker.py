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
        
    def haversine_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points in nautical miles"""
        lat1, lon1, lat2, lon2 = map(radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        nm = 3440.065 * c
        return nm
    
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
        
        # Check altitude
        altitude_msl_m = aircraft_data['baro_altitude']
        if altitude_msl_m is not None:
            altitude_msl_ft = altitude_msl_m * 3.28084
            altitude_agl_ft = altitude_msl_ft - airspace['field_elevation_ft_msl']
            in_vertical = airspace['floor_ft_agl'] <= altitude_agl_ft <= airspace['ceiling_ft_agl']
        else:
            altitude_agl_ft = 0
            altitude_msl_ft = 0
            in_vertical = on_ground
        
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
            
            if max_distance is not None and prev_distance is not None:
                for alert_distance in alert_distances:
                    alert_key = f"{alert_distance}nm"
                    
                    was_beyond_boundary = max_distance > alert_distance
                    crossed_boundary = (prev_distance > alert_distance and distance_nm <= alert_distance)
                    
                    if crossed_boundary and was_beyond_boundary and alert_key not in self.distance_alerts_sent[aircraft_id]:
                        # Special handling for 2nm = landing assumption
                        if alert_distance == 2.0:
                            if "10.0nm" in self.distance_alerts_sent[aircraft_id] and "5.0nm" in self.distance_alerts_sent[aircraft_id]:
                                # Plane crossed 10nm -> 5nm -> 2nm = LANDING!
                                if self.should_notify('landing', aircraft_id):
                                    already_landed = self.aircraft_state.get(aircraft_id, {}).get('landed', False)
                                    if not already_landed:
                                        notifications.append({
                                            'type': 'landing',
                                            'tail': callsign,
                                            'distance': distance_nm,
                                            'altitude': altitude_agl_ft,
                                            'time': datetime.now()
                                        })
                                        self.aircraft_state.setdefault(aircraft_id, {})['landed'] = True
                                        self.distance_alerts_sent[aircraft_id].add(alert_key)
                            else:
                                # Send distance alert instead
                                if self.should_notify(f'distance_{alert_distance}', aircraft_id):
                                    eta_minutes = int(distance_nm / 1.5)
                                    notifications.append({
                                        'type': f'{alert_distance}nm',
                                        'tail': callsign,
                                        'distance': distance_nm,
                                        'altitude': altitude_agl_ft,
                                        'eta': eta_minutes,
                                        'time': datetime.now()
                                    })
                                    self.distance_alerts_sent[aircraft_id].add(alert_key)
                        else:
                            # Regular distance alert (10nm or 5nm)
                            if self.should_notify(f'distance_{alert_distance}', aircraft_id):
                                eta_minutes = int(distance_nm / 1.5)
                                notifications.append({
                                    'type': f'{alert_distance}nm',
                                    'tail': callsign,
                                    'distance': distance_nm,
                                    'altitude': altitude_agl_ft,
                                    'eta': eta_minutes,
                                    'time': datetime.now()
                                })
                                self.distance_alerts_sent[aircraft_id].add(alert_key)
            
            # Reset alerts if plane goes back out beyond 12nm
            if distance_nm > 12.0:
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
            'consecutive_missing': 0
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
        
        # Gather all unique ICAO24 codes to track
        all_icao24 = set()
        for tracker in self.user_trackers.values():
            all_icao24.update(tracker.aircraft_to_track.keys())
        
        if not all_icao24:
            return
        
        # Fetch aircraft data from adsb.lol
        # Group by location to minimize API calls
        # For now, do one query per user's location
        
        async with aiohttp.ClientSession() as session:
            for user_id, tracker in self.user_trackers.items():
                try:
                    config = tracker.config['airspace']
                    lat = config['center_lat']
                    lon = config['center_lon']
                    radius = config['query_radius_nm']
                    
                    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius}"
                    
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        if response.status == 200:
                            data = await response.json()
                            aircraft_list = data.get('ac', [])
                            
                            # Filter to only tracked aircraft
                            for aircraft_data in aircraft_list:
                                icao24 = aircraft_data.get('hex', '').lower()
                                if icao24 in tracker.aircraft_to_track:
                                    # Build aircraft dict
                                    aircraft_dict = {
                                        'icao24': icao24,
                                        'callsign': tracker.aircraft_to_track[icao24],
                                        'latitude': aircraft_data.get('lat'),
                                        'longitude': aircraft_data.get('lon'),
                                        'baro_altitude': aircraft_data.get('alt_baro'),
                                        'on_ground': aircraft_data.get('alt_baro') == 'ground',
                                        'velocity': aircraft_data.get('gs')
                                    }
                                    
                                    # Check and get notifications
                                    notifications = await tracker.check_and_notify(aircraft_dict)
                                    
                                    # Send notifications
                                    if notifications:
                                        await self.send_notifications(user_id, notifications)
                
                except Exception as e:
                    print(f"Error tracking for user {user_id}: {e}")
    
    async def send_notifications(self, user_id: str, notifications: List[dict]):
        """Send notifications via configured integrations"""
        db = SessionLocal()
        try:
            # Get user's integrations
            integrations = db.query(Integration).filter(
                Integration.user_id == user_id,
                Integration.enabled == True
            ).all()
            
            # Get alert settings to get custom message templates
            alert_settings = {
                s.alert_type: s.message_template
                for s in db.query(AlertSetting).filter(AlertSetting.user_id == user_id).all()
            }
            
            for notification in notifications:
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
        templates = {
            '10nm': '**{tail} - 10nm out**\nETA ~{eta}min, Alt {altitude}ft AGL',
            '5nm': '**{tail} - 5nm out**\nETA ~{eta}min, Alt {altitude}ft AGL',
            '2nm': '**{tail} - 2nm out**\nETA ~{eta}min, Alt {altitude}ft AGL',
            'landing': '**🛬 {tail} LANDING**\nTime: {time}\n✅ Ready to put away'
        }
        return templates.get(alert_type, '{tail} alert')
    
    def format_message(self, template: str, notification: dict) -> str:
        """Format message from template"""
        return template.format(
            tail=notification.get('tail', 'N/A'),
            distance=f"{notification.get('distance', 0):.1f}",
            altitude=f"{notification.get('altitude', 0):.0f}",
            eta=notification.get('eta', 'N/A'),
            time=notification.get('time', datetime.now()).strftime('%H:%M')
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
    
    async def send_test_notification(self, integration: Integration) -> bool:
        """Send test notification"""
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
                    'distance_nm': state.get('last_distance', 0),
                    'altitude_ft_agl': state.get('altitude_agl', 0),
                    'altitude_ft_msl': state.get('altitude_msl', 0),
                    'velocity_kts': state.get('velocity', 0),
                    'is_approaching': state.get('last_distance', 0) < state.get('max_distance', 999),
                    'last_seen': state.get('last_update', datetime.utcnow()),
                    'latitude': None,  # Not stored in state currently
                    'longitude': None
                })
        
        return result
