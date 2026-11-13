"""What an account's plan allows right now.

Since 2026-11-13 there is one paid Personal plan. "starter", "premium" and
"pro" are the same plan under the names older licences were sold as, and an
account with no live licence is "free": it can sign in, but nothing is tracked
and no alerts go out until it starts a trial or subscribes.

Shared by main.py and tracker.py so the two cannot disagree.
"""

import os
from datetime import datetime, timedelta

from models import License, NotificationLog

# A lapsed licence keeps working this long, so a late renewal webhook or a
# card retry does not cut anyone off mid-flight.
LICENSE_GRACE_PERIOD = timedelta(hours=48)

# Text messages are the one channel that costs money per alert.
SMS_MONTHLY_LIMIT = int(os.getenv("SMS_MONTHLY_LIMIT", "100"))


def live_license(db, user, now: datetime = None):
    """The licence that entitles this account today, or None."""
    if user is None or not getattr(user, "license_id", None):
        return None
    lic = db.query(License).filter(License.id == user.license_id).first()
    if lic is None or lic.status == "expired":
        return None
    if lic.expires_at and lic.expires_at + LICENSE_GRACE_PERIOD < (now or datetime.utcnow()):
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
