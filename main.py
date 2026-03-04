"""
FinalPing Cloud Backend
Main FastAPI application
"""

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from pydantic import BaseModel
import jwt
import os
import httpx
from typing import List, Optional

from database import get_db, engine, Base
from models import User, License, Aircraft, AlertSetting, Integration, AirportConfig
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


async def sync_license_to_website(license_key: str, activated_at: datetime, expires_at: datetime):
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
    await sync_license_to_website(license.license_key, license.activated_at, license.expires_at)

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
    license = db.query(License).filter(License.id == current_user.license_id).first()
    
    return UserResponse(
        id=str(current_user.id),
        email=current_user.email,
        license_tier=license.tier if license else "unknown",
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
        Aircraft.tail_number == aircraft_data.tail_number
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
    
    aircraft.active = False
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
        "detection_radius_nm": config.query_radius_nm,  # alias for frontend
        "polling_interval_seconds": getattr(config, 'polling_interval_seconds', 10),
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
        if hasattr(config, 'polling_interval_seconds') and config_data.get("polling_interval_seconds"):
            config.polling_interval_seconds = int(config_data.get("polling_interval_seconds"))
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
    print("✅ FinalPing Cloud Backend ready!")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    print("🛑 Shutting down FinalPing Cloud Backend...")
    await tracker.stop()
    print("✅ Shutdown complete")
