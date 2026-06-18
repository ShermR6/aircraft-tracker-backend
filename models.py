"""
Database Models
SQLAlchemy ORM models for PostgreSQL
"""

from sqlalchemy import Column, String, Boolean, Integer, Float, DateTime, ForeignKey, JSON, Text, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import uuid
from datetime import datetime

from database import Base


class User(Base):
    """User account"""
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    license_id = Column(UUID(as_uuid=True), ForeignKey("licenses.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    ground_station_enabled = Column(Boolean, default=False)
    gs_device_key = Column(String(64), unique=True, nullable=True, index=True)
    gs_last_heartbeat = Column(DateTime, nullable=True)
    
    # Relationships
    license = relationship("License", back_populates="users")
    aircraft = relationship("Aircraft", back_populates="user", cascade="all, delete-orphan")
    alert_settings = relationship("AlertSetting", back_populates="user", cascade="all, delete-orphan")
    integrations = relationship("Integration", back_populates="user", cascade="all, delete-orphan")
    airport_config = relationship("AirportConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")


class License(Base):
    """License keys"""
    __tablename__ = "licenses"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    license_key = Column(String(24), unique=True, nullable=False, index=True)
    tier = Column(String(20), nullable=False)
    activations_used = Column(Integer, default=0)
    activations_max = Column(Integer, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="inactive")
    stripe_subscription_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    users = relationship("User", back_populates="license")


class Aircraft(Base):
    """Tracked aircraft"""
    __tablename__ = "aircraft"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    tail_number = Column(String(10), nullable=False)
    icao24 = Column(String(10), nullable=True)
    friendly_name = Column(String(100), nullable=True)
    aircraft_type = Column(String(100), nullable=True)
    alert_distances = Column(JSON, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="aircraft")


class AirportConfig(Base):
    """Airport configuration for each user"""
    __tablename__ = "airport_configs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    
    # Airport details
    airport_code = Column(String(10), nullable=True)
    airport_name = Column(String(255), nullable=True)
    latitude = Column(String(20), nullable=False)
    longitude = Column(String(20), nullable=False)
    elevation_ft_msl = Column(Integer, nullable=False)
    
    # Airspace configuration
    radius_nm = Column(String(10), default="4.0")
    floor_ft_agl = Column(Integer, default=0)
    ceiling_ft_agl = Column(Integer, default=2500)
    
    # Detection settings
    query_radius_nm = Column(String(10), default="100.0")
    alert_distances_nm = Column(JSON, default=["10.0", "5.0", "2.0"])
    
    # SDR reception range (36 buckets, one per 10-degree bearing, in nm)
    sdr_range_nm = Column(JSON, nullable=True)
    sdr_range_updated_at = Column(DateTime, nullable=True)

    # Approach corridor
    runway_info = Column(JSON, nullable=True)          # [{ident, leHdg, heHdg, lengthFt}, ...]
    approach_corridor_enabled = Column(Boolean, default=False)
    approach_runway_heading = Column(Float, nullable=True)  # degrees true

    # Quiet hours
    quiet_hours_enabled = Column(Boolean, default=True)
    quiet_hours_start = Column(String(5), default="23:00")
    quiet_hours_end = Column(String(5), default="06:00")
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="airport_config")


class AlertSetting(Base):
    """Alert configuration"""
    __tablename__ = "alert_settings"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    alert_type = Column(String(50), nullable=False)
    enabled = Column(Boolean, default=True)
    message_template = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="alert_settings")


class Integration(Base):
    """Third-party integrations (Discord, Slack, etc.)"""
    __tablename__ = "integrations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type = Column(String(50), nullable=False)
    config = Column(JSON, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="integrations")


class NotificationLog(Base):
    """Log of sent notifications"""
    __tablename__ = "notification_logs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    aircraft_tail = Column(String(10), nullable=False)
    alert_type = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    integration_type = Column(String(50), nullable=False)
    status = Column(String(20), default="sent")
    sent_at = Column(DateTime, default=datetime.utcnow)


