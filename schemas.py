"""
Pydantic Schemas
Request and response models for API validation
"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# ============================================================================
# LICENSE & AUTHENTICATION
# ============================================================================

class LicenseActivation(BaseModel):
    """License activation request"""
    license_key: str = Field(..., min_length=19, max_length=24)
    email: EmailStr


class LicenseResponse(BaseModel):
    """License information"""
    license_key: str
    tier: str
    activations_used: int
    activations_max: int
    expires_at: Optional[datetime]
    status: str


class TokenResponse(BaseModel):
    """JWT token response"""
    access_token: str
    token_type: str
    user_id: str
    email: str
    license_tier: str
    expires_at: Optional[datetime]


class UserLogin(BaseModel):
    """User login request"""
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    """User information"""
    id: str
    email: str
    license_tier: str
    created_at: datetime


# ============================================================================
# AIRCRAFT
# ============================================================================

class AircraftCreate(BaseModel):
    """Create aircraft request"""
    tail_number: str = Field(..., min_length=3, max_length=10)
    icao24: Optional[str] = Field(None, min_length=6, max_length=6)
    friendly_name: Optional[str] = None


class AircraftResponse(BaseModel):
    """Aircraft response"""
    id: str
    tail_number: str
    icao24: Optional[str]
    friendly_name: Optional[str]
    active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class LiveAircraftResponse(BaseModel):
    """Real-time aircraft data"""
    tail_number: str
    icao24: Optional[str]
    status: str  # 'in_airspace', 'outside', 'on_ground'
    distance_nm: float
    altitude_ft_agl: Optional[float]
    altitude_ft_msl: Optional[float]
    velocity_kts: Optional[float]
    is_approaching: bool
    last_seen: datetime
    latitude: Optional[float]
    longitude: Optional[float]


# ============================================================================
# AIRPORT CONFIGURATION
# ============================================================================

class AirportConfigCreate(BaseModel):
    """Create airport configuration"""
    airport_code: Optional[str] = None
    airport_name: Optional[str] = None
    latitude: str
    longitude: str
    elevation_ft_msl: int
    radius_nm: str = "4.0"
    floor_ft_agl: int = 0
    ceiling_ft_agl: int = 2500
    query_radius_nm: str = "100.0"
    alert_distances_nm: List[str] = ["10.0", "5.0", "2.0"]
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "06:00"


class AirportConfigResponse(BaseModel):
    """Airport configuration response"""
    id: str
    airport_code: Optional[str]
    airport_name: Optional[str]
    latitude: str
    longitude: str
    elevation_ft_msl: int
    radius_nm: str
    floor_ft_agl: int
    ceiling_ft_agl: int
    query_radius_nm: str
    alert_distances_nm: List[str]
    quiet_hours_enabled: bool
    quiet_hours_start: str
    quiet_hours_end: str
    created_at: datetime
    updated_at: datetime


# ============================================================================
# ALERT SETTINGS
# ============================================================================

class AlertSettingCreate(BaseModel):
    """Create alert setting"""
    alert_type: str = Field(..., pattern="^(\d+nm|landing)$")
    enabled: bool = True
    message_template: str


class AlertSettingResponse(BaseModel):
    """Alert setting response"""
    id: str
    alert_type: str
    enabled: bool
    message_template: str
    created_at: datetime


# ============================================================================
# INTEGRATIONS
# ============================================================================

class IntegrationCreate(BaseModel):
    """Create integration"""
    type: str = Field(..., pattern="^(discord|slack|teams|email)$")
    config: Dict[str, Any]
    enabled: bool = True


class IntegrationResponse(BaseModel):
    """Integration response"""
    id: str
    type: str
    config: Dict[str, Any]
    enabled: bool
    created_at: datetime


# ============================================================================
# NOTIFICATIONS
# ============================================================================

class NotificationCreate(BaseModel):
    """Create notification"""
    aircraft_tail: str
    alert_type: str
    message: str


class NotificationResponse(BaseModel):
    """Notification response"""
    id: str
    aircraft_tail: str
    alert_type: str
    message: str
    integration_type: str
    status: str
    sent_at: datetime
