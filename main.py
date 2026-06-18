"""
FinalPing Cloud Backend
Main FastAPI application
"""

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timedelta
from pydantic import BaseModel
import jwt
import os
import httpx
import secrets
import string
import logging
import uuid
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("finalpingapp")

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

sentry_sdk.init(
    dsn="https://671a1631ac770069f122a26298e33a6c@o4511365849874432.ingest.us.sentry.io/4511365869928448",
    integrations=[FastApiIntegration(), SqlalchemyIntegration()],
    traces_sample_rate=0.2,
    send_default_pii=False,
)

from database import get_db, engine, Base, SessionLocal
from models import User, License, Aircraft, AlertSetting, Integration, AirportConfig, SavedLocation, Team, TeamMember, TeamChannel, TeamAircraft, TeamAirportConfig, TeamAlertSetting, TeamInviteToken, TeamRole, AircraftClaim, TeamShift, TeamShiftMember, TeamDutyOverride, ExpectedArrival, EscalationConfig, AlertEscalation
from schemas import (
    LicenseActivation, LicenseResponse,
    UserLogin, UserResponse, TokenResponse,
    AircraftCreate, AircraftUpdate, AircraftResponse,
    AlertSettingCreate, AlertSettingResponse,
    IntegrationCreate, IntegrationResponse,
    LiveAircraftResponse,
    TeamChannelCreate, TeamChannelResponse, TeamMemberResponse,
    TeamResponse, TeamRoutingUpdate, TeamInviteRequest, TeamActivityResponse,
    TeamInviteCreate, TeamActivateInviteRequest, TeamRoleCreate, TeamRoleUpdate, AssignMemberRoleRequest
)
from tracker import CloudAircraftTracker

