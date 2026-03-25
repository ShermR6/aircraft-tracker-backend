"""
FinalPing Cloud Backend
Main FastAPI application
"""

from fastapi import FastAPI, Depends, HTTPException, Request, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
import jwt
import os
import httpx
import secrets
import string
from typing import List, Optional

from database import get_db, engine, Base, SessionLocal
from models import User, License, Aircraft, AlertSetting, Integration, AirportConfig, SavedLocation
from schemas import (
    LicenseActivation, LicenseResponse,
    UserLogin, UserResponse, TokenResponse,
    AircraftCreate, AircraftResponse,
    AlertSettingCreate, AlertSettingResponse,
    IntegrationCreate, IntegrationResponse,
    LiveAircraftResponse
)
from tracker import CloudAircraftTracker

# Create database tables
Base.metadata.create_all(bind=engine)

# Initialize FastAPI app
app = FastAPI(
    title="FinalPing Cloud API",
    description="Real-time aircraft tracking and notifications",
    version="1.0.0"
)

# CORS middleware (allow desktop app and web app to connect)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
WEBHOOK_INTERNAL_SECRET = os.getenv("WEBHOOK_INTERNAL_SECRET", "skyping-internal-secret")

# License duration
LICENSE_DURATION_DAYS = 30

# Website URL for syncing license status
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://finalpingapp.com")

# Tier feature limits (None = unlimited)
TIER_LIMITS = {
    "starter":      {"aircraft": 3,    "locations": 1,    "integrations": 1},
    "premium":      {"aircraft": 10,   "locations": 5,    "integrations": 3},
    "pro":          {"aircraft": None, "locations": None, "integrations": None},
    "team-starter": {"aircraft": 3,    "locations": 1,    "integrations": 1},
    "team-premium": {"aircraft": 10,   "locations": 5,    "integrations": 3},
    "team-pro":     {"aircraft": None, "locations": None, "integrations": None},
}


def get_user_tier(user: "User", db) -> str:
    """Get the license tier for a user"""
    if user.license_id:
        from models import License
        lic = db.query(License).filter(License.id == user.license_id).first()
        if lic:
            return lic.tier
    return "starter"


def get_tier_limit(tier: str, feature: str):
    """Get the limit for a feature on a given tier. Returns None for unlimited."""
    return TIER_LIMITS.get(tier, TIER_LIMITS["starter"]).get(feature, 1)