class SavedLocation(Base):
    """Saved tracking locations (multiple per user)"""
    __tablename__ = "saved_locations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)           # e.g. "KDTO Home Base"
    airport_code = Column(String(10), nullable=True)
    latitude = Column(String(20), nullable=False)
    longitude = Column(String(20), nullable=False)
    elevation_ft_msl = Column(Integer, default=0)
    is_active = Column(Boolean, default=False)           # only one can be active
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class Team(Base):
    """Team for multi-user (team tier) accounts"""
    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    license_id = Column(UUID(as_uuid=True), ForeignKey("licenses.id"), unique=True, nullable=False)
    name = Column(String(255), nullable=True)
    routing = Column(JSON, default=dict)  # {"10nm": [disabled_channel_id, ...], ...}
    created_at = Column(DateTime, default=datetime.utcnow)

    members = relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    channels = relationship("TeamChannel", back_populates="team", cascade="all, delete-orphan")
    aircraft = relationship("TeamAircraft", back_populates="team", cascade="all, delete-orphan")
    airport_config = relationship("TeamAirportConfig", back_populates="team", uselist=False, cascade="all, delete-orphan")
    alert_settings = relationship("TeamAlertSetting", back_populates="team", cascade="all, delete-orphan")
    invite_tokens = relationship("TeamInviteToken", back_populates="team", cascade="all, delete-orphan")
    roles = relationship("TeamRole", back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    """Member of a team"""
    __tablename__ = "team_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    role = Column(String(20), default="member")  # "owner", "admin", "member"
    custom_role_id = Column(UUID(as_uuid=True), ForeignKey("team_roles.id"), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="members")
    user = relationship("User")
    custom_role = relationship("TeamRole", foreign_keys=[custom_role_id])


class TeamChannel(Base):
    """A notification channel belonging to a team"""
    __tablename__ = "team_channels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    integration_type = Column(String(50), nullable=False)  # "sms", "discord", "slack", "email"
    label = Column(String(100), nullable=False)
    config = Column(JSON, nullable=False)  # {"to_phone": ...} / {"webhook_url": ...} / {"to_email": ...}
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="channels")


class TeamAircraft(Base):
    """Aircraft tracked by a team (separate from personal user aircraft)"""
    __tablename__ = "team_aircraft"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    tail_number = Column(String(10), nullable=False)
    icao24 = Column(String(10), nullable=True)
    friendly_name = Column(String(100), nullable=True)
    aircraft_type = Column(String(100), nullable=True)
    alert_distances = Column(JSON, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="aircraft")


class TeamAirportConfig(Base):
    """Airport/location config for a team (separate from personal user config)"""
    __tablename__ = "team_airport_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False, unique=True)
    airport_code = Column(String(10), nullable=True)
    airport_name = Column(String(255), nullable=True)
    latitude = Column(String(20), nullable=False)
    longitude = Column(String(20), nullable=False)
    elevation_ft_msl = Column(Integer, nullable=False)
    radius_nm = Column(String(10), default="4.0")
    floor_ft_agl = Column(Integer, default=0)
    ceiling_ft_agl = Column(Integer, default=2500)
    query_radius_nm = Column(String(10), default="100.0")
    alert_distances_nm = Column(JSON, default=["10.0", "5.0", "2.0"])
    runway_info = Column(JSON, nullable=True)
    approach_corridor_enabled = Column(Boolean, default=False)
    approach_runway_heading = Column(Float, nullable=True)
    quiet_hours_enabled = Column(Boolean, default=True)
    quiet_hours_start = Column(String(5), default="23:00")
    quiet_hours_end = Column(String(5), default="06:00")
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team = relationship("Team", back_populates="airport_config")