# Create database tables
Base.metadata.create_all(bind=engine)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize FastAPI app
app = FastAPI(
    title="FinalPing Cloud API",
    description="Real-time aircraft tracking and notifications",
    version="1.0.6"
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware (allow desktop app and web app to connect)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://finalpingapp.com",
        "https://www.finalpingapp.com",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY environment variable is not set")
ALGORITHM = "HS256"
WEBHOOK_INTERNAL_SECRET = os.getenv("WEBHOOK_INTERNAL_SECRET", "finalping-internal-secret")

# License duration
LICENSE_DURATION_DAYS = 30

# Grace period after expiry before blocking access — gives the Stripe renewal
# webhook time to arrive and extend the license before users go dark.
LICENSE_GRACE_PERIOD = timedelta(hours=48)

import ground_state as _gs

# Website URL for syncing license status
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://finalpingapp.com")

# Tier feature limits (None = unlimited)
TIER_LIMITS = {
    "starter":      {"aircraft": 3,    "zones": 2,  "locations": 1,    "integrations": 1,    "channels": ["discord", "email"]},
    "premium":      {"aircraft": 7,    "zones": 5,  "locations": 5,    "integrations": 3,    "channels": ["discord", "email", "slack", "teams", "google_chat"]},
    "pro":          {"aircraft": 15,   "zones": 7,  "locations": None, "integrations": 5,    "channels": ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"]},
    "team-starter": {"aircraft": 25,   "locations": 3,    "integrations": 3,    "channels": ["discord", "email"]},
    "team-premium": {"aircraft": 75,   "locations": 10,   "integrations": 10,   "channels": ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"]},
    "team-pro":     {"aircraft": None, "locations": None, "integrations": None, "channels": ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"]},
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
        logger.warning("Website license sync failed (non-critical): %s", e)

# Global tracker instance (runs 24/7)
tracker = CloudAircraftTracker()


# Schema for license provisioning
class LicenseProvision(BaseModel):
    license_key: str
    tier: str
    email: str
    stripe_subscription_id: str = None


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
    """Verify JWT token or GS device key and return current user"""
    token = credentials.credentials

    # GS device keys are 64-char hex strings — try that path first (no JWT decode needed)
    if len(token) == 64 and all(c in '0123456789abcdef' for c in token.lower()):
        user = db.query(User).filter(User.gs_device_key == token).first()
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid device key")
        if not getattr(user, 'ground_station_enabled', False):
            raise HTTPException(status_code=403, detail="ground_station_not_enabled")
        return user

    try:
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

    # Check license expiry — free accounts (no license) are allowed through
    if user.license_id:
        license = db.query(License).filter(License.id == user.license_id).first()
        if license and license.expires_at and license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow():
            raise HTTPException(status_code=401, detail="license_expired")

    return user


@app.post("/api/auth/refresh")
@limiter.limit("20/minute")
async def refresh_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    """Refresh an expired JWT token — allows tokens expired within the last 7 days"""
    try:
        token = credentials.credentials
        # Decode WITHOUT verifying expiration — we want to accept recently expired tokens
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], options={"verify_exp": False})
        user_id: str = payload.get("sub")
        exp = payload.get("exp", 0)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # Only allow refresh if token expired within the last 7 days
        expired_at = datetime.utcfromtimestamp(exp)
        if datetime.utcnow() - expired_at > timedelta(days=7):
            raise HTTPException(status_code=401, detail="Token too old to refresh — please log in again")
        
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    # Verify user still exists and license is valid
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    
    if user.license_id:
        license = db.query(License).filter(License.id == user.license_id).first()
        if license and license.expires_at and license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow():
            raise HTTPException(status_code=401, detail="license_expired")

    # Issue a fresh token
    new_token = create_access_token(str(user.id))
    return {"access_token": new_token, "token_type": "bearer"}


@app.post("/api/auth/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    credentials: UserLogin,
    db: Session = Depends(get_db)
):
    """
    Login with website email + password.
    Verifies credentials against Vercel/Prisma via internal API call,
    then issues a JWT token for the desktop app.
    """
    import aiohttp
    import os

    website_url = os.environ.get("WEBSITE_URL", "https://finalpingapp.com")
    internal_secret = os.environ.get("WEBHOOK_INTERNAL_SECRET", "")

    # Step 1 — Verify credentials with Vercel
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{website_url}/api/auth/verify",
                json={"email": credentials.email.lower(), "password": credentials.password},
                headers={"x-internal-secret": internal_secret},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 401:
                    raise HTTPException(status_code=401, detail="Invalid email or password")
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail="Could not verify credentials. Please try again.")
                verified = await resp.json()
    except aiohttp.ClientError:
        raise HTTPException(status_code=502, detail="Could not reach verification service. Please check your connection.")

    if not verified.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    email = credentials.email.lower().strip()

    # Step 2 — Find user in Railway DB by email
    user = db.query(User).filter(User.email == email).first()

    # If no Railway account exists, auto-create a free-tier account
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Auto-created free account for %s", email)

    # Step 3 — Get license info (may not exist for free accounts)
    license = db.query(License).filter(License.id == user.license_id).first() if user.license_id else None

    # Determine tier — free if no license
    tier = "free"
    expires_at = None
    if license:
        if license.status == "expired" or (license.expires_at and license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow()):
            raise HTTPException(status_code=401, detail="license_expired")
        tier = license.tier
        expires_at = license.expires_at

    # Step 4 — Issue JWT token
    access_token = create_access_token(str(user.id))

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=str(user.id),
        email=user.email,
        license_tier=tier,
        expires_at=expires_at,
    )


@app.post("/api/auth/google-desktop", response_model=TokenResponse)
@limiter.limit("10/minute")
async def google_desktop_login(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Desktop Google OAuth login. Verifies the short-lived desktop token
    issued by the website OAuth callback, then issues a Railway JWT.
    """
    import aiohttp
    import os

    body = await request.json()
    token = body.get("token", "").strip()
    email = body.get("email", "").strip().lower()

    if not token or not email:
        raise HTTPException(status_code=400, detail="Missing token or email")

    website_url = os.environ.get("WEBSITE_URL", "https://finalpingapp.com")
    internal_secret = os.environ.get("WEBHOOK_INTERNAL_SECRET", "")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{website_url}/api/auth/google/desktop-verify",
                json={"token": token, "email": email},
                headers={"x-internal-secret": internal_secret},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=401, detail="Invalid or expired OAuth token")
                verified = await resp.json()
    except aiohttp.ClientError:
        raise HTTPException(status_code=502, detail="Could not reach verification service.")

    if not verified.get("valid"):
        raise HTTPException(status_code=401, detail="Invalid or expired OAuth token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email)
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Auto-created account for Google OAuth user %s", email)

    license = db.query(License).filter(License.id == user.license_id).first() if user.license_id else None
    tier = "free"
    expires_at = None
    if license:
        if license.status == "expired" or (license.expires_at and license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow()):
            raise HTTPException(status_code=401, detail="license_expired")
        tier = license.tier
        expires_at = license.expires_at

    access_token = create_access_token(str(user.id))
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=str(user.id),
        email=user.email,
        license_tier=tier,
        expires_at=expires_at,
    )


@app.post("/api/ground/ingest")
async def ground_ingest(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Receives alert data from a user's local FinalPing Ground Station.
    Processes the alert through their configured notification integrations
    and logs it — exactly like the cloud tracker does.
    """
    from models import NotificationLog, Integration, AlertSetting

    # Check ground station access
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")

    body = await request.json()
    alert_type = body.get("type")
    tail = body.get("tail", "Unknown")
    distance = body.get("distance", 0)
    altitude = body.get("altitude", 0)
    eta = body.get("eta", 0)
    speed = body.get("speed", 0)

    if not alert_type:
        raise HTTPException(status_code=400, detail="Missing alert type")

    integrations = db.query(Integration).filter(
        Integration.user_id == current_user.id,
        Integration.enabled == True
    ).all()

    if not integrations:
        return {"message": "No integrations configured", "alerts_sent": 0}

    alert_settings = {
        s.alert_type: s.message_template
        for s in db.query(AlertSetting).filter(AlertSetting.user_id == current_user.id).all()
    }

    default_templates = {
        "landing": "🛬 **{tail} has landed** — Ground station confirmed touchdown",
        "takeoff": "🛫 **{tail} is airborne** — Departed at {speed}kts",
        "10nm":    "✈️ **{tail} - 10nm out** ETA ~{eta}min, Alt {altitude}ft MSL",
        "5nm":     "⚠️ **{tail} - 5nm out** ETA ~{eta}min, Alt {altitude}ft MSL",
        "2nm":     "🔴 **{tail} - 2nm out** ETA ~{eta}min, Alt {altitude}ft MSL",
    }
    template = alert_settings.get(alert_type, default_templates.get(alert_type, "✈️ **{tail}** — {type} alert"))
    airport_cfg = db.query(AirportConfig).filter(AirportConfig.user_id == current_user.id).first()
    airport_code = airport_cfg.airport_code if airport_cfg else ""
    tail_number = tail
    try:
        message = template.format(
            tail=tail, tail_number=tail_number,
            distance=f"{float(distance):.1f}",
            altitude=f"{float(altitude):.0f}", eta=eta,
            speed=f"{float(speed):.0f}", type=alert_type,
            time=datetime.utcnow().strftime('%H:%M'),
            airport=airport_code,
        )
    except Exception:
        message = f"✈️ {tail} — {alert_type} (Ground Station)"

    alerts_sent = 0
    for integration in integrations:
        try:
            success = await tracker.send_via_integration(integration, message)
            log_entry = NotificationLog(
                user_id=current_user.id,
                aircraft_tail=tail,
                alert_type=alert_type,
                message=message,
                integration_type=integration.type,
                status="sent" if success else "failed",
                sent_at=datetime.utcnow(),
            )
            db.add(log_entry)
            if success:
                alerts_sent += 1
        except Exception as e:
            logger.error("Ground ingest send error: %s", e)

    db.commit()

    _gs.ground_last_seen[str(current_user.id)] = datetime.utcnow()

    return {
        "message": f"Alert processed — {alerts_sent}/{len(integrations)} notifications sent",
        "alert_type": alert_type,
        "tail": tail,
        "alerts_sent": alerts_sent,
    }


@app.post("/api/ground/validate")
async def ground_validate(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Called by FinalPing Ground Station on startup.
    Returns whether this account has ground station access enabled.
    """
    enabled = getattr(current_user, 'ground_station_enabled', False)
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail="ground_station_not_enabled"
        )
    return {
        "enabled": True,
        "email": current_user.email,
        "message": "Ground station access confirmed",
    }


@app.get("/api/ground/config")
async def ground_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns the user's location and tracked aircraft for the ground station.
    Called on startup so credentials + config are never baked into the script.
    """
    from models import AirportConfig, Aircraft
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")

    airport = db.query(AirportConfig).filter(AirportConfig.user_id == current_user.id).first()
    if not airport:
        raise HTTPException(
            status_code=404,
            detail="No location configured. Set up your airport location in the FinalPing app first."
        )

    aircraft = db.query(Aircraft).filter(
        Aircraft.user_id == current_user.id,
        Aircraft.active == True
    ).all()

    return {
        "lat": float(airport.latitude),
        "lon": float(airport.longitude),
        "elevation_ft": airport.elevation_ft_msl,
        "aircraft": [
            {"tail": a.tail_number, "icao24": a.icao24 or ""}
            for a in aircraft
        ],
    }


@app.post("/api/ground/heartbeat")
async def ground_heartbeat(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Called by the ground station every minute to signal it is online."""
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")
    now = datetime.utcnow()
    _gs.ground_last_seen[str(current_user.id)] = now
    current_user.gs_last_heartbeat = now
    db.commit()
    return {"ok": True}


@app.get("/api/ground/status")
async def ground_status(current_user: User = Depends(get_current_user)):
    """Returns whether this user's ground station is currently online."""
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")
    last_seen = _gs.ground_last_seen.get(str(current_user.id))
    online = _gs.is_ground_station_online(str(current_user.id))
    return {
        "online": online,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "gs_device_key": getattr(current_user, 'gs_device_key', None),
    }


@app.post("/api/ground/range")
async def ground_range_post(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Receives SDR reception range data (36 buckets, one per 10-degree bearing)."""
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")
    body = await request.json()
    range_nm = body.get("range_nm", [])
    if not isinstance(range_nm, list) or len(range_nm) != 36:
        raise HTTPException(status_code=400, detail="range_nm must be a list of 36 floats")
    updated_at = datetime.utcnow()
    _gs.ground_range[str(current_user.id)] = {
        "range_nm": range_nm,
        "updated_at": updated_at.isoformat(),
    }
    try:
        airport_cfg = db.query(AirportConfig).filter(AirportConfig.user_id == current_user.id).first()
        if airport_cfg:
            airport_cfg.sdr_range_nm = range_nm
            airport_cfg.sdr_range_updated_at = updated_at
            db.commit()
    except Exception as e:
        logger.warning("Failed to persist range to DB: %s", e)
        db.rollback()
    return {"ok": True}


@app.get("/api/ground/range")
async def ground_range_get(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the latest SDR reception range polygon for this user."""
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")
    data = _gs.ground_range.get(str(current_user.id))
    if not data:
        # Fall back to DB (survives backend restarts)
        airport_cfg = db.query(AirportConfig).filter(AirportConfig.user_id == current_user.id).first()
        if airport_cfg and airport_cfg.sdr_range_nm:
            data = {
                "range_nm": airport_cfg.sdr_range_nm,
                "updated_at": airport_cfg.sdr_range_updated_at.isoformat() if airport_cfg.sdr_range_updated_at else None,
            }
            _gs.ground_range[str(current_user.id)] = data
    if not data:
        return {"range_nm": None, "updated_at": None}
    return data


@app.post("/api/ground/positions")
async def ground_positions_post(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Receives live aircraft positions from the ground station for the live map."""
    if not getattr(current_user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="ground_station_not_enabled")
    body = await request.json()
    positions = body.get("positions", {})
    if isinstance(positions, dict):
        _gs.ground_positions[str(current_user.id)] = positions
    return {"ok": True}


@app.post("/api/ground/claim")
@limiter.limit("10/minute")
async def ground_claim(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Called by a Pi on first boot after connecting to WiFi.
    Takes the user's email and returns their GS device key if GS is enabled.
    No auth required — the device key is the credential returned, not input.
    """
    body = await request.json()
    email = body.get("email", "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")

    user = db.query(User).filter(User.email == email).first()
    if not user or not getattr(user, 'ground_station_enabled', False):
        raise HTTPException(status_code=403, detail="Ground station not enabled for this account")

    if not user.gs_device_key:
        import secrets
        user.gs_device_key = secrets.token_hex(32)
        db.commit()

    return {"gs_device_key": user.gs_device_key}


@app.post("/api/admin/grant-ground-station")
async def grant_ground_station(
    request: Request,
    db: Session = Depends(get_db)
):
    """Admin endpoint to manually grant ground station access to a user"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    email = body.get("email", "").lower().strip()
    enabled = body.get("enabled", True)

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.ground_station_enabled = enabled

    # Generate a permanent device key when enabling GS for the first time
    if enabled and not user.gs_device_key:
        import secrets
        user.gs_device_key = secrets.token_hex(32)

    db.commit()

    return {
        "email": email,
        "ground_station_enabled": enabled,
        "gs_device_key": user.gs_device_key if enabled else None,
        "message": f"Ground station {'enabled' if enabled else 'disabled'} for {email}",
    }


@app.post("/api/activate", response_model=TokenResponse)
@limiter.limit("10/minute")
async def activate_license(
    request: Request,
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

    # Enforce key prefix matches license tier
    key_upper = activation.license_key.upper()
    is_team_key = key_upper.startswith("FPT-")
    is_team_tier = license.tier.startswith("team-")
    if is_team_key and not is_team_tier:
        raise HTTPException(status_code=400, detail="This is a Teams license key. Please use FinalPing for Teams.")
    if not is_team_key and is_team_tier and (key_upper.startswith("FP-") or not key_upper[0].isalpha()):
        raise HTTPException(status_code=400, detail="This is a personal license key. Please use the personal FinalPing app.")

    # Check if expired
    if license.status == "expired":
        raise HTTPException(status_code=403, detail="License has expired")
    
    if license.expires_at and license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow():
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

        # Unpause the Stripe subscription and use its current_period_end as
        # the authoritative expiry date. We fetch raw JSON via httpx to avoid
        # stripe-python SDK attribute issues with newer API versions.
        if license.stripe_subscription_id:
            try:
                import stripe as stripe_lib
                stripe_key = os.getenv("STRIPE_SECRET_KEY")
                if stripe_key:
                    stripe_lib.api_key = stripe_key
                    # Unpause via SDK
                    stripe_lib.Subscription.modify(
                        license.stripe_subscription_id,
                        pause_collection="",
                    )
                    # Fetch raw subscription JSON using a stable API version
                    async with httpx.AsyncClient() as client:
                        r = await client.get(
                            f"https://api.stripe.com/v1/subscriptions/{license.stripe_subscription_id}",
                            headers={
                                "Authorization": f"Bearer {stripe_key}",
                                "Stripe-Version": "2023-10-16",
                            },
                        )
                        sub_data = r.json()
                    logger.debug("Stripe sub keys: %s", list(sub_data.keys()))
                    period_end = sub_data.get("current_period_end")
                    logger.debug("Stripe raw period_end: %s", period_end)
                    if period_end and period_end > datetime.utcnow().timestamp():
                        license.expires_at = datetime.utcfromtimestamp(period_end)
                        db.commit()
                        db.refresh(license)
                        logger.info("expires_at set to %s from Stripe", license.expires_at)
                    else:
                        logger.warning("Stripe period_end unusable (%s), keeping +30 days fallback", period_end)
            except Exception as e:
                logger.error("Stripe error during activation: %s", e)
    elif not license.expires_at:
        # Already activated but expires_at is missing — always set to at least now+30d
        from_activated = license.activated_at + timedelta(days=LICENSE_DURATION_DAYS)
        license.expires_at = max(from_activated, datetime.utcnow() + timedelta(days=LICENSE_DURATION_DAYS))
        license.status = "active"
        db.commit()
        db.refresh(license)
    elif license.status != "active":
        # Was provisioned but not yet marked active — also renew if expiry is stale
        if license.expires_at < datetime.utcnow() + timedelta(days=1):
            license.expires_at = datetime.utcnow() + timedelta(days=LICENSE_DURATION_DAYS)
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
    elif license.tier.startswith("team-"):
        # For team licenses: only set license_id if user has no existing personal license.
        # Team access is managed via TeamMember — we don't want to overwrite a personal
        # license with the team license, as that would corrupt the personal app's tier display.
        existing_license = db.query(License).filter(License.id == user.license_id).first() if user.license_id else None
        if not existing_license or existing_license.tier.startswith("team-"):
            # No personal license (or already has a team license) — safe to link team license
            if user.license_id != license.id:
                user.license_id = license.id
                db.commit()
                db.refresh(user)
        # If user has a personal license, leave user.license_id alone — team access via TeamMember
    elif user.license_id != license.id:
        user.license_id = license.id
        db.commit()
        db.refresh(user)
    
    # For team tiers: create or join the team linked to this license
    if license.tier.startswith("team-"):
        team = db.query(Team).filter(Team.license_id == license.id).first()
        if not team:
            team = Team(license_id=license.id)
            db.add(team)
            db.commit()
            db.refresh(team)
            db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
            db.commit()
        else:
            existing = db.query(TeamMember).filter(
                TeamMember.team_id == team.id,
                TeamMember.user_id == user.id
            ).first()
            if not existing:
                db.add(TeamMember(team_id=team.id, user_id=user.id, role="member"))
                db.commit()

    # Auto-enable Ground Station for Pro license holders
    if license.tier == "pro":
        if not getattr(user, 'ground_station_enabled', False):
            user.ground_station_enabled = True
        if not user.gs_device_key:
            import secrets
            user.gs_device_key = secrets.token_hex(32)
        db.commit()

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

    # Find the most recently activated personal (non-team) license for this user.
    # Team licenses are excluded here — team access is via TeamMember, and the personal
    # app should display the user's personal tier, not the team tier.
    from sqlalchemy import desc

    user_license_ids = [u.license_id for u in db.query(User).filter(
        User.email == current_user.email
    ).all() if u.license_id]

    # Prefer personal (non-team) licenses
    license = db.query(License).filter(
        License.id.in_(user_license_ids),
        License.status == "active",
        License.activated_at.isnot(None),
        ~License.tier.startswith("team-")
    ).order_by(desc(License.activated_at)).first()

    # Fallback: directly linked license (could be team tier)
    if not license and current_user.license_id:
        license = db.query(License).filter(
            License.id == current_user.license_id
        ).first()

    # If the only license we found is a team license, report the tier as "starter"
    # so the personal app renders correct personal-tier limits.
    display_tier = "starter"
    if license:
        if license.tier.startswith("team-"):
            display_tier = "starter"
        else:
            display_tier = license.tier

    # Team membership info — independent of personal license
    team_membership = db.query(TeamMember).filter(TeamMember.user_id == current_user.id).first()
    has_team = team_membership is not None
    team_id = None
    team_name = None
    team_role = None
    team_license_valid = False
    if team_membership:
        team_obj = db.query(Team).filter(Team.id == team_membership.team_id).first()
        if team_obj:
            team_id = str(team_obj.id)
            team_name = team_obj.name
            team_role = team_membership.role
            if team_obj.license_id:
                tl = db.query(License).filter(License.id == team_obj.license_id).first()
                if tl and (not tl.expires_at or tl.expires_at + LICENSE_GRACE_PERIOD > datetime.utcnow()):
                    team_license_valid = True

    return UserResponse(
        id=str(current_user.id),
        email=current_user.email,
        license_tier=display_tier,
        activated_at=license.activated_at if license else None,
        expires_at=license.expires_at if license and not license.tier.startswith("team-") else None,
        created_at=current_user.created_at,
        has_team=has_team,
        team_id=team_id,
        team_name=team_name,
        team_role=team_role,
        team_license_valid=team_license_valid,
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
        stripe_subscription_id=data.stripe_subscription_id,
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


@app.post("/api/licenses/renew")
async def renew_license(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Called by the website's Stripe webhook when a subscription renews.
    Updates expires_at on the existing active license.
    """
    secret = request.headers.get("X-Webhook-Secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")

    body = await request.json()
    license_key = body.get("license_key")
    expires_at_str = body.get("expires_at")

    if not license_key or not expires_at_str:
        raise HTTPException(status_code=400, detail="Missing license_key or expires_at")

    license = db.query(License).filter(License.license_key == license_key).first()
    if not license:
        raise HTTPException(status_code=404, detail="License not found")

    new_expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).replace(tzinfo=None)

    # Out-of-order webhook protection — never shorten an existing expiry
    if license.expires_at and new_expires_at <= license.expires_at:
        logger.warning("Renewal ignored (out-of-order): %s new=%s existing=%s", license_key, new_expires_at, license.expires_at)
        return {"message": "Renewal ignored (out-of-order)", "license_key": license_key}

    license.expires_at = new_expires_at
    license.status = "active"
    db.commit()

    logger.info("License renewed: %s until %s", license_key, expires_at_str)
    return {"message": "License renewed", "license_key": license_key, "expires_at": expires_at_str}


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
            aircraft_type=a.aircraft_type,
            alert_distances=a.alert_distances,
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
    tier = get_user_tier(current_user, db)
    limit = get_tier_limit(tier, "aircraft")
    if limit is not None:
        count = db.query(Aircraft).filter(Aircraft.user_id == current_user.id, Aircraft.active == True).count()
        if count >= limit:
            raise HTTPException(status_code=403, detail=f"Your {tier} plan allows up to {limit} aircraft. Upgrade to add more.")

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
        aircraft_type=aircraft_data.aircraft_type,
        alert_distances=aircraft_data.alert_distances,
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
        aircraft_type=aircraft.aircraft_type,
        alert_distances=aircraft.alert_distances,
        active=aircraft.active,
        created_at=aircraft.created_at
    )


@app.put("/api/aircraft/{aircraft_id}", response_model=AircraftResponse)
async def update_aircraft(
    aircraft_id: str,
    aircraft_data: AircraftUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an existing aircraft"""
    aircraft = db.query(Aircraft).filter(
        Aircraft.id == aircraft_id,
        Aircraft.user_id == current_user.id,
        Aircraft.active == True
    ).first()

    if not aircraft:
        raise HTTPException(status_code=404, detail="Aircraft not found")

    if aircraft_data.tail_number is not None:
        aircraft.tail_number = aircraft_data.tail_number
    if aircraft_data.icao24 is not None:
        aircraft.icao24 = aircraft_data.icao24
    if aircraft_data.friendly_name is not None:
        aircraft.friendly_name = aircraft_data.friendly_name
    if aircraft_data.aircraft_type is not None:
        aircraft.aircraft_type = aircraft_data.aircraft_type
    if aircraft_data.alert_distances is not None:
        aircraft.alert_distances = aircraft_data.alert_distances

    db.commit()
    db.refresh(aircraft)

    await tracker.update_user_aircraft(str(current_user.id), db)

    return AircraftResponse(
        id=str(aircraft.id),
        tail_number=aircraft.tail_number,
        icao24=aircraft.icao24,
        friendly_name=aircraft.friendly_name,
        aircraft_type=aircraft.aircraft_type,
        alert_distances=aircraft.alert_distances,
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
        "runway_info": config.runway_info,
        "approach_corridor_enabled": config.approach_corridor_enabled,
        "approach_runway_heading": config.approach_runway_heading,
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
    # Enforce approach zone limit
    if "alert_distances_nm" in config_data:
        tier = get_user_tier(current_user, db)
        zone_limit = get_tier_limit(tier, "zones")
        if zone_limit is not None and len(config_data["alert_distances_nm"]) > zone_limit:
            raise HTTPException(status_code=403, detail=f"Your {tier} plan allows up to {zone_limit} approach zones. Upgrade to add more.")

    # Auto-lookup elevation from coordinates if not provided
    lat = config_data.get("latitude")
    lon = config_data.get("longitude")
    elevation = config_data.get("elevation_ft_msl")

    if lat and lon and (not elevation or elevation == 0):
        try:
            import httpx
            resp = httpx.get(f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}", timeout=5)
            if resp.status_code == 200:
                elev_meters = resp.json().get("elevation", [0])[0]
                elevation = int(elev_meters * 3.28084)  # convert meters to feet
                logger.info("Auto-detected elevation: %dft MSL for %s,%s", elevation, lat, lon)
        except Exception as e:
            logger.warning("Failed to auto-detect elevation: %s", e)
            elevation = config_data.get("elevation_ft_msl", 0)

    config = db.query(AirportConfig).filter(
        AirportConfig.user_id == current_user.id
    ).first()
    
    if config:
        config.airport_code = config_data.get("airport_code", config.airport_code)
        config.latitude = str(config_data.get("latitude", config.latitude))
        config.longitude = str(config_data.get("longitude", config.longitude))
        if elevation:
            config.elevation_ft_msl = elevation
        config.query_radius_nm = str(config_data.get("detection_radius_nm", config.query_radius_nm))
        config.radius_nm = str(config_data.get("polling_interval_seconds", config.radius_nm))
        config.quiet_hours_enabled = config_data.get("quiet_hours_enabled", config.quiet_hours_enabled)
        config.quiet_hours_start = config_data.get("quiet_hours_start", config.quiet_hours_start)
        config.quiet_hours_end = config_data.get("quiet_hours_end", config.quiet_hours_end)
        if "alert_distances_nm" in config_data:
            config.alert_distances_nm = [str(d) for d in config_data["alert_distances_nm"]]
        if "runway_info" in config_data:
            config.runway_info = config_data["runway_info"]
        if "approach_corridor_enabled" in config_data:
            config.approach_corridor_enabled = config_data["approach_corridor_enabled"]
        if "approach_runway_heading" in config_data:
            config.approach_runway_heading = config_data["approach_runway_heading"]
        config.airport_name = config_data.get("airport_name", config.airport_name)
        config.updated_at = datetime.utcnow()
    else:
        config = AirportConfig(
            user_id=current_user.id,
            airport_code=config_data.get("airport_code", "KDTO"),
            airport_name=config_data.get("airport_name", ""),
            latitude=str(config_data.get("latitude", "33.2001")),
            longitude=str(config_data.get("longitude", "-97.1998")),
            elevation_ft_msl=elevation or config_data.get("elevation_ft_msl", 0),
            query_radius_nm=str(config_data.get("detection_radius_nm", "100.0")),
            radius_nm=str(config_data.get("polling_interval_seconds", "10")),
            alert_distances_nm=[str(d) for d in config_data.get("alert_distances_nm", [10.0, 5.0, 2.0])],
            runway_info=config_data.get("runway_info", []),
            approach_corridor_enabled=config_data.get("approach_corridor_enabled", False),
            approach_runway_heading=config_data.get("approach_runway_heading"),
            quiet_hours_enabled=config_data.get("quiet_hours_enabled", False),
            quiet_hours_start=config_data.get("quiet_hours_start", "23:00"),
            quiet_hours_end=config_data.get("quiet_hours_end", "06:00"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(config)
    
    db.commit()
    db.refresh(config)

    # Clean up orphaned alert settings for distances that were removed
    if "alert_distances_nm" in config_data:
        try:
            valid_types = {f"{int(float(d))}nm" if float(d) == int(float(d)) else f"{float(d)}nm"
                          for d in config_data["alert_distances_nm"]}
            valid_types.add("landing")  # never delete landing alerts
            existing_settings = db.query(AlertSetting).filter(
                AlertSetting.user_id == current_user.id
            ).all()
            for setting in existing_settings:
                if setting.alert_type not in valid_types:
                    db.delete(setting)
            db.commit()
        except Exception as e:
            logger.warning("Failed to clean up orphaned alert settings: %s", e)

    # Reload the user's tracker so it picks up new distances immediately
    try:
        await tracker.update_user_aircraft(str(current_user.id), db)
    except Exception as e:
        logger.error("Failed to reload tracker after config update: %s", e)

    return {"message": "Configuration saved successfully", "id": str(config.id)}


@app.delete("/api/airport/config")
async def delete_airport_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete airport configuration for current user"""
    config = db.query(AirportConfig).filter(
        AirportConfig.user_id == current_user.id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="No airport configuration found")

    db.delete(config)
    db.commit()
    return {"message": "Airport configuration deleted"}

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

    if not existing:
        tier = get_user_tier(current_user, db)
        allowed_channels = TIER_LIMITS.get(tier, TIER_LIMITS["starter"])["channels"]
        if integration_data.type not in allowed_channels:
            raise HTTPException(status_code=403, detail=f"Your {tier} plan does not include {integration_data.type} notifications. Upgrade to unlock.")
        limit = get_tier_limit(tier, "integrations")
        if limit is not None:
            count = db.query(Integration).filter(Integration.user_id == current_user.id).count()
            if count >= limit:
                raise HTTPException(status_code=403, detail=f"Your {tier} plan allows up to {limit} integrations. Upgrade to add more.")

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
@limiter.limit("5/minute")
async def test_integration(
    integration_id: str,
    request: Request,
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

LATEST_APP_VERSION = "1.0.8"

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


@app.post("/api/admin/user-logs")
async def admin_get_user_logs(
    request: Request,
    db: Session = Depends(get_db)
):
    """Return all notification logs for a user by email — used by website dashboard"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    email = body.get("email", "").lower().strip()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return []

    from models import NotificationLog
    logs = db.query(NotificationLog).filter(
        NotificationLog.user_id == user.id
    ).order_by(NotificationLog.sent_at.desc()).limit(500).all()

    return [{
        "id": str(l.id),
        "aircraft_tail": l.aircraft_tail,
        "alert_type": l.alert_type,
        "message": l.message,
        "integration_type": l.integration_type,
        "status": l.status,
        "sent_at": l.sent_at.isoformat(),
    } for l in logs]


@app.post("/api/admin/user-aircraft")
async def admin_get_user_aircraft(
    request: Request,
    db: Session = Depends(get_db)
):
    """Return all aircraft for a user by email — used by website dashboard filters"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    email = body.get("email", "").lower().strip()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return []

    aircraft = db.query(Aircraft).filter(Aircraft.user_id == user.id, Aircraft.active == True).all()
    return [{"id": str(a.id), "tail_number": a.tail_number, "icao24": a.icao24, "friendly_name": a.friendly_name} for a in aircraft]


@app.post("/api/admin/user-integrations")
async def admin_get_user_integrations(
    request: Request,
    db: Session = Depends(get_db)
):
    """Return all integrations for a user by email — used by website dashboard filters"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    email = body.get("email", "").lower().strip()

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return []

    integrations = db.query(Integration).filter(Integration.user_id == user.id).all()
    return [{"id": str(i.id), "type": i.type, "enabled": i.enabled} for i in integrations]


@app.post("/api/licenses/{license_key}/expire")
async def expire_license(
    license_key: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Deactivate a license — called when a Stripe subscription is cancelled or a trial ends."""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    license = db.query(License).filter(License.license_key == license_key).first()
    if not license:
        raise HTTPException(status_code=404, detail="License not found")

    license.status = "inactive"
    license.expires_at = datetime.utcnow()
    db.commit()

    # Stop tracking and revoke GS access for any user on this license
    try:
        user = db.query(User).filter(User.license_id == license.id).first()
        if user:
            tracker.remove_user(str(user.id))
            if getattr(user, 'ground_station_enabled', False):
                user.ground_station_enabled = False
                db.commit()
    except Exception as e:
        logger.error("Failed to stop tracker for expired license %s: %s", license_key, e)

    return {"message": "License expired", "license_key": license_key}


@app.put("/api/licenses/{license_key}/tier")
async def update_license_tier(
    license_key: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Update a license's tier — called by the web dashboard after a plan upgrade."""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    new_tier = body.get("tier")
    if not new_tier or new_tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail="Invalid tier")

    license = db.query(License).filter(License.license_key == license_key).first()
    if not license:
        raise HTTPException(status_code=404, detail="License not found")

    old_tier = license.tier
    license.tier = new_tier
    db.commit()

    # Sync GS access with tier change
    try:
        user = db.query(User).filter(User.license_id == license.id).first()
        if user:
            if new_tier == "pro" and not getattr(user, 'ground_station_enabled', False):
                user.ground_station_enabled = True
                if not user.gs_device_key:
                    import secrets
                    user.gs_device_key = secrets.token_hex(32)
                db.commit()
            elif old_tier == "pro" and new_tier != "pro":
                user.ground_station_enabled = False
                db.commit()
    except Exception as e:
        logger.error("Failed to sync GS access on tier change %s: %s", license_key, e)

    return {"message": "Tier updated", "tier": new_tier}


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
    duration_days = body.get("duration_days", LICENSE_DURATION_DAYS)
    activate_immediately = body.get("activate_immediately", False)

    if tier not in ["starter", "premium", "pro", "team-starter", "team-premium", "team-pro"]:
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Generate prefixed license key: FPT-XXXX-XXXX-XXXX-XXXX for teams, FP-XXXX-XXXX-XXXX-XXXX for personal
    chars = string.ascii_uppercase + string.digits
    segments = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    prefix = "FPT" if tier.startswith("team-") else "FP"
    license_key = f"{prefix}-{'-'.join(segments)}"

    # Calculate expiry based on duration_days (supports fractions for short test keys)
    now = datetime.utcnow()
    expires_at = now + timedelta(days=float(duration_days)) if activate_immediately else None
    activated_at = now if activate_immediately else None
    status = "active" if activate_immediately else "inactive"

    # Create in backend DB
    license = License(
        license_key=license_key,
        tier=tier,
        status=status,
        activations_used=1 if activate_immediately else 0,
        activations_max=activations_max,
        activated_at=activated_at,
        expires_at=expires_at,
        created_at=now,
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
        logger.warning("Website provision failed (non-critical): %s", e)

    return {
        "license_key": license_key,
        "tier": tier,
        "email": email,
        "status": status,
        "duration_days": duration_days,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "activate_immediately": activate_immediately,
        "message": f"License created successfully. {'Active immediately, expires ' + expires_at.strftime('%Y-%m-%d %H:%M:%S UTC') if activate_immediately else 'Share the key with the user to activate.'}"
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

    # Move airport config if keep_user doesn't have one
    keep_config = db.query(AirportConfig).filter(AirportConfig.user_id == keep_user.id).first()
    if not keep_config:
        db.query(AirportConfig).filter(AirportConfig.user_id == merge_user.id).update({"user_id": keep_user.id})

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

    # Delete merge_user
    db.delete(merge_user)
    db.commit()

    return {
        "message": f"Merged {merge_email} into {keep_email} successfully",
        "keep_user_id": str(keep_user.id),
        "license_id": str(keep_user.license_id) if keep_user.license_id else None,
    }


@app.get("/api/internal/ground-devices")
async def list_ground_devices(request: Request, db: Session = Depends(get_db)):
    """Returns all users with ground station enabled — for admin panel."""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    users = db.query(User).filter(User.ground_station_enabled == True).all()
    now = datetime.utcnow()
    result = []
    for u in users:
        last_seen_mem = _gs.ground_last_seen.get(str(u.id))
        last_seen_db = getattr(u, 'gs_last_heartbeat', None)
        last_seen = last_seen_mem or last_seen_db
        online = last_seen is not None and (now - last_seen).total_seconds() < 90
        result.append({
            "email": u.email,
            "gs_device_key": u.gs_device_key,
            "online": online,
            "last_seen": last_seen.isoformat() if last_seen else None,
        })

    return {"devices": result}


@app.delete("/api/internal/user")
async def delete_user_account(
    request: Request,
    db: Session = Depends(get_db)
):
    """Delete a user account and all associated data — called by web dashboard on account deletion"""
    secret = request.headers.get("x-internal-secret")
    if secret != WEBHOOK_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    body = await request.json()
    email = body.get("email", "").lower().strip()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return {"message": "User not found — already deleted or never existed"}

    # Stop active tracker if running
    tracker.remove_user(str(user.id))

    from models import NotificationLog, SavedLocation, TeamMember, TeamInviteToken, Team

    # Delete notification logs and saved locations
    db.query(NotificationLog).filter(NotificationLog.user_id == user.id).delete()
    db.query(SavedLocation).filter(SavedLocation.user_id == user.id).delete()

    # Remove user from any teams they were a member of
    db.query(TeamMember).filter(TeamMember.user_id == user.id).delete()
    db.query(TeamInviteToken).filter(TeamInviteToken.created_by == user.id).delete()

    # Null out user's license_id first so the license can be deleted independently
    license_id = user.license_id
    user.license_id = None
    db.flush()

    # Delete the license — must delete any team referencing it first to avoid FK violation.
    # Query by license_id directly rather than relying on TeamMember ownership records.
    if license_id:
        for team in db.query(Team).filter(Team.license_id == license_id).all():
            db.delete(team)  # cascades: channels, aircraft, airport_config, alert_settings, invite_tokens, roles, members
        db.flush()

        license = db.query(License).filter(License.id == license_id).first()
        if license:
            db.delete(license)

    # Delete user — cascades: personal aircraft, alert_settings, integrations, airport_config
    db.delete(user)
    db.commit()

    return {"message": f"User {email} and all associated data deleted"}


@app.api_route("/health", methods=["GET", "HEAD"])
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


# ── Twilio SMS webhook — STOP + command handler ───────────────────────────────

TWILIO_STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}

def _twiml_reply(msg: str) -> Response:
    safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
        media_type="application/xml",
    )

def _twiml_empty() -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")

def _phone_norm(p: str) -> str:
    return "".join(c for c in p if c.isdigit() or c == "+")

def _find_user_by_phone(phone: str, db: Session):
    """Find a user whose personal SMS integration matches this phone."""
    norm = _phone_norm(phone)
    integrations = db.query(Integration).filter(Integration.type == "sms", Integration.enabled == True).all()
    for intg in integrations:
        cfg = intg.config or {}
        if _phone_norm(cfg.get("to_phone", "")) == norm:
            return db.query(User).filter(User.id == intg.user_id).first()
    return None

def _find_team_by_phone(phone: str, db: Session):
    """Find the team whose SMS channel matches this phone."""
    from models import TeamChannel as TC
    norm = _phone_norm(phone)
    channels = db.query(TC).filter(TC.integration_type == "sms", TC.enabled == True).all()
    for ch in channels:
        cfg = ch.config or {}
        if _phone_norm(cfg.get("to_phone", "")) == norm:
            return db.query(Team).filter(Team.id == ch.team_id).first(), ch
    return None, None

SMS_HELP = (
    "FinalPing commands:\n"
    "STATUS — aircraft summary\n"
    "CLAIM [tail] — claim aircraft\n"
    "UNCLAIM — release your claim\n"
    "DUTY ON/OFF — toggle duty status\n"
    "WHOSON — see who is on duty\n"
    "ARRIVALS — upcoming expected arrivals\n"
    "ACK — acknowledge last alert\n"
    "HELP — show this message"
)

@app.post("/api/webhooks/twilio/sms")
async def twilio_sms_webhook(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    from_number = (form.get("From") or "").strip()
    raw_body = (form.get("Body") or "").strip()
    body_lower = raw_body.lower()

    if not from_number:
        return _twiml_empty()

    # Handle carrier STOP words first — disable integrations, no reply needed
    if body_lower in TWILIO_STOP_WORDS:
        integrations = db.query(Integration).filter(Integration.type == "sms", Integration.enabled == True).all()
        disabled = 0
        norm = _phone_norm(from_number)
        for integration in integrations:
            cfg = integration.config or {}
            if _phone_norm(cfg.get("to_phone", "")) == norm:
                integration.enabled = False
                disabled += 1
        if disabled:
            db.commit()
            logger.info(f"Disabled {disabled} SMS integration(s) for {from_number} via STOP")
        return _twiml_empty()

    # Identify sender
    user = _find_user_by_phone(from_number, db)
    team, team_channel = _find_team_by_phone(from_number, db)

    if not user and not team:
        return _twiml_reply("Number not linked to a FinalPing account.")

    parts = raw_body.strip().split()
    cmd = parts[0].upper() if parts else ""
    args = parts[1:] if len(parts) > 1 else []

    # --- Personal commands (work regardless of team context) ---
    if cmd == "HELP":
        return _twiml_reply(SMS_HELP)

    if cmd == "STATUS":
        # Return aircraft currently in airspace for this user or team
        if team:
            live = await tracker.get_live_aircraft(f"team:{team.id}") or []
            if not live:
                return _twiml_reply("No aircraft tracked for your team right now.")
            lines = [f"{a.get('tail_number','?')} {a.get('status','?')} {a.get('distance_nm',0):.1f}nm" for a in live[:5]]
            return _twiml_reply("Team aircraft:\n" + "\n".join(lines))
        if user:
            live = await tracker.get_live_aircraft(str(user.id)) or []
            if not live:
                return _twiml_reply("No aircraft tracked right now.")
            lines = [f"{a.get('tail_number','?')} {a.get('status','?')} {a.get('distance_nm',0):.1f}nm" for a in live[:5]]
            return _twiml_reply("Your aircraft:\n" + "\n".join(lines))

    if cmd == "ACK" and user:
        from models import NotificationLog
        logs = (
            db.query(NotificationLog)
            .filter(NotificationLog.user_id == user.id)
            .order_by(NotificationLog.sent_at.desc())
            .limit(1)
            .all()
        )
        if logs:
            logs[0].status = f"acked_sms"
            db.commit()
            return _twiml_reply("Alert acknowledged.")
        return _twiml_reply("No recent alerts to acknowledge.")

    # --- Team commands ---
    if not team:
        return _twiml_reply(f"Unknown command '{cmd}'. Reply HELP for commands.")

    # Find the member record
    member = None
    if user:
        member = db.query(TeamMember).filter(TeamMember.user_id == user.id, TeamMember.team_id == team.id).first()

    if cmd == "WHOSON":
        on_duty_members = []
        for m in team.members:
            if _is_member_on_duty(team.id, m.user_id, db):
                u = db.query(User).filter(User.id == m.user_id).first()
                on_duty_members.append(u.email.split("@")[0] if u else "?")
        if not on_duty_members:
            return _twiml_reply("Nobody is currently on duty.")
        return _twiml_reply("On duty: " + ", ".join(on_duty_members))

    if cmd == "ARRIVALS":
        now = datetime.utcnow()
        arrivals = db.query(ExpectedArrival).filter(
            ExpectedArrival.team_id == team.id,
            ExpectedArrival.status == "pending",
            ExpectedArrival.expected_at >= now,
        ).order_by(ExpectedArrival.expected_at.asc()).limit(5).all()
        if not arrivals:
            return _twiml_reply("No upcoming expected arrivals.")
        lines = []
        for a in arrivals:
            diff = a.expected_at - now
            mins = int(diff.total_seconds() / 60)
            lines.append(f"{a.tail_number} in {mins}min" + (f" — {a.notes}" if a.notes else ""))
        return _twiml_reply("Arrivals:\n" + "\n".join(lines))

    if cmd in ("DUTY",) and args:
        toggle = args[0].upper()
        if toggle == "ON":
            on_duty = True
        elif toggle == "OFF":
            on_duty = False
        else:
            return _twiml_reply("Usage: DUTY ON or DUTY OFF")
        if not user or not member:
            return _twiml_reply("Cannot find your team membership.")
        override = TeamDutyOverride(team_id=team.id, user_id=user.id, on_duty=on_duty, override_until=None)
        db.add(override)
        db.commit()
        return _twiml_reply(f"Duty status set to {'ON' if on_duty else 'OFF'}.")

    if cmd == "CLAIM":
        if not args:
            return _twiml_reply("Usage: CLAIM [tail number]")
        if not user or not member:
            return _twiml_reply("Cannot find your team membership.")
        tail = args[0].upper()
        now = datetime.utcnow()
        existing = db.query(AircraftClaim).filter(
            AircraftClaim.team_id == team.id,
            AircraftClaim.tail_number == tail,
            AircraftClaim.released_at.is_(None),
            AircraftClaim.expires_at > now,
        ).first()
        if existing and str(existing.claimed_by_user_id) != str(user.id):
            claimer = db.query(User).filter(User.id == existing.claimed_by_user_id).first()
            return _twiml_reply(f"{tail} already claimed by {claimer.email.split('@')[0] if claimer else '?'}.")
        claim = AircraftClaim(
            team_id=team.id,
            icao24="unknown",
            tail_number=tail,
            claimed_by_user_id=user.id,
            expires_at=now + timedelta(hours=2),
        )
        db.add(claim)
        db.commit()
        return _twiml_reply(f"You claimed {tail} for 2 hours.")

    if cmd == "UNCLAIM":
        if not user or not member:
            return _twiml_reply("Cannot find your team membership.")
        now = datetime.utcnow()
        claims = db.query(AircraftClaim).filter(
            AircraftClaim.team_id == team.id,
            AircraftClaim.claimed_by_user_id == user.id,
            AircraftClaim.released_at.is_(None),
            AircraftClaim.expires_at > now,
        ).all()
        for c in claims:
            c.released_at = now
        db.commit()
        if claims:
            return _twiml_reply(f"Released {len(claims)} claim(s).")
        return _twiml_reply("No active claims to release.")

    return _twiml_reply(f"Unknown command '{cmd}'. Reply HELP for commands.")


# ============================================================================
# STARTUP EVENT
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Start the global aircraft tracker on startup"""
    logger.info("Starting FinalPing Cloud Backend...")

    # Auto-migrate: add SDR range columns if missing
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                ALTER TABLE airport_configs
                ADD COLUMN IF NOT EXISTS sdr_range_nm JSONB,
                ADD COLUMN IF NOT EXISTS sdr_range_updated_at TIMESTAMP;
            """))
            conn.commit()
    except Exception as e:
        logger.warning("SDR range migration skipped: %s", e)

    logger.info("Initializing global aircraft tracker...")
    await tracker.start()

    # Load all existing users into the tracker
    db = SessionLocal()
    try:
        from sqlalchemy import text

        # Schema migrations — ADD COLUMN IF NOT EXISTS is idempotent on PostgreSQL
        migrations = [
            # Licenses
            "ALTER TABLE licenses ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(255)",
            # Widen stripe_subscription_id if it was created as VARCHAR(20) in older schema
            "ALTER TABLE licenses ALTER COLUMN stripe_subscription_id TYPE VARCHAR(255)",
            # Aircraft — added v1.0.6
            "ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS aircraft_type VARCHAR(100)",
            "ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS alert_distances JSON",
            # AirportConfig — added v1.0.6
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS runway_info JSON",
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS approach_corridor_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS approach_runway_heading FLOAT",
            # AirportConfig — SDR range persistence
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS sdr_range_nm JSONB",
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS sdr_range_updated_at TIMESTAMP",
            # TeamMember — added for custom roles
            "ALTER TABLE team_members ADD COLUMN IF NOT EXISTS custom_role_id UUID REFERENCES team_roles(id) ON DELETE SET NULL",
            # Users — ground station columns
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS gs_last_heartbeat TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS gs_device_key VARCHAR(64)",
            # TeamAirportConfig — multi-airport support
            "ALTER TABLE team_airport_configs ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT FALSE",
            # New tables for teams v2
            """CREATE TABLE IF NOT EXISTS aircraft_claims (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                icao24 VARCHAR(10) NOT NULL,
                tail_number VARCHAR(10),
                claimed_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                claimed_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL,
                released_at TIMESTAMP,
                flight_note TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS team_shifts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                days_of_week JSON NOT NULL,
                start_time VARCHAR(5) NOT NULL,
                end_time VARCHAR(5) NOT NULL,
                timezone VARCHAR(50) DEFAULT 'UTC',
                color VARCHAR(7) DEFAULT '#22d3a3',
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS team_shift_members (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                shift_id UUID NOT NULL REFERENCES team_shifts(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE
            )""",
            """CREATE TABLE IF NOT EXISTS team_duty_overrides (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                on_duty BOOLEAN NOT NULL,
                override_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS expected_arrivals (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                tail_number VARCHAR(10) NOT NULL,
                icao24 VARCHAR(10),
                expected_at TIMESTAMP NOT NULL,
                notes TEXT,
                reminder_minutes INTEGER DEFAULT 30,
                status VARCHAR(20) DEFAULT 'pending',
                linked_icao24 VARCHAR(10),
                reminder_sent BOOLEAN DEFAULT FALSE,
                created_by_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS escalation_configs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID UNIQUE NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                enabled BOOLEAN DEFAULT FALSE,
                first_escalation_minutes INTEGER DEFAULT 5,
                first_escalation_target VARCHAR(20) DEFAULT 'all_admins',
                second_escalation_minutes INTEGER DEFAULT 10,
                second_escalation_target VARCHAR(20) DEFAULT 'owner'
            )""",
            """CREATE TABLE IF NOT EXISTS alert_escalations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                aircraft_tail VARCHAR(10) NOT NULL,
                alert_type VARCHAR(50) NOT NULL,
                original_fired_at TIMESTAMP NOT NULL,
                escalation_level INTEGER NOT NULL,
                escalated_at TIMESTAMP DEFAULT NOW(),
                acked_by_user_id UUID REFERENCES users(id),
                acked_at TIMESTAMP
            )""",
        ]
        for sql in migrations:
            try:
                db.execute(text(sql))
                db.commit()
            except Exception as mig_err:
                db.rollback()
                logger.warning("Migration skipped (%s): %s", sql[:60], mig_err)

        # Normalize legacy alert type labels
        db.execute(text("UPDATE notification_logs SET alert_type = '2nm' WHERE alert_type = '2.0nm'"))
        db.execute(text("UPDATE notification_logs SET alert_type = '5nm' WHERE alert_type = '5.0nm'"))
        db.execute(text("UPDATE notification_logs SET alert_type = '10nm' WHERE alert_type = '10.0nm'"))
        db.execute(text("UPDATE notification_logs SET alert_type = '15nm' WHERE alert_type = '15.0nm'"))
        db.execute(text("DELETE FROM alert_settings WHERE alert_type IN ('2.0nm', '5.0nm', '10.0nm', '15.0nm')"))
        db.commit()
        logger.info("Schema migrations complete")

        users = db.query(User).all()
        for user in users:
            try:
                await tracker.update_user_aircraft(str(user.id), db)
            except Exception as e:
                logger.error("Failed to load tracker for user %s: %s", user.id, e)
        logger.info("Loaded %d users into tracker", len(users))

        # Load team trackers
        from models import Team as TeamModel
        teams = db.query(TeamModel).all()
        for team in teams:
            try:
                await tracker.update_team_aircraft(str(team.id), db)
            except Exception as e:
                logger.error("Failed to load tracker for team %s: %s", team.id, e)
        logger.info("Loaded %d teams into tracker", len(teams))
    except Exception as e:
        logger.error("Error loading users on startup: %s", e)
    finally:
        db.close()

    logger.info("FinalPing Cloud Backend ready!")


# ============================================================================
# TEAM HELPERS
# ============================================================================

def _channel_value(integration_type: str, config: dict) -> str:
    if integration_type == "sms":
        return config.get("to_phone", "")
    elif integration_type == "email":
        return config.get("to_email", "")
    elif integration_type == "telegram":
        return config.get("value", "")
    return config.get("webhook_url", "")


def _channel_config(integration_type: str, value: str) -> dict:
    if integration_type == "sms":
        return {"to_phone": value}
    elif integration_type == "email":
        return {"to_email": value}
    elif integration_type == "telegram":
        return {"value": value}
    return {"webhook_url": value}


def _get_user_team(user: User, db: Session) -> Team:
    # Use TeamMember as the source of truth — removing a member immediately revokes access
    member = db.query(TeamMember).filter(TeamMember.user_id == user.id).first()
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of any team")
    team = db.query(Team).filter(Team.id == member.team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    # Validate the team's license hasn't expired
    team_license = db.query(License).filter(License.id == team.license_id).first()
    if team_license and team_license.expires_at and team_license.expires_at + LICENSE_GRACE_PERIOD < datetime.utcnow():
        raise HTTPException(status_code=403, detail="team_license_expired")
    return team


def _build_team_response(team: Team, db: Session) -> dict:
    members = []
    for m in team.members:
        user = db.query(User).filter(User.id == m.user_id).first()
        custom_role = db.query(TeamRole).filter(TeamRole.id == m.custom_role_id).first() if m.custom_role_id else None
        members.append({
            "id": str(m.id),
            "user_id": str(m.user_id),
            "email": user.email if user else "",
            "role": m.role,
            "custom_role_id": str(m.custom_role_id) if m.custom_role_id else None,
            "custom_role_name": custom_role.name if custom_role else None,
            "custom_role_color": custom_role.color if custom_role else None,
            "joined_at": m.joined_at,
        })
    channels = [
        {
            "id": str(c.id),
            "integration_type": c.integration_type,
            "label": c.label,
            "value": _channel_value(c.integration_type, c.config),
            "enabled": c.enabled,
            "created_at": c.created_at,
        }
        for c in team.channels
    ]
    now = datetime.utcnow()
    pending_invites = [
        {
            "id": str(i.id),
            "token": i.token,
            "note": i.note,
            "expires_at": i.expires_at,
            "created_at": i.created_at,
        }
        for i in team.invite_tokens
        if i.used_at is None and i.expires_at > now
    ]
    roles = [
        {"id": str(r.id), "name": r.name, "permissions": r.permissions or [], "color": r.color}
        for r in team.roles
    ]
    license = db.query(License).filter(License.id == team.license_id).first()
    return {
        "id": str(team.id),
        "name": team.name,
        "license_tier": license.tier if license else "team-starter",
        "license_expires_at": license.expires_at.isoformat() if license and license.expires_at else None,
        "members": members,
        "channels": channels,
        "routing": team.routing or {},
        "pending_invites": pending_invites,
        "roles": roles,
        "created_at": team.created_at,
    }


# ============================================================================
# TEAM ENDPOINTS
# ============================================================================

@app.get("/api/teams/me")
async def get_my_team(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    return _build_team_response(team, db)


@app.post("/api/teams/channels", status_code=201)
async def add_team_channel(
    body: TeamChannelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    channel = TeamChannel(
        team_id=team.id,
        integration_type=body.integration_type,
        label=body.label,
        config=_channel_config(body.integration_type, body.value),
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return {
        "id": str(channel.id),
        "integration_type": channel.integration_type,
        "label": channel.label,
        "value": body.value,
        "enabled": channel.enabled,
        "created_at": channel.created_at,
    }


@app.delete("/api/teams/channels/{channel_id}", status_code=204)
async def remove_team_channel(
    channel_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    channel = db.query(TeamChannel).filter(
        TeamChannel.id == channel_id,
        TeamChannel.team_id == team.id
    ).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    # Remove channel from routing rules
    routing = dict(team.routing or {})
    for dist in routing:
        routing[dist] = [cid for cid in routing[dist] if cid != channel_id]
    team.routing = routing
    db.delete(channel)
    db.commit()
    return None


@app.put("/api/teams/routing")
async def update_team_routing(
    body: TeamRoutingUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    team.routing = body.routing
    db.commit()
    return {"routing": team.routing}


@app.post("/api/teams/invite")
async def invite_team_member(
    body: TeamInviteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    license = db.query(License).filter(License.id == team.license_id).first()
    if not license:
        raise HTTPException(status_code=500, detail="Team license not found")

    resend_key = os.getenv("RESEND_API_KEY")
    if resend_key:
        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;">
          <div style="background:#0f1117;padding:32px;border-radius:12px;">
            <h2 style="color:#0ea5e9;margin:0 0 8px">You've been invited to FinalPing for Teams</h2>
            <p style="color:#9ca3af;margin:0 0 24px;font-size:14px;line-height:1.6">
              {current_user.email} has invited you to join their team on FinalPing for Teams —
              real-time aircraft proximity alerts for your whole crew.
            </p>
            <div style="background:#1a2030;border:1px solid #2d3748;border-radius:8px;padding:16px;margin-bottom:24px;">
              <div style="font-size:11px;color:#4b5563;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Your Team License Key</div>
              <div style="font-size:18px;font-weight:700;color:#f9fafb;letter-spacing:.05em">{license.license_key}</div>
            </div>
            <p style="color:#9ca3af;font-size:13px;line-height:1.6;margin:0 0 20px">
              1. Download <strong style="color:#f9fafb">FinalPing for Teams</strong> at finalpingapp.com/download<br>
              2. Open the app and click <strong style="color:#f9fafb">Activate License</strong><br>
              3. Enter the key above and your email address to join the team
            </p>
            <hr style="border-color:#2d3748;margin:20px 0">
            <p style="color:#4b5563;font-size:12px;margin:0">
              Sent by <a href="https://finalpingapp.com" style="color:#0ea5e9">FinalPing</a>
            </p>
          </div>
        </div>
        """
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                    json={
                        "from": "FinalPing <noreply@finalpingapp.com>",
                        "to": [body.email],
                        "subject": f"You've been invited to join FinalPing for Teams",
                        "html": html,
                    },
                    timeout=10.0,
                )
        except Exception as e:
            logger.warning("Invite email failed (non-critical): %s", e)

    logger.info("Team invite sent to %s by %s", body.email, current_user.email)
    return {"message": f"Invite sent to {body.email}"}


@app.delete("/api/teams/members/{member_id}", status_code=204)
async def remove_team_member(
    member_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(
        TeamMember.team_id == team.id,
        TeamMember.user_id == current_user.id
    ).first()
    if not caller or caller.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only owners and admins can remove members")
    member = db.query(TeamMember).filter(
        TeamMember.id == member_id,
        TeamMember.team_id == team.id
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot remove the team owner")
    db.delete(member)
    db.commit()
    return None


@app.post("/api/teams/invites", status_code=201)
async def generate_team_invite(
    body: TeamInviteCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    import secrets
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only owners and admins can generate invite codes")
    raw = secrets.token_hex(6).upper()
    token = f"FPTI-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"
    invite = TeamInviteToken(
        team_id=team.id,
        token=token,
        created_by=current_user.id,
        note=body.note,
        expires_at=datetime.utcnow() + timedelta(hours=48),
    )
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return {"id": str(invite.id), "token": invite.token, "note": invite.note, "expires_at": invite.expires_at, "created_at": invite.created_at}


@app.get("/api/teams/invites")
async def list_team_invites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    now = datetime.utcnow()
    invites = db.query(TeamInviteToken).filter(
        TeamInviteToken.team_id == team.id,
        TeamInviteToken.used_at.is_(None),
        TeamInviteToken.expires_at > now,
    ).order_by(TeamInviteToken.created_at.desc()).all()
    return [{"id": str(i.id), "token": i.token, "note": i.note, "expires_at": i.expires_at, "created_at": i.created_at} for i in invites]


@app.delete("/api/teams/invites/{invite_id}", status_code=204)
async def revoke_team_invite(
    invite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only owners and admins can revoke invites")
    invite = db.query(TeamInviteToken).filter(TeamInviteToken.id == invite_id, TeamInviteToken.team_id == team.id).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found")
    db.delete(invite)
    db.commit()
    return None


@app.post("/api/teams/activate-invite", response_model=TokenResponse)
@limiter.limit("10/minute")
async def activate_team_invite(
    request: Request,
    body: TeamActivateInviteRequest,
    db: Session = Depends(get_db)
):
    body.email = body.email.lower().strip()
    now = datetime.utcnow()
    invite = db.query(TeamInviteToken).filter(
        TeamInviteToken.token == body.token.upper().strip(),
        TeamInviteToken.used_at.is_(None),
        TeamInviteToken.expires_at > now,
    ).first()
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid or expired invite code")
    team = db.query(Team).filter(Team.id == invite.team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    license = db.query(License).filter(License.id == team.license_id).first()
    if not license:
        raise HTTPException(status_code=404, detail="Team license not found")
    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        user = User(email=body.email, license_id=license.id, created_at=datetime.utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)
    elif user.license_id != license.id:
        user.license_id = license.id
        db.commit()
    existing = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == user.id).first()
    if not existing:
        db.add(TeamMember(team_id=team.id, user_id=user.id, role="member"))
        db.commit()
    invite.used_at = now
    invite.used_by_user_id = user.id
    db.commit()
    access_token = create_access_token(str(user.id))
    return TokenResponse(access_token=access_token, token_type="bearer", user_id=str(user.id), email=user.email, license_tier=license.tier, expires_at=license.expires_at)


@app.get("/api/teams/roles")
async def get_team_roles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    roles = db.query(TeamRole).filter(TeamRole.team_id == team.id).all()
    return [{"id": str(r.id), "name": r.name, "permissions": r.permissions or [], "color": r.color} for r in roles]


@app.post("/api/teams/roles", status_code=201)
async def create_team_role(
    body: TeamRoleCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can create roles")
    role = TeamRole(team_id=team.id, name=body.name, permissions=body.permissions, color=body.color)
    db.add(role)
    db.commit()
    db.refresh(role)
    return {"id": str(role.id), "name": role.name, "permissions": role.permissions or [], "color": role.color}


@app.put("/api/teams/roles/{role_id}")
async def update_team_role(
    role_id: str,
    body: TeamRoleUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can edit roles")
    role = db.query(TeamRole).filter(TeamRole.id == role_id, TeamRole.team_id == team.id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if body.name is not None:
        role.name = body.name
    if body.permissions is not None:
        role.permissions = body.permissions
    if body.color is not None:
        role.color = body.color
    db.commit()
    db.refresh(role)
    return {"id": str(role.id), "name": role.name, "permissions": role.permissions or [], "color": role.color}


@app.delete("/api/teams/roles/{role_id}", status_code=204)
async def delete_team_role(
    role_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can delete roles")
    role = db.query(TeamRole).filter(TeamRole.id == role_id, TeamRole.team_id == team.id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    db.query(TeamMember).filter(TeamMember.custom_role_id == role.id).update({"custom_role_id": None})
    db.delete(role)
    db.commit()
    return None


@app.put("/api/teams/members/{member_id}/role")
async def update_member_role(
    member_id: str,
    body: AssignMemberRoleRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    caller = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.user_id == current_user.id).first()
    if not caller or caller.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Only owners and admins can assign roles")
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.team_id == team.id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    if member.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot change the owner's role")
    if body.role:
        if body.role not in ("admin", "member"):
            raise HTTPException(status_code=400, detail="Use 'admin' or 'member'")
        member.role = body.role
        member.custom_role_id = None
    elif body.custom_role_id:
        custom_role = db.query(TeamRole).filter(TeamRole.id == body.custom_role_id, TeamRole.team_id == team.id).first()
        if not custom_role:
            raise HTTPException(status_code=404, detail="Custom role not found")
        member.custom_role_id = custom_role.id
    db.commit()
    user = db.query(User).filter(User.id == member.user_id).first()
    custom_role = db.query(TeamRole).filter(TeamRole.id == member.custom_role_id).first() if member.custom_role_id else None
    return {
        "id": str(member.id), "user_id": str(member.user_id), "email": user.email if user else "",
        "role": member.role, "custom_role_id": str(member.custom_role_id) if member.custom_role_id else None,
        "custom_role_name": custom_role.name if custom_role else None, "custom_role_color": custom_role.color if custom_role else None,
        "joined_at": member.joined_at,
    }


@app.get("/api/teams/activity")
async def get_team_activity(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    from models import NotificationLog
    team = _get_user_team(current_user, db)
    member_user_ids = [m.user_id for m in team.members]
    logs = (
        db.query(NotificationLog)
        .filter(NotificationLog.user_id.in_(member_user_ids))
        .order_by(NotificationLog.sent_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": str(l.id),
            "aircraft_tail": l.aircraft_tail,
            "alert_type": l.alert_type,
            "message": l.message,
            "integration_type": l.integration_type,
            "status": l.status,
            "sent_at": l.sent_at,
        }
        for l in logs
    ]


# ============================================================================
# TEAM-SCOPED AIRCRAFT (isolated from personal user aircraft)
# ============================================================================

@app.get("/api/teams/aircraft", response_model=List[AircraftResponse])
async def get_team_aircraft(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    aircraft = db.query(TeamAircraft).filter(
        TeamAircraft.team_id == team.id,
        TeamAircraft.active == True
    ).all()
    return [
        AircraftResponse(
            id=str(a.id), tail_number=a.tail_number, icao24=a.icao24,
            friendly_name=a.friendly_name, aircraft_type=a.aircraft_type,
            alert_distances=a.alert_distances, active=a.active, created_at=a.created_at
        )
        for a in aircraft
    ]


@app.post("/api/teams/aircraft", response_model=AircraftResponse, status_code=201)
async def add_team_aircraft(
    aircraft_data: AircraftCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    tier = get_user_tier(current_user, db)
    limit = get_tier_limit(tier, "aircraft")
    if limit is not None:
        count = db.query(TeamAircraft).filter(TeamAircraft.team_id == team.id, TeamAircraft.active == True).count()
        if count >= limit:
            raise HTTPException(status_code=403, detail=f"Your {tier} plan allows up to {limit} aircraft.")
    existing = db.query(TeamAircraft).filter(
        TeamAircraft.team_id == team.id,
        TeamAircraft.tail_number == aircraft_data.tail_number,
        TeamAircraft.active == True
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Aircraft already exists")
    aircraft = TeamAircraft(
        team_id=team.id,
        tail_number=aircraft_data.tail_number,
        icao24=aircraft_data.icao24,
        friendly_name=aircraft_data.friendly_name,
        aircraft_type=aircraft_data.aircraft_type,
        alert_distances=aircraft_data.alert_distances,
        active=True,
        created_at=datetime.utcnow()
    )
    db.add(aircraft)
    db.commit()
    db.refresh(aircraft)
    await tracker.update_team_aircraft(str(team.id), db)
    return AircraftResponse(
        id=str(aircraft.id), tail_number=aircraft.tail_number, icao24=aircraft.icao24,
        friendly_name=aircraft.friendly_name, aircraft_type=aircraft.aircraft_type,
        alert_distances=aircraft.alert_distances, active=aircraft.active, created_at=aircraft.created_at
    )


@app.put("/api/teams/aircraft/{aircraft_id}", response_model=AircraftResponse)
async def update_team_aircraft(
    aircraft_id: str,
    aircraft_data: AircraftUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    aircraft = db.query(TeamAircraft).filter(
        TeamAircraft.id == aircraft_id,
        TeamAircraft.team_id == team.id,
        TeamAircraft.active == True
    ).first()
    if not aircraft:
        raise HTTPException(status_code=404, detail="Aircraft not found")
    if aircraft_data.tail_number is not None:
        aircraft.tail_number = aircraft_data.tail_number
    if aircraft_data.icao24 is not None:
        aircraft.icao24 = aircraft_data.icao24
    if aircraft_data.friendly_name is not None:
        aircraft.friendly_name = aircraft_data.friendly_name
    if aircraft_data.aircraft_type is not None:
        aircraft.aircraft_type = aircraft_data.aircraft_type
    if aircraft_data.alert_distances is not None:
        aircraft.alert_distances = aircraft_data.alert_distances
    db.commit()
    db.refresh(aircraft)
    await tracker.update_team_aircraft(str(team.id), db)
    return AircraftResponse(
        id=str(aircraft.id), tail_number=aircraft.tail_number, icao24=aircraft.icao24,
        friendly_name=aircraft.friendly_name, aircraft_type=aircraft.aircraft_type,
        alert_distances=aircraft.alert_distances, active=aircraft.active, created_at=aircraft.created_at
    )


@app.delete("/api/teams/aircraft/{aircraft_id}")
async def delete_team_aircraft(
    aircraft_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    aircraft = db.query(TeamAircraft).filter(
        TeamAircraft.id == aircraft_id,
        TeamAircraft.team_id == team.id
    ).first()
    if not aircraft:
        raise HTTPException(status_code=404, detail="Aircraft not found")
    db.delete(aircraft)
    db.commit()
    await tracker.update_team_aircraft(str(team.id), db)
    return {"message": "Aircraft deleted"}


@app.get("/api/teams/aircraft/live", response_model=List[LiveAircraftResponse])
async def get_team_live_aircraft(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    aircraft_data = await tracker.get_live_aircraft(f"team:{team.id}")
    return aircraft_data


# ============================================================================
# TEAM-SCOPED AIRPORT CONFIG (isolated from personal user airport config)
# ============================================================================

@app.get("/api/teams/airport/config")
async def get_team_airport_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    config = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).first()
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
        "runway_info": config.runway_info,
        "approach_corridor_enabled": config.approach_corridor_enabled,
        "approach_runway_heading": config.approach_runway_heading,
        "quiet_hours_enabled": config.quiet_hours_enabled,
        "quiet_hours_start": config.quiet_hours_start,
        "quiet_hours_end": config.quiet_hours_end,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


@app.post("/api/teams/airport/config")
async def save_team_airport_config(
    config_data: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    lat = config_data.get("latitude")
    lon = config_data.get("longitude")
    elevation = config_data.get("elevation_ft_msl")
    if lat and lon and (not elevation or elevation == 0):
        try:
            import httpx
            resp = httpx.get(f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}", timeout=5)
            if resp.status_code == 200:
                elev_meters = resp.json().get("elevation", [0])[0]
                elevation = int(elev_meters * 3.28084)
        except Exception as e:
            logger.warning("Failed to auto-detect elevation for team config: %s", e)
            elevation = config_data.get("elevation_ft_msl", 0)
    config = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).first()
    if config:
        config.airport_code = config_data.get("airport_code", config.airport_code)
        config.airport_name = config_data.get("airport_name", config.airport_name)
        config.latitude = str(config_data.get("latitude", config.latitude))
        config.longitude = str(config_data.get("longitude", config.longitude))
        if elevation:
            config.elevation_ft_msl = elevation
        config.query_radius_nm = str(config_data.get("detection_radius_nm", config.query_radius_nm))
        config.radius_nm = str(config_data.get("polling_interval_seconds", config.radius_nm))
        config.quiet_hours_enabled = config_data.get("quiet_hours_enabled", config.quiet_hours_enabled)
        config.quiet_hours_start = config_data.get("quiet_hours_start", config.quiet_hours_start)
        config.quiet_hours_end = config_data.get("quiet_hours_end", config.quiet_hours_end)
        if "alert_distances_nm" in config_data:
            config.alert_distances_nm = [str(d) for d in config_data["alert_distances_nm"]]
        if "runway_info" in config_data:
            config.runway_info = config_data["runway_info"]
        if "approach_corridor_enabled" in config_data:
            config.approach_corridor_enabled = config_data["approach_corridor_enabled"]
        if "approach_runway_heading" in config_data:
            config.approach_runway_heading = config_data["approach_runway_heading"]
        config.updated_at = datetime.utcnow()
    else:
        config = TeamAirportConfig(
            team_id=team.id,
            airport_code=config_data.get("airport_code", ""),
            airport_name=config_data.get("airport_name", ""),
            latitude=str(config_data.get("latitude", "0")),
            longitude=str(config_data.get("longitude", "0")),
            elevation_ft_msl=elevation or config_data.get("elevation_ft_msl", 0),
            query_radius_nm=str(config_data.get("detection_radius_nm", "100.0")),
            radius_nm=str(config_data.get("polling_interval_seconds", "10")),
            alert_distances_nm=[str(d) for d in config_data.get("alert_distances_nm", [10.0, 5.0, 2.0])],
            runway_info=config_data.get("runway_info", []),
            approach_corridor_enabled=config_data.get("approach_corridor_enabled", False),
            approach_runway_heading=config_data.get("approach_runway_heading"),
            quiet_hours_enabled=config_data.get("quiet_hours_enabled", False),
            quiet_hours_start=config_data.get("quiet_hours_start", "23:00"),
            quiet_hours_end=config_data.get("quiet_hours_end", "06:00"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(config)
    db.commit()
    db.refresh(config)

    # Clean up team alert settings for distances that were removed
    if "alert_distances_nm" in config_data:
        try:
            valid_types = {f"{int(float(d))}nm" if float(d) == int(float(d)) else f"{float(d)}nm"
                          for d in config_data["alert_distances_nm"]}
            valid_types.add("landing")
            for setting in list(team.alert_settings):
                if setting.alert_type not in valid_types:
                    db.delete(setting)
            db.commit()
        except Exception as e:
            logger.warning("Failed to clean up orphaned team alert settings: %s", e)

    await tracker.update_team_aircraft(str(team.id), db)
    return {"message": "Configuration saved successfully", "id": str(config.id)}


@app.delete("/api/teams/airport/config")
async def delete_team_airport_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    config = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).first()
    if not config:
        raise HTTPException(status_code=404, detail="No airport configuration found")
    db.delete(config)
    db.commit()
    return {"message": "Airport configuration deleted"}


# ============================================================================
# TEAM-SCOPED ALERT SETTINGS (isolated from personal user alert settings)
# ============================================================================

@app.get("/api/teams/alert-settings", response_model=List[AlertSettingResponse])
async def get_team_alert_settings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    settings = db.query(TeamAlertSetting).filter(TeamAlertSetting.team_id == team.id).all()
    return [
        AlertSettingResponse(
            id=str(s.id), alert_type=s.alert_type, enabled=s.enabled,
            message_template=s.message_template, created_at=s.created_at
        )
        for s in settings
    ]


@app.post("/api/teams/alert-settings", response_model=AlertSettingResponse)
async def save_team_alert_setting(
    setting_data: AlertSettingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    team = _get_user_team(current_user, db)
    existing = db.query(TeamAlertSetting).filter(
        TeamAlertSetting.team_id == team.id,
        TeamAlertSetting.alert_type == setting_data.alert_type
    ).first()
    if existing:
        existing.enabled = setting_data.enabled
        existing.message_template = setting_data.message_template
        db.commit()
        db.refresh(existing)
        setting = existing
    else:
        setting = TeamAlertSetting(
            team_id=team.id,
            alert_type=setting_data.alert_type,
            enabled=setting_data.enabled,
            message_template=setting_data.message_template,
            created_at=datetime.utcnow()
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return AlertSettingResponse(
        id=str(setting.id), alert_type=setting.alert_type, enabled=setting.enabled,
        message_template=setting.message_template, created_at=setting.created_at
    )


# ============================================================================
# TEAM MULTI-AIRPORT
# ============================================================================

@app.get("/api/teams/airports")
async def get_team_airports(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    configs = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).all()
    return [_serialize_team_airport(c) for c in configs]


@app.post("/api/teams/airports", status_code=201)
async def add_team_airport(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_airport")
    body = await request.json()
    existing_count = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).count()
    config = TeamAirportConfig(
        team_id=team.id,
        airport_code=body.get("airport_code"),
        airport_name=body.get("airport_name"),
        latitude=str(body.get("latitude", "0")),
        longitude=str(body.get("longitude", "0")),
        elevation_ft_msl=int(body.get("elevation_ft_msl", 0)),
        radius_nm=str(body.get("radius_nm", "4.0")),
        floor_ft_agl=int(body.get("floor_ft_agl", 0)),
        ceiling_ft_agl=int(body.get("ceiling_ft_agl", 2500)),
        query_radius_nm=str(body.get("query_radius_nm", "100.0")),
        alert_distances_nm=body.get("alert_distances_nm", ["10.0", "5.0", "2.0"]),
        quiet_hours_enabled=body.get("quiet_hours_enabled", True),
        quiet_hours_start=body.get("quiet_hours_start", "23:00"),
        quiet_hours_end=body.get("quiet_hours_end", "06:00"),
        is_active=existing_count == 0,  # first airport auto-activates
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return _serialize_team_airport(config)


@app.put("/api/teams/airports/{airport_id}")
async def update_team_airport(airport_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_airport")
    config = db.query(TeamAirportConfig).filter(TeamAirportConfig.id == airport_id, TeamAirportConfig.team_id == team.id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Airport not found")
    body = await request.json()
    for field in ["airport_code", "airport_name", "latitude", "longitude", "elevation_ft_msl",
                  "radius_nm", "floor_ft_agl", "ceiling_ft_agl", "query_radius_nm",
                  "alert_distances_nm", "quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end"]:
        if field in body:
            setattr(config, field, body[field])
    db.commit()
    db.refresh(config)
    return _serialize_team_airport(config)


@app.put("/api/teams/airports/{airport_id}/active", status_code=204)
async def set_active_team_airport(airport_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_airport")
    configs = db.query(TeamAirportConfig).filter(TeamAirportConfig.team_id == team.id).all()
    for c in configs:
        c.is_active = (str(c.id) == airport_id)
    db.commit()
    await tracker.update_team_aircraft(str(team.id), db)


@app.delete("/api/teams/airports/{airport_id}", status_code=204)
async def delete_team_airport(airport_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_airport")
    config = db.query(TeamAirportConfig).filter(TeamAirportConfig.id == airport_id, TeamAirportConfig.team_id == team.id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Airport not found")
    db.delete(config)
    db.commit()


def _serialize_team_airport(c: TeamAirportConfig) -> dict:
    return {
        "id": str(c.id),
        "airport_code": c.airport_code,
        "airport_name": c.airport_name,
        "latitude": c.latitude,
        "longitude": c.longitude,
        "elevation_ft_msl": c.elevation_ft_msl,
        "radius_nm": c.radius_nm,
        "floor_ft_agl": c.floor_ft_agl,
        "ceiling_ft_agl": c.ceiling_ft_agl,
        "query_radius_nm": c.query_radius_nm,
        "alert_distances_nm": c.alert_distances_nm or ["10.0", "5.0", "2.0"],
        "quiet_hours_enabled": c.quiet_hours_enabled,
        "quiet_hours_start": c.quiet_hours_start,
        "quiet_hours_end": c.quiet_hours_end,
        "is_active": c.is_active or False,
        "created_at": c.created_at,
    }


def _require_team_permission(user: User, team: Team, db: Session, permission: str):
    member = db.query(TeamMember).filter(TeamMember.user_id == user.id, TeamMember.team_id == team.id).first()
    if not member:
        raise HTTPException(status_code=403, detail="Not a team member")
    if member.role == "owner":
        return  # owners always have all permissions
    # Check custom role permissions
    if member.custom_role_id:
        role = db.query(TeamRole).filter(TeamRole.id == member.custom_role_id).first()
        if role and permission in (role.permissions or []):
            return
    # Default admin permissions
    admin_permissions = ["manage_channels", "manage_routing", "manage_aircraft", "manage_airport",
                         "manage_alerts", "manage_shifts", "claim_aircraft", "ack_alerts",
                         "view_activity", "manage_arrivals"]
    if member.role == "admin" and permission in admin_permissions:
        return
    # Default member permissions
    member_permissions = ["claim_aircraft", "ack_alerts", "view_activity", "manage_arrivals"]
    if member.role == "member" and permission in member_permissions:
        return
    raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")


# ============================================================================
# TEAM CLAIMS
# ============================================================================

@app.get("/api/teams/claims")
async def get_team_claims(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    now = datetime.utcnow()
    claims = db.query(AircraftClaim).filter(
        AircraftClaim.team_id == team.id,
        AircraftClaim.released_at.is_(None),
        AircraftClaim.expires_at > now,
    ).all()
    result = []
    for c in claims:
        user = db.query(User).filter(User.id == c.claimed_by_user_id).first()
        result.append({
            "id": str(c.id),
            "icao24": c.icao24,
            "tail_number": c.tail_number,
            "claimed_by_user_id": str(c.claimed_by_user_id),
            "claimed_by_email": user.email if user else "",
            "claimed_at": c.claimed_at,
            "expires_at": c.expires_at,
            "flight_note": c.flight_note,
        })
    return result


@app.post("/api/teams/claims", status_code=201)
async def claim_aircraft(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "claim_aircraft")
    body = await request.json()
    icao24 = body.get("icao24", "").lower().strip()
    if not icao24:
        raise HTTPException(status_code=400, detail="icao24 required")
    # Release any existing claim on this aircraft from this team
    now = datetime.utcnow()
    existing = db.query(AircraftClaim).filter(
        AircraftClaim.team_id == team.id,
        AircraftClaim.icao24 == icao24,
        AircraftClaim.released_at.is_(None),
        AircraftClaim.expires_at > now,
    ).first()
    if existing:
        if str(existing.claimed_by_user_id) != str(current_user.id):
            user = db.query(User).filter(User.id == existing.claimed_by_user_id).first()
            raise HTTPException(status_code=409, detail=f"Already claimed by {user.email if user else 'another member'}")
        return {"message": "Already claimed by you"}
    claim = AircraftClaim(
        team_id=team.id,
        icao24=icao24,
        tail_number=body.get("tail_number"),
        claimed_by_user_id=current_user.id,
        expires_at=now + timedelta(hours=2),
        flight_note=body.get("note"),
    )
    db.add(claim)
    db.commit()
    db.refresh(claim)
    return {"id": str(claim.id), "icao24": icao24, "claimed_at": claim.claimed_at, "expires_at": claim.expires_at}


@app.delete("/api/teams/claims/{icao24}", status_code=204)
async def release_claim(icao24: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    now = datetime.utcnow()
    claim = db.query(AircraftClaim).filter(
        AircraftClaim.team_id == team.id,
        AircraftClaim.icao24 == icao24.lower(),
        AircraftClaim.released_at.is_(None),
        AircraftClaim.expires_at > now,
    ).first()
    if claim:
        claim.released_at = now
        db.commit()


# ============================================================================
# TEAM SHIFTS & ON-DUTY
# ============================================================================

def _is_member_on_duty(team_id, user_id: str, db: Session) -> bool:
    now = datetime.utcnow()
    # Check manual override first (most recent wins)
    override = db.query(TeamDutyOverride).filter(
        TeamDutyOverride.team_id == team_id,
        TeamDutyOverride.user_id == user_id,
    ).order_by(TeamDutyOverride.created_at.desc()).first()
    if override:
        if override.override_until is None or override.override_until > now:
            return override.on_duty
    # Check shift schedule
    import pytz
    weekday = now.weekday()  # 0=Mon
    shifts = db.query(TeamShift).filter(TeamShift.team_id == team_id).all()
    for shift in shifts:
        if weekday not in (shift.days_of_week or []):
            continue
        try:
            tz = pytz.timezone(shift.timezone or "UTC")
            local_now = now.replace(tzinfo=pytz.utc).astimezone(tz)
            local_time = local_now.strftime("%H:%M")
            if shift.start_time <= local_time <= shift.end_time:
                assigned = db.query(TeamShiftMember).filter(
                    TeamShiftMember.shift_id == shift.id,
                    TeamShiftMember.user_id == user_id,
                ).first()
                if assigned:
                    return True
        except Exception:
            pass
    return False


@app.get("/api/teams/on-duty")
async def get_on_duty_members(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    result = []
    for m in team.members:
        on_duty = _is_member_on_duty(team.id, m.user_id, db)
        user = db.query(User).filter(User.id == m.user_id).first()
        result.append({
            "member_id": str(m.id),
            "user_id": str(m.user_id),
            "email": user.email if user else "",
            "role": m.role,
            "on_duty": on_duty,
        })
    return result


@app.put("/api/teams/members/{member_id}/duty", status_code=204)
async def set_member_duty(member_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.team_id == team.id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    # Members can only toggle themselves; admins/owners can toggle anyone
    cm = db.query(TeamMember).filter(TeamMember.user_id == current_user.id, TeamMember.team_id == team.id).first()
    if str(member.user_id) != str(current_user.id) and cm and cm.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Cannot change duty status for other members")
    body = await request.json()
    on_duty = body.get("on_duty", True)
    until_str = body.get("until")
    until = datetime.fromisoformat(until_str) if until_str else None
    override = TeamDutyOverride(
        team_id=team.id,
        user_id=member.user_id,
        on_duty=on_duty,
        override_until=until,
    )
    db.add(override)
    db.commit()


@app.get("/api/teams/shifts")
async def get_team_shifts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    shifts = db.query(TeamShift).filter(TeamShift.team_id == team.id).all()
    result = []
    for s in shifts:
        members = db.query(TeamShiftMember).filter(TeamShiftMember.shift_id == s.id).all()
        member_info = []
        for sm in members:
            user = db.query(User).filter(User.id == sm.user_id).first()
            member_info.append({"user_id": str(sm.user_id), "email": user.email if user else ""})
        result.append({
            "id": str(s.id),
            "name": s.name,
            "days_of_week": s.days_of_week,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "timezone": s.timezone,
            "color": s.color,
            "members": member_info,
            "created_at": s.created_at,
        })
    return result


@app.post("/api/teams/shifts", status_code=201)
async def create_team_shift(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_shifts")
    body = await request.json()
    shift = TeamShift(
        team_id=team.id,
        name=body["name"],
        days_of_week=body.get("days_of_week", [0, 1, 2, 3, 4]),
        start_time=body.get("start_time", "08:00"),
        end_time=body.get("end_time", "17:00"),
        timezone=body.get("timezone", "UTC"),
        color=body.get("color", "#22d3a3"),
    )
    db.add(shift)
    db.flush()
    for uid in body.get("user_ids", []):
        db.add(TeamShiftMember(shift_id=shift.id, user_id=uid))
    db.commit()
    db.refresh(shift)
    return {"id": str(shift.id), "name": shift.name, "days_of_week": shift.days_of_week,
            "start_time": shift.start_time, "end_time": shift.end_time,
            "timezone": shift.timezone, "color": shift.color}


@app.put("/api/teams/shifts/{shift_id}")
async def update_team_shift(shift_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_shifts")
    shift = db.query(TeamShift).filter(TeamShift.id == shift_id, TeamShift.team_id == team.id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    body = await request.json()
    for field in ["name", "days_of_week", "start_time", "end_time", "timezone", "color"]:
        if field in body:
            setattr(shift, field, body[field])
    db.commit()
    return {"id": str(shift.id)}


@app.put("/api/teams/shifts/{shift_id}/members", status_code=204)
async def set_shift_members(shift_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_shifts")
    shift = db.query(TeamShift).filter(TeamShift.id == shift_id, TeamShift.team_id == team.id).first()
    if not shift:
        raise HTTPException(status_code=404, detail="Shift not found")
    body = await request.json()
    db.query(TeamShiftMember).filter(TeamShiftMember.shift_id == shift.id).delete()
    for uid in body.get("user_ids", []):
        db.add(TeamShiftMember(shift_id=shift.id, user_id=uid))
    db.commit()


@app.delete("/api/teams/shifts/{shift_id}", status_code=204)
async def delete_team_shift(shift_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_shifts")
    shift = db.query(TeamShift).filter(TeamShift.id == shift_id, TeamShift.team_id == team.id).first()
    if shift:
        db.delete(shift)
        db.commit()


# ============================================================================
# EXPECTED ARRIVALS
# ============================================================================

@app.get("/api/teams/arrivals")
async def get_expected_arrivals(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=24)
    arrivals = db.query(ExpectedArrival).filter(
        ExpectedArrival.team_id == team.id,
        ExpectedArrival.status != "cancelled",
        ExpectedArrival.expected_at >= cutoff,
    ).order_by(ExpectedArrival.expected_at.asc()).all()
    result = []
    for a in arrivals:
        # Auto-mark late
        if a.status == "pending" and a.expected_at < now - timedelta(minutes=15):
            a.status = "late"
        result.append({
            "id": str(a.id),
            "tail_number": a.tail_number,
            "icao24": a.icao24,
            "expected_at": a.expected_at,
            "notes": a.notes,
            "reminder_minutes": a.reminder_minutes,
            "status": a.status,
            "linked_icao24": a.linked_icao24,
            "created_at": a.created_at,
        })
    db.commit()
    return result


@app.post("/api/teams/arrivals", status_code=201)
async def create_expected_arrival(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_arrivals")
    body = await request.json()
    arrival = ExpectedArrival(
        team_id=team.id,
        tail_number=body["tail_number"].upper().strip(),
        icao24=body.get("icao24", "").lower().strip() or None,
        expected_at=datetime.fromisoformat(body["expected_at"]),
        notes=body.get("notes"),
        reminder_minutes=int(body.get("reminder_minutes", 30)),
        created_by_user_id=current_user.id,
    )
    db.add(arrival)
    db.commit()
    db.refresh(arrival)
    return {"id": str(arrival.id), "tail_number": arrival.tail_number, "expected_at": arrival.expected_at}


@app.put("/api/teams/arrivals/{arrival_id}")
async def update_expected_arrival(arrival_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_arrivals")
    arrival = db.query(ExpectedArrival).filter(ExpectedArrival.id == arrival_id, ExpectedArrival.team_id == team.id).first()
    if not arrival:
        raise HTTPException(status_code=404, detail="Arrival not found")
    body = await request.json()
    for field in ["tail_number", "icao24", "notes", "reminder_minutes", "status"]:
        if field in body:
            setattr(arrival, field, body[field])
    if "expected_at" in body:
        arrival.expected_at = datetime.fromisoformat(body["expected_at"])
    db.commit()
    return {"id": str(arrival.id)}


@app.delete("/api/teams/arrivals/{arrival_id}", status_code=204)
async def delete_expected_arrival(arrival_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    arrival = db.query(ExpectedArrival).filter(ExpectedArrival.id == arrival_id, ExpectedArrival.team_id == team.id).first()
    if arrival:
        arrival.status = "cancelled"
        db.commit()


# ============================================================================
# ESCALATION CONFIG
# ============================================================================

@app.get("/api/teams/escalation-config")
async def get_escalation_config(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    config = db.query(EscalationConfig).filter(EscalationConfig.team_id == team.id).first()
    if not config:
        return {"enabled": False, "first_escalation_minutes": 5, "first_escalation_target": "all_admins",
                "second_escalation_minutes": 10, "second_escalation_target": "owner"}
    return {
        "id": str(config.id),
        "enabled": config.enabled,
        "first_escalation_minutes": config.first_escalation_minutes,
        "first_escalation_target": config.first_escalation_target,
        "second_escalation_minutes": config.second_escalation_minutes,
        "second_escalation_target": config.second_escalation_target,
    }


@app.put("/api/teams/escalation-config")
async def update_escalation_config(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "manage_routing")
    body = await request.json()
    config = db.query(EscalationConfig).filter(EscalationConfig.team_id == team.id).first()
    if not config:
        config = EscalationConfig(team_id=team.id)
        db.add(config)
    for field in ["enabled", "first_escalation_minutes", "first_escalation_target",
                  "second_escalation_minutes", "second_escalation_target"]:
        if field in body:
            setattr(config, field, body[field])
    db.commit()
    return {"message": "Escalation config updated"}


# ============================================================================
# ACTIVITY ACK
# ============================================================================

@app.post("/api/teams/activity/{log_id}/ack", status_code=204)
async def ack_activity(log_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from models import NotificationLog
    team = _get_user_team(current_user, db)
    _require_team_permission(current_user, team, db, "ack_alerts")
    log = db.query(NotificationLog).filter(NotificationLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    log.status = f"acked_by_{current_user.email}"
    db.commit()
    # Also ack any pending escalation for this alert
    escalation = db.query(AlertEscalation).filter(
        AlertEscalation.team_id == team.id,
        AlertEscalation.acked_at.is_(None),
    ).order_by(AlertEscalation.escalated_at.desc()).first()
    if escalation:
        escalation.acked_by_user_id = current_user.id
        escalation.acked_at = datetime.utcnow()
        db.commit()


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down FinalPing Cloud Backend...")
    await tracker.stop()
    logger.info("Shutdown complete")