async def sync_license_to_website(license_key: str, activated_at: datetime, expires_at: datetime, tier: str = None, email: str = None):
    """Notify the website DB that a license has been activated. Fire-and-forget."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{WEBSITE_URL}/api/licenses/sync",
                headers={"x-webhook-secret": WEBHOOK_INTERNAL_SECRET},
                json={
                    "license_key": license_key,
                    "activated_at": activated_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "tier": tier,
                    "email": email,
                },
                timeout=5.0
            )
    except Exception as e:
        print(f"Website license sync failed (non-critical): {e}")

# Global tracker instance (runs 24/7)
tracker = CloudAircraftTracker()


# Schema for license provisioning
class LicenseProvision(BaseModel):
    license_key: str
    tier: str
    email: str


# ============================================================================
# AUTHENTICATION & LICENSE MANAGEMENT
# ============================================================================

def create_access_token(user_id: str, expires_delta: timedelta = timedelta(days=30)):
    """Create JWT access token"""
    expire = datetime.utcnow() + expires_delta
    to_encode = {"sub": user_id, "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Verify JWT token and return current user"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user


@app.post("/api/activate", response_model=TokenResponse)
async def activate_license(
    activation: LicenseActivation,
    db: Session = Depends(get_db)
):
    """
    Activate a license key.
    - First activation starts the 30-day timer.
    - Subsequent activations (same key) just log in if not expired.
    """
    # Normalize email to lowercase to prevent duplicate accounts
    activation.email = activation.email.lower().strip()

    # Find license
    license = db.query(License).filter(
        License.license_key == activation.license_key
    ).first()
    
    if not license:
        raise HTTPException(status_code=404, detail="Invalid license key")
    
    # Check if expired
    if license.status == "expired":
        raise HTTPException(status_code=403, detail="License has expired")
    
    if license.expires_at and license.expires_at < datetime.utcnow():
        license.status = "expired"
        db.commit()
        raise HTTPException(status_code=403, detail="License has expired")
    
    # If this is the first activation, start the 30-day timer
    if not license.activated_at:
        license.activated_at = datetime.utcnow()
        license.expires_at = datetime.utcnow() + timedelta(days=LICENSE_DURATION_DAYS)
        license.status = "active"
        license.activations_used += 1
        db.commit()
        db.refresh(license)
    elif not license.expires_at:
        # Already activated but expires_at is missing — set it from activated_at
        license.expires_at = license.activated_at + timedelta(days=LICENSE_DURATION_DAYS)
        license.status = "active"
        db.commit()
        db.refresh(license)
    elif license.status != "active":
        # Was provisioned but not yet marked active (edge case)
        license.status = "active"
        db.commit()
    
    # Check activation limit (for re-activations on different devices)
    if license.activations_max != -1:  # -1 = unlimited
        if license.activations_used > license.activations_max:
            raise HTTPException(
                status_code=403,
                detail=f"Maximum activations ({license.activations_max}) reached"
            )
    
    # Always sync license status to website DB (non-critical, fire-and-forget)
    await sync_license_to_website(license.license_key, license.activated_at, license.expires_at, tier=license.tier, email=activation.email)

    # Find or create user
    user = db.query(User).filter(User.email == activation.email).first()
    
    if not user:
        user = User(
            email=activation.email,
            license_id=license.id,
            created_at=datetime.utcnow()
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.license_id != license.id:
        user.license_id = license.id
        db.commit()
        db.refresh(user)
    
    # Create access token
    access_token = create_access_token(str(user.id))
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=str(user.id),
        email=user.email,
        license_tier=license.tier,
        expires_at=license.expires_at
    )


@app.get("/api/user/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user information"""
    from sqlalchemy import desc

    # Find the most recently activated active license for this user
    from sqlalchemy import desc

    # Get all license IDs ever associated with this user's email
    # by finding all users with this email (should be 1) and their license_id history
    # Plus check the activation log — licenses activated by this user
    user_license_ids = [u.license_id for u in db.query(User).filter(
        User.email == current_user.email
    ).all() if u.license_id]

    # Also include any license that was activated and linked to this specific user
    activated_licenses = db.query(License).filter(
        License.id.in_(user_license_ids),
        License.status == "active",
        License.activated_at.isnot(None)
    ).order_by(desc(License.activated_at)).first()

    license = activated_licenses

    # Fallback to directly linked license
    if not license:
        license = db.query(License).filter(
            License.id == current_user.license_id
        ).first()

    return UserResponse(
        id=str(current_user.id),
        email=current_user.email,
        license_tier=license.tier if license else "unknown",
        expires_at=license.expires_at if license else None,
        created_at=current_user.created_at
    )


# ============================================================================
# LICENSE PROVISIONING (called by website Stripe webhook)
# ============================================================================

