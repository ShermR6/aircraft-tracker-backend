"""What an account's plan allows right now.

Since 2026-11-13 there is one paid Personal plan. "starter", "premium" and
"pro" are the same plan under the names older licences were sold as, and an
account with no live licence is "free": it can sign in, but nothing is tracked
and no alerts go out until it starts a trial or subscribes.

Shared by main.py and tracker.py so the two cannot disagree.
"""

import os
from datetime import datetime, timedelta

from feeder import is_feeder_license
from models import License, NotificationLog

# A lapsed licence keeps working this long, so a late renewal webhook or a
# card retry does not cut anyone off mid-flight.
LICENSE_GRACE_PERIOD = timedelta(hours=48)

# Text messages are the one channel that costs money per alert.
SMS_MONTHLY_LIMIT = int(os.getenv("SMS_MONTHLY_LIMIT", "100"))

# The free plan a receiver earns by feeding adsb.lol (tier "feeder", since
# 2026-11-19): meant for hobbyists, so it is capped below what a flight school
# needs. Channels are in alert_rules.PLAN_CHANNELS["feeder"].
FEEDER_AIRCRAFT = 5


def tracked_aircraft_limit(tier: str):
    """How many of an account's aircraft the tracker follows (the oldest
    first), or None for all of them. Only the feeder plan can have more
    aircraft than it covers: an account that drops from the paid plan to it
    keeps its aircraft list, and the ones beyond the cap are paused."""
    return FEEDER_AIRCRAFT if tier == "feeder" else None


def live_license(db, user, now: datetime = None):
    """The licence that entitles this account today, or None."""
    if user is None or not getattr(user, "license_id", None):
        return None
    lic = db.query(License).filter(License.id == user.license_id).first()
    if lic is None or lic.status == "expired":
        return None
    # The free plan for feeding is already dated to the last day its rule
    # holds, so it gets no grace on top.
    grace = timedelta(0) if is_feeder_license(lic) else LICENSE_GRACE_PERIOD
    if lic.expires_at and lic.expires_at + grace < (now or datetime.utcnow()):
        return None
    return lic


def plan_tier(db, user) -> str:
    """The account's plan name, or "free" when it has no live licence."""
    lic = live_license(db, user)
    return lic.tier if lic else "free"


def sms_sent_this_month(db, user_id, now: datetime = None) -> int:
    now = now or datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return db.query(NotificationLog).filter(
        NotificationLog.user_id == user_id,
        NotificationLog.integration_type == "sms",
        NotificationLog.status == "sent",
        NotificationLog.sent_at >= month_start,
    ).count()


def sms_over_limit(db, user_id, now: datetime = None) -> bool:
    """True once this account has used its text messages for the calendar month (UTC)."""
    return sms_sent_this_month(db, user_id, now) >= SMS_MONTHLY_LIMIT