class TeamAlertSetting(Base):
    """Alert setting for a team (separate from personal user alert settings)"""
    __tablename__ = "team_alert_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    alert_type = Column(String(50), nullable=False)
    enabled = Column(Boolean, default=True)
    message_template = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="alert_settings")


class TeamInviteToken(Base):
    """One-time-use invite token for joining a team"""
    __tablename__ = "team_invite_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    token = Column(String(36), unique=True, nullable=False, index=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    note = Column(String(100), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    used_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="invite_tokens")


class TeamRole(Base):
    """Custom role defined by team owner"""
    __tablename__ = "team_roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    name = Column(String(50), nullable=False)
    permissions = Column(JSON, default=list)
    color = Column(String(7), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team", back_populates="roles")


class AircraftClaim(Base):
    """A team member's claim on an incoming aircraft"""
    __tablename__ = "aircraft_claims"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    icao24 = Column(String(10), nullable=False)
    tail_number = Column(String(10), nullable=True)
    claimed_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    claimed_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    released_at = Column(DateTime, nullable=True)
    flight_note = Column(Text, nullable=True)

    team = relationship("Team")
    claimed_by = relationship("User")


class TeamShift(Base):
    """A recurring shift definition for a team"""
    __tablename__ = "team_shifts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    name = Column(String(100), nullable=False)
    days_of_week = Column(JSON, nullable=False)  # [0,1,2,3,4] = Mon-Fri
    start_time = Column(String(5), nullable=False)  # "06:00"
    end_time = Column(String(5), nullable=False)    # "14:00"
    timezone = Column(String(50), default="UTC")
    color = Column(String(7), default="#22d3a3")
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team")
    shift_members = relationship("TeamShiftMember", back_populates="shift", cascade="all, delete-orphan")


class TeamShiftMember(Base):
    """Assignment of a user to a shift"""
    __tablename__ = "team_shift_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_id = Column(UUID(as_uuid=True), ForeignKey("team_shifts.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    shift = relationship("TeamShift", back_populates="shift_members")
    user = relationship("User")


class TeamDutyOverride(Base):
    """Manual on/off duty override for a team member"""
    __tablename__ = "team_duty_overrides"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    on_duty = Column(Boolean, nullable=False)
    override_until = Column(DateTime, nullable=True)  # None = indefinite
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team")
    user = relationship("User")


class ExpectedArrival(Base):
    """A pre-logged expected aircraft arrival"""
    __tablename__ = "expected_arrivals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    tail_number = Column(String(10), nullable=False)
    icao24 = Column(String(10), nullable=True)
    expected_at = Column(DateTime, nullable=False)
    notes = Column(Text, nullable=True)
    reminder_minutes = Column(Integer, default=30)
    status = Column(String(20), default="pending")  # pending/arrived/cancelled/late
    linked_icao24 = Column(String(10), nullable=True)  # set when ADS-B match found
    reminder_sent = Column(Boolean, default=False)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    team = relationship("Team")
    created_by = relationship("User")


class EscalationConfig(Base):
    """Escalation settings for a team"""
    __tablename__ = "escalation_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), unique=True, nullable=False)
    enabled = Column(Boolean, default=False)
    first_escalation_minutes = Column(Integer, default=5)
    first_escalation_target = Column(String(20), default="all_admins")  # all_admins/owner/all_members
    second_escalation_minutes = Column(Integer, default=10)
    second_escalation_target = Column(String(20), default="owner")

    team = relationship("Team")


class AlertEscalation(Base):
    """Record of a fired escalation"""
    __tablename__ = "alert_escalations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    aircraft_tail = Column(String(10), nullable=False)
    alert_type = Column(String(50), nullable=False)
    original_fired_at = Column(DateTime, nullable=False)
    escalation_level = Column(Integer, nullable=False)  # 1 or 2
    escalated_at = Column(DateTime, default=datetime.utcnow)
    acked_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    acked_at = Column(DateTime, nullable=True)

    team = relationship("Team")