@app.post("/api/licenses/provision")
async def provision_license(
    data: LicenseProvision,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Called by the website's Stripe webhook to create a license
    in the backend database. Timer does NOT start until desktop activation.
    """
    # Verify internal secret
    secret = request.headers.get("X-Webhook-Secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")

    # Check if license already exists
    existing = db.query(License).filter(License.license_key == data.license_key).first()
    if existing:
        return {"message": "License already exists", "license_key": data.license_key}

    # Determine max activations based on tier
    tier_limits = {
        "starter": 100,
        "premium": 100,
        "pro": -1,
        "team-starter": 100,
        "team-premium": 100,
        "team-pro": -1,
    }

    # Create the license — status is "inactive" until desktop activation
    license = License(
        license_key=data.license_key,
        tier=data.tier,
        status="inactive",
        activations_max=tier_limits.get(data.tier, 100),
        activations_used=0,
        created_at=datetime.utcnow(),
        # activated_at and expires_at are NULL — set when user activates in desktop app
    )
    db.add(license)
    db.commit()
    db.refresh(license)

    return {
        "message": "License provisioned successfully",
        "license_key": data.license_key,
        "tier": data.tier,
    }


# ============================================================================
# AIRCRAFT MANAGEMENT
# ============================================================================

@app.get("/api/aircraft", response_model=List[AircraftResponse])
async def get_aircraft(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all aircraft for current user"""
    aircraft = db.query(Aircraft).filter(
        Aircraft.user_id == current_user.id,
        Aircraft.active == True
    ).all()
    
    return [
        AircraftResponse(
            id=str(a.id),
            tail_number=a.tail_number,
            icao24=a.icao24,
            friendly_name=a.friendly_name,
            active=a.active,
            created_at=a.created_at
        )
        for a in aircraft
    ]


@app.post("/api/aircraft", response_model=AircraftResponse)
async def add_aircraft(
    aircraft_data: AircraftCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Add new aircraft to track"""
    existing = db.query(Aircraft).filter(
        Aircraft.user_id == current_user.id,
        Aircraft.tail_number == aircraft_data.tail_number,
        Aircraft.active == True
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Aircraft already exists")
    
    aircraft = Aircraft(
        user_id=current_user.id,
        tail_number=aircraft_data.tail_number,
        icao24=aircraft_data.icao24,
        friendly_name=aircraft_data.friendly_name,
        active=True,
        created_at=datetime.utcnow()
    )
    
    db.add(aircraft)
    db.commit()
    db.refresh(aircraft)
    
    await tracker.update_user_aircraft(str(current_user.id), db)
    
    return AircraftResponse(
        id=str(aircraft.id),
        tail_number=aircraft.tail_number,
        icao24=aircraft.icao24,
        friendly_name=aircraft.friendly_name,
        active=aircraft.active,
        created_at=aircraft.created_at
    )


@app.delete("/api/aircraft/{aircraft_id}")
async def delete_aircraft(
    aircraft_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete aircraft"""
    aircraft = db.query(Aircraft).filter(
        Aircraft.id == aircraft_id,
        Aircraft.user_id == current_user.id
    ).first()
    
    if not aircraft:
        raise HTTPException(status_code=404, detail="Aircraft not found")
    
    db.delete(aircraft)
    db.commit()
    
    await tracker.update_user_aircraft(str(current_user.id), db)
    
    return {"message": "Aircraft deleted"}


@app.get("/api/aircraft/live", response_model=List[LiveAircraftResponse])
async def get_live_aircraft(
    current_user: User = Depends(get_current_user)
):
    """Get real-time aircraft data for current user"""
    aircraft_data = await tracker.get_live_aircraft(str(current_user.id))
    return aircraft_data


# ============================================================================
# ALERT SETTINGS
# ============================================================================

@app.get("/api/settings/alerts", response_model=List[AlertSettingResponse])
async def get_alert_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all alert settings"""
    settings = db.query(AlertSetting).filter(
        AlertSetting.user_id == current_user.id
    ).all()
    
    return [
        AlertSettingResponse(
            id=str(s.id),
            alert_type=s.alert_type,
            enabled=s.enabled,
            message_template=s.message_template,
            created_at=s.created_at
        )
        for s in settings
    ]


@app.post("/api/settings/alerts", response_model=AlertSettingResponse)
async def create_alert_setting(
    setting_data: AlertSettingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create or update alert setting"""
    existing = db.query(AlertSetting).filter(
        AlertSetting.user_id == current_user.id,
        AlertSetting.alert_type == setting_data.alert_type
    ).first()
    
    if existing:
        existing.enabled = setting_data.enabled
        existing.message_template = setting_data.message_template
        db.commit()
        db.refresh(existing)
        setting = existing
    else:
        setting = AlertSetting(
            user_id=current_user.id,
            alert_type=setting_data.alert_type,
            enabled=setting_data.enabled,
            message_template=setting_data.message_template,
            created_at=datetime.utcnow()
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)
    
    return AlertSettingResponse(
        id=str(setting.id),
        alert_type=setting.alert_type,
        enabled=setting.enabled,
        message_template=setting.message_template,
        created_at=setting.created_at
    )

# ============================================================================
# AIRPORT CONFIGURATION
# ============================================================================

@app.get("/api/airport/config")
async def get_airport_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get airport configuration for current user"""
    config = db.query(AirportConfig).filter(
        AirportConfig.user_id == current_user.id
    ).first()
    
    if not config:
        raise HTTPException(status_code=404, detail="No airport configuration found")
    
    return {
        "id": str(config.id),
        "airport_code": config.airport_code,
        "airport_name": config.airport_name,
        "latitude": config.latitude,
        "longitude": config.longitude,
        "elevation_ft_msl": config.elevation_ft_msl,
        "radius_nm": config.radius_nm,
        "floor_ft_agl": config.floor_ft_agl,
        "ceiling_ft_agl": config.ceiling_ft_agl,
        "query_radius_nm": config.query_radius_nm,
        "detection_radius_nm": config.query_radius_nm,
        "polling_interval_seconds": config.radius_nm or "10",
        "alert_distances_nm": config.alert_distances_nm,
        "quiet_hours_enabled": config.quiet_hours_enabled,
        "quiet_hours_start": config.quiet_hours_start,
        "quiet_hours_end": config.quiet_hours_end,
        "created_at": config.created_at,
        "updated_at": config.updated_at
    }


@app.post("/api/airport/config")
async def save_airport_config(
    config_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create or update airport configuration"""
    config = db.query(AirportConfig).filter(
        AirportConfig.user_id == current_user.id
    ).first()
    
    if config:
        config.airport_code = config_data.get("airport_code", config.airport_code)
        config.latitude = str(config_data.get("latitude", config.latitude))
        config.longitude = str(config_data.get("longitude", config.longitude))
        config.query_radius_nm = str(config_data.get("detection_radius_nm", config.query_radius_nm))
        config.radius_nm = str(config_data.get("polling_interval_seconds", config.radius_nm))
        config.quiet_hours_start = config_data.get("quiet_hours_start", config.quiet_hours_start)
        config.quiet_hours_end = config_data.get("quiet_hours_end", config.quiet_hours_end)
        config.updated_at = datetime.utcnow()
    else:
        config = AirportConfig(
            user_id=current_user.id,
            airport_code=config_data.get("airport_code", "KDTO"),
            latitude=str(config_data.get("latitude", "33.2001")),
            longitude=str(config_data.get("longitude", "-97.1998")),
            elevation_ft_msl=config_data.get("elevation_ft_msl", 0),
            query_radius_nm=str(config_data.get("detection_radius_nm", "100.0")),
            radius_nm=str(config_data.get("polling_interval_seconds", "10")),
            quiet_hours_start=config_data.get("quiet_hours_start", "23:00"),
            quiet_hours_end=config_data.get("quiet_hours_end", "06:00"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(config)
    
    db.commit()
    db.refresh(config)
    
    return {"message": "Configuration saved successfully", "id": str(config.id)}

# ============================================================================
# INTEGRATIONS (Discord, Slack, etc.)
# ============================================================================

@app.get("/api/integrations", response_model=List[IntegrationResponse])
async def get_integrations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all integrations"""
    integrations = db.query(Integration).filter(
        Integration.user_id == current_user.id
    ).all()
    
    return [
        IntegrationResponse(
            id=str(i.id),
            type=i.type,
            config=i.config,
            enabled=i.enabled,
            created_at=i.created_at
        )
        for i in integrations
    ]


@app.post("/api/integrations", response_model=IntegrationResponse)
async def create_integration(
    integration_data: IntegrationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create or update integration"""
    existing = db.query(Integration).filter(
        Integration.user_id == current_user.id,
        Integration.type == integration_data.type
    ).first()
    
    if existing:
        existing.config = integration_data.config
        existing.enabled = integration_data.enabled
        db.commit()
        db.refresh(existing)
        integration = existing
    else:
        integration = Integration(
            user_id=current_user.id,
            type=integration_data.type,
            config=integration_data.config,
            enabled=integration_data.enabled,
            created_at=datetime.utcnow()
        )
        db.add(integration)
        db.commit()
        db.refresh(integration)
    
    return IntegrationResponse(
        id=str(integration.id),
        type=integration.type,
        config=integration.config,
        enabled=integration.enabled,
        created_at=integration.created_at
    )


@app.post("/api/integrations/{integration_id}/test")
async def test_integration(
    integration_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Test an integration (send test notification)"""
    integration = db.query(Integration).filter(
        Integration.id == integration_id,
        Integration.user_id == current_user.id
    ).first()
    
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    
    success = await tracker.send_test_notification(integration)
    
    if success:
        return {"message": "Test notification sent successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to send test notification")


@app.delete("/api/integrations/{integration_id}")
async def delete_integration(
    integration_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an integration"""
    integration = db.query(Integration).filter(
        Integration.id == integration_id,
        Integration.user_id == current_user.id
    ).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    db.delete(integration)
    db.commit()
    return {"message": "Integration deleted"}


@app.put("/api/integrations/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    integration_id: str,
    integration_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an integration"""
    integration = db.query(Integration).filter(
        Integration.id == integration_id,
        Integration.user_id == current_user.id
    ).first()
    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")
    integration.config = integration_data.get("config", integration.config)
    integration.enabled = integration_data.get("enabled", integration.enabled)
    db.commit()
    db.refresh(integration)
    return IntegrationResponse(
        id=str(integration.id),
        type=integration.type,
        config=integration.config,
        enabled=integration.enabled,
        created_at=integration.created_at
    )


# ============================================================================
# NOTIFICATION LOGS
# ============================================================================

@app.get("/api/notifications/recent")
async def get_recent_notifications(
    limit: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get recent notification logs for current user"""
    from models import NotificationLog
    logs = db.query(NotificationLog).filter(
        NotificationLog.user_id == current_user.id
    ).order_by(NotificationLog.sent_at.desc()).limit(limit).all()

    return [
        {
            "id": str(log.id),
            "aircraft_tail": log.aircraft_tail,
            "alert_type": log.alert_type,
            "message": log.message,
            "integration_type": log.integration_type,
            "status": log.status,
            "sent_at": log.sent_at.isoformat(),
        }
        for log in logs
    ]


@app.get("/api/notifications/stats")
async def get_notification_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get notification counts for today and this week"""
    from models import NotificationLog
    from datetime import date, timedelta

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    today_count = db.query(NotificationLog).filter(
        NotificationLog.user_id == current_user.id,
        NotificationLog.sent_at >= today_start
    ).count()

    week_count = db.query(NotificationLog).filter(
        NotificationLog.user_id == current_user.id,
        NotificationLog.sent_at >= week_start
    ).count()

    total_count = db.query(NotificationLog).filter(
        NotificationLog.user_id == current_user.id
    ).count()

    return {
        "today": today_count,
        "this_week": week_count,
        "total": total_count,
    }


@app.get("/api/notifications/logs")
async def get_notification_logs(
    page: int = 1,
    limit: int = 25,
    aircraft: str = None,
    alert_type: str = None,
    integration: str = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get paginated notification logs with filters"""
    from models import NotificationLog

    query = db.query(NotificationLog).filter(
        NotificationLog.user_id == current_user.id
    )

    if aircraft:
        query = query.filter(NotificationLog.aircraft_tail == aircraft)
    if alert_type:
        query = query.filter(NotificationLog.alert_type == alert_type)
    if integration:
        query = query.filter(NotificationLog.integration_type == integration)

    total = query.count()
    pages = max(1, (total + limit - 1) // limit)
    offset = (page - 1) * limit

    logs = query.order_by(NotificationLog.sent_at.desc()).offset(offset).limit(limit).all()

    return {
        "logs": [
            {
                "id": str(log.id),
                "aircraft_tail": log.aircraft_tail,
                "alert_type": log.alert_type,
                "message": log.message,
                "integration_type": log.integration_type,
                "status": log.status,
                "sent_at": log.sent_at.isoformat(),
            }
            for log in logs
        ],
        "total": total,
        "page": page,
        "pages": pages,
    }


@app.get("/api/internal/notifications")
async def get_notifications_for_website(
    email: str,
    limit: int = 50,
    x_internal_secret: str = Header(None),
    db: Session = Depends(get_db)
):
    """Fetch notification logs for a user by email — for website use only"""
    if x_internal_secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    from models import NotificationLog
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user:
        return []

    logs = db.query(NotificationLog).filter(
        NotificationLog.user_id == user.id
    ).order_by(NotificationLog.sent_at.desc()).limit(limit).all()

    return [
        {
            "id": str(log.id),
            "aircraft_tail": log.aircraft_tail,
            "alert_type": log.alert_type,
            "message": log.message,
            "integration_type": log.integration_type,
            "status": log.status,
            "sent_at": log.sent_at.isoformat(),
        }
        for log in logs
    ]


# ============================================================================
# SAVED LOCATIONS
# ============================================================================

@app.get("/api/locations")
async def get_locations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    locations = db.query(SavedLocation).filter(
        SavedLocation.user_id == current_user.id
    ).order_by(SavedLocation.created_at).all()
    return [
        {
            "id": str(loc.id),
            "name": loc.name,
            "airport_code": loc.airport_code,
            "latitude": loc.latitude,
            "longitude": loc.longitude,
            "elevation_ft_msl": loc.elevation_ft_msl,
            "is_active": loc.is_active,
            "created_at": loc.created_at.isoformat(),
        }
        for loc in locations
    ]


@app.post("/api/locations")
async def create_location(
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Enforce tier limits
    tier = get_user_tier(current_user, db)
    limit = get_tier_limit(tier, "locations")
    if limit is not None:
        count = db.query(SavedLocation).filter(SavedLocation.user_id == current_user.id).count()
        if count >= limit:
            raise HTTPException(status_code=403, detail=f"Your {tier} plan allows up to {limit} saved location(s). Upgrade to add more.")

    # If this is the first location, make it active
    existing_count = db.query(SavedLocation).filter(SavedLocation.user_id == current_user.id).count()
    is_active = existing_count == 0

    loc = SavedLocation(
        user_id=current_user.id,
        name=data.get("name", "My Location"),
        airport_code=data.get("airport_code"),
        latitude=str(data["latitude"]),
        longitude=str(data["longitude"]),
        elevation_ft_msl=data.get("elevation_ft_msl", 0),
        is_active=is_active,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return {"id": str(loc.id), "name": loc.name, "is_active": loc.is_active}


@app.put("/api/locations/{location_id}")
async def update_location(
    location_id: str,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    loc = db.query(SavedLocation).filter(
        SavedLocation.id == location_id,
        SavedLocation.user_id == current_user.id
    ).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    for field in ["name", "airport_code", "latitude", "longitude", "elevation_ft_msl"]:
        if field in data:
            setattr(loc, field, str(data[field]) if field in ["latitude", "longitude"] else data[field])
    loc.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Updated"}


@app.post("/api/locations/{location_id}/activate")
async def activate_location(
    location_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Deactivate all locations for this user
    db.query(SavedLocation).filter(
        SavedLocation.user_id == current_user.id
    ).update({"is_active": False})
    # Activate the selected one
    loc = db.query(SavedLocation).filter(
        SavedLocation.id == location_id,
        SavedLocation.user_id == current_user.id
    ).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    loc.is_active = True

    # Also sync to AirportConfig so tracker uses it
    config = db.query(AirportConfig).filter(AirportConfig.user_id == current_user.id).first()
    if config:
        config.airport_code = loc.airport_code
        config.latitude = loc.latitude
        config.longitude = loc.longitude
        config.elevation_ft_msl = loc.elevation_ft_msl or 0
        config.updated_at = datetime.utcnow()

    db.commit()
    return {"message": f"{loc.name} is now active"}


@app.delete("/api/locations/{location_id}")
async def delete_location(
    location_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    loc = db.query(SavedLocation).filter(
        SavedLocation.id == location_id,
        SavedLocation.user_id == current_user.id
    ).first()
    if not loc:
        raise HTTPException(status_code=404, detail="Location not found")
    was_active = loc.is_active
    db.delete(loc)
    db.commit()
    # If deleted location was active, activate the next one
    if was_active:
        next_loc = db.query(SavedLocation).filter(
            SavedLocation.user_id == current_user.id
        ).first()
        if next_loc:
            next_loc.is_active = True
            db.commit()
    return {"message": "Deleted"}


# ============================================================================
# STRIPE BILLING PORTAL
# ============================================================================

@app.post("/api/billing/portal")
async def create_billing_portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a Stripe billing portal session for the current user"""
    stripe_secret = os.getenv("STRIPE_SECRET_KEY")
    if not stripe_secret:
        raise HTTPException(status_code=500, detail="Stripe not configured")

    return_url = os.getenv("WEBSITE_URL", "https://finalpingapp.com") + "/dashboard"

    async with httpx.AsyncClient() as client:
        # Look up customer by email
        search_resp = await client.get(
            "https://api.stripe.com/v1/customers/search",
            params={"query": f"email:'{current_user.email}'"},
            auth=(stripe_secret, ""),
        )
        if search_resp.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to look up Stripe customer")

        customers = search_resp.json().get("data", [])
        if not customers:
            raise HTTPException(status_code=404, detail="No Stripe customer found for this account. Please purchase a plan first.")

        customer_id = customers[0]["id"]

        # Create portal session
        portal_resp = await client.post(
            "https://api.stripe.com/v1/billing_portal/sessions",
            data={"customer": customer_id, "return_url": return_url},
            auth=(stripe_secret, ""),
        )
        if portal_resp.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to create billing portal session")

        return {"url": portal_resp.json()["url"]}


# ============================================================================
# APP VERSION CHECK
# ============================================================================

LATEST_APP_VERSION = "1.0.0"

@app.get("/api/app/version")
async def get_app_version():
    """Returns the latest desktop app version for update checking"""
    return {
        "latest_version": LATEST_APP_VERSION,
        "download_url": "https://finalpingapp.com/download",
    }


# ============================================================================
# HEALTH & STATUS
# ============================================================================

@app.get("/api/debug/licenses")
async def debug_licenses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Debug endpoint to see all license data for current user"""
    licenses = db.query(License).filter(
        License.id == current_user.license_id
    ).all()
    user = db.query(User).filter(User.id == current_user.id).first()
    return {
        "user_id": str(current_user.id),
        "user_email": current_user.email,
        "user_license_id": str(user.license_id) if user.license_id else None,
        "licenses": [
            {
                "id": str(l.id),
                "license_key": l.license_key,
                "tier": l.tier,
                "status": l.status,
                "activated_at": l.activated_at.isoformat() if l.activated_at else None,
                "expires_at": l.expires_at.isoformat() if l.expires_at else None,
            }
            for l in licenses
        ]
    }


@app.post("/api/admin/generate-license")
async def generate_license(
    request: Request,
    db: Session = Depends(get_db)
):
    """Admin endpoint to generate a license key for any tier"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    tier = body.get("tier", "starter")
    email = body.get("email", "").lower().strip()
    activations_max = body.get("activations_max", 1)

    if tier not in ["starter", "premium", "pro", "team-starter", "team-premium", "team-pro"]:
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Generate license key in XXXX-XXXX-XXXX-XXXX format
    chars = string.ascii_uppercase + string.digits
    segments = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    license_key = "-".join(segments)

    # Create in backend DB
    license = License(
        license_key=license_key,
        tier=tier,
        status="inactive",
        activations_used=0,
        activations_max=activations_max,
        created_at=datetime.utcnow()
    )
    db.add(license)
    db.commit()
    db.refresh(license)

    # Provision on website DB via existing webhook
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{WEBSITE_URL}/api/licenses/provision",
                headers={
                    "Content-Type": "application/json",
                    "X-Webhook-Secret": WEBHOOK_INTERNAL_SECRET,
                },
                json={
                    "license_key": license_key,
                    "tier": tier,
                    "email": email,
                },
                timeout=10.0
            )
    except Exception as e:
        print(f"Website provision failed (non-critical): {e}")

    return {
        "license_key": license_key,
        "tier": tier,
        "email": email,
        "status": "inactive",
        "message": "License created successfully. Share the key with the user."
    }


@app.post("/api/admin/merge-accounts")
async def merge_accounts(
    request: Request,
    db: Session = Depends(get_db)
):
    """Merge two user accounts into one — admin only"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    keep_email = body.get("keep_email", "").lower().strip()
    merge_email = body.get("merge_email", "").lower().strip()

    keep_user = db.query(User).filter(User.email == keep_email).first()
    merge_user = db.query(User).filter(User.email == merge_email).first()

    if not keep_user:
        raise HTTPException(status_code=404, detail=f"User not found: {keep_email}")
    if not merge_user:
        raise HTTPException(status_code=404, detail=f"User not found: {merge_email}")

    # Move all aircraft from merge_user to keep_user
    db.query(Aircraft).filter(Aircraft.user_id == merge_user.id).update({"user_id": keep_user.id})

    # Move all integrations
    db.query(Integration).filter(Integration.user_id == merge_user.id).update({"user_id": keep_user.id})

    # Move all alert settings
    db.query(AlertSetting).filter(AlertSetting.user_id == merge_user.id).update({"user_id": keep_user.id})

    # Move airport config if keep_user doesn't have one, otherwise delete merge user's config
    keep_config = db.query(AirportConfig).filter(AirportConfig.user_id == keep_user.id).first()
    if not keep_config:
        db.query(AirportConfig).filter(AirportConfig.user_id == merge_user.id).update({"user_id": keep_user.id})
    else:
        db.query(AirportConfig).filter(AirportConfig.user_id == merge_user.id).delete()

    # Update keep_user license to the best active license from either account
    from sqlalchemy import desc
    best_license = db.query(License).filter(
        License.id.in_([keep_user.license_id, merge_user.license_id]),
        License.status == "active"
    ).order_by(desc(License.activated_at)).first()

    if best_license:
        keep_user.license_id = best_license.id

    # Move notification logs to keep_user
    from models import NotificationLog
    db.query(NotificationLog).filter(NotificationLog.user_id == merge_user.id).update({"user_id": keep_user.id})

    # Move saved locations
    db.query(SavedLocation).filter(SavedLocation.user_id == merge_user.id).update({"user_id": keep_user.id})

    # Delete any remaining references to merge_user
    db.query(AlertSetting).filter(AlertSetting.user_id == merge_user.id).delete()

    # Flush to ensure all updates are applied before deleting user
    db.flush()

    # Delete merge_user
    db.delete(merge_user)
    db.commit()

    return {
        "message": f"Merged {merge_email} into {keep_email} successfully",
        "keep_user_id": str(keep_user.id),
        "license_id": str(keep_user.license_id) if keep_user.license_id else None,
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0"
    }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "FinalPing Cloud API",
        "version": "1.0.0",
        "docs": "/docs"
    }


# ============================================================================
# STARTUP EVENT
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Start the global aircraft tracker on startup"""
    print("🚀 Starting FinalPing Cloud Backend...")
    print("📡 Initializing global aircraft tracker...")
    await tracker.start()

    # Load all existing users into the tracker
    db = SessionLocal()
    try:
        users = db.query(User).all()
        for user in users:
            try:
                await tracker.update_user_aircraft(str(user.id), db)
            except Exception as e:
                print(f"Failed to load tracker for user {user.id}: {e}")
        print(f"✅ Loaded {len(users)} users into tracker")
    except Exception as e:
        print(f"Error loading users on startup: {e}")
    finally:
        db.close()

    print("✅ FinalPing Cloud Backend ready!")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    print("🛑 Shutting down FinalPing Cloud Backend...")
    await tracker.stop()
    print("✅ Shutdown complete")
