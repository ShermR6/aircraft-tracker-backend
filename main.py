"""
FinalPing Cloud Backend
Main FastAPI application
"""

from fastapi import FastAPI, Depends, HTTPException, Request, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session
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
from models import User, License, Aircraft, AlertSetting, Integration, AirportConfig, SavedLocation, Team, TeamMember, TeamChannel, TeamAircraft, TeamAirportConfig, TeamAlertSetting, TeamInviteToken, TeamRole
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
WEBHOOK_INTERNAL_SECRET = os.getenv("WEBHOOK_INTERNAL_SECRET", "skyping-internal-secret")

# License duration
LICENSE_DURATION_DAYS = 30

# Grace period after expiry before blocking access — gives the Stripe renewal
# webhook time to arrive and extend the license before users go dark.
LICENSE_GRACE_PERIOD = timedelta(hours=2)

# Website URL for syncing license status
WEBSITE_URL = os.getenv("WEBSITE_URL", "https://finalpingapp.com")

# Tier feature limits (None = unlimited)
TIER_LIMITS = {
    "starter":      {"aircraft": 3,    "locations": 1,    "integrations": 2,    "channels": ["discord", "email"]},
    "premium":      {"aircraft": 7,    "locations": 5,    "integrations": 4,    "channels": ["discord", "email", "slack", "teams", "google_chat"]},
    "pro":          {"aircraft": 15,   "locations": None, "integrations": 5,    "channels": ["discord", "email", "slack", "teams", "google_chat", "sms", "telegram", "webhook"]},
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
    try:
        message = template.format(
            tail=tail, distance=f"{float(distance):.1f}",
            altitude=f"{float(altitude):.0f}", eta=eta,
            speed=f"{float(speed):.0f}", type=alert_type,
            time=datetime.utcnow().strftime('%H:%M'),
            airport=current_user.email,
        )
    except Exception:
        message = f"✈️ {tail} — {alert_type} (Ground Station)"

    from tracker import cloud_tracker
    alerts_sent = 0
    for integration in integrations:
        try:
            success = await cloud_tracker.send_via_integration(integration, message)
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
    db.commit()

    return {
        "email": email,
        "ground_station_enabled": enabled,
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
        activated_at=license.activated_at if license else None,
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

    license.expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
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

    # Stop tracking for any user currently using this license
    try:
        user = db.query(User).filter(User.license_id == license.id).first()
        if user:
            tracker.remove_user(str(user.id))
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

    license.tier = new_tier
    db.commit()
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


# ============================================================================
# STARTUP EVENT
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Start the global aircraft tracker on startup"""
    logger.info("Starting FinalPing Cloud Backend...")
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
            # Aircraft — added v1.0.6
            "ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS aircraft_type VARCHAR(100)",
            "ALTER TABLE aircraft ADD COLUMN IF NOT EXISTS alert_distances JSON",
            # AirportConfig — added v1.0.6
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS runway_info JSON",
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS approach_corridor_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS approach_runway_heading FLOAT",
            # TeamMember — added for custom roles
            "ALTER TABLE team_members ADD COLUMN IF NOT EXISTS custom_role_id UUID REFERENCES team_roles(id) ON DELETE SET NULL",
            # TeamChannel — value column (legacy channels stored config without top-level value)
        ]
        for sql in migrations:
            try:
                db.execute(text(sql))
            except Exception as mig_err:
                logger.warning("Migration skipped (%s): %s", sql[:60], mig_err)
        db.commit()

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
    return {
        "id": str(team.id),
        "name": team.name,
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


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Shutting down FinalPing Cloud Backend...")
    await tracker.stop()
    logger.info("Shutdown complete")
