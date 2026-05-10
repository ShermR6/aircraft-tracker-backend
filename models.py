"""
Database Models
SQLAlchemy ORM models for PostgreSQL
"""

from sqlalchemy import Column, String, Boolean, Integer, DateTime, ForeignKey, JSON, Text
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
