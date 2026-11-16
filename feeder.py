"""The free plan for accounts whose receiver feeds adsb.lol.

A receiver (agent 3.9+) says with each heartbeat whether adsb.lol can see its
feed. Those minutes are counted per UTC day on the account. An account whose
receiver was feeding on at least FEED_DAYS_NEEDED of the FEED_WINDOW_DAYS days
before today has the plan for free, for as long as that stays true. The window
moves forward a day at a time, so the receiver has to keep feeding every week.

    A day counts when the receiver fed for FEED_MINUTES_PER_DAY minutes of it
    (18 hours, three quarters of the day). A day is judged once it is over:
    today is not in the window, so the plan never drops in the morning only
    to come back when today's hours are in.

    The day a receiver first feeds counts whatever its hours, and the days
    before it are credited, so a new feeder has the plan from its first
    heartbeat and keeps it by continuing to feed. That credit is given once
    per account.

The plan is an ordinary licence row (key FP-FEEDER-...), dated to the last day
the rule would still hold if the receiver stopped now. Nothing has to notice a
receiver going quiet: the date passes and the account is on no plan. A paid
plan always wins; the feeder licence is only attached when there is no other.

This is what the receiver reports about itself. It is not proof: someone
could send the heartbeats by hand. It is the same trust the rest of the
Ground Station API places in a device key.
"""

import hashlib
import os
from datetime import date, datetime, timedelta
from typing import Optional

from models import License

FEEDER_KEY_PREFIX = "FP-FEEDER-"
FEEDER_TIER = "starter"

FEED_WINDOW_DAYS = int(os.getenv("FEED_WINDOW_DAYS", "7"))
FEED_DAYS_NEEDED = int(os.getenv("FEED_DAYS_NEEDED", "5"))
FEED_MINUTES_PER_DAY = int(os.getenv("FEED_MINUTES_PER_DAY", "1080"))   # 18 h: 75% of the day
# Days of history kept on the account: the window, and a little to look back on.
FEED_DAYS_KEPT = 14
# Heartbeats arrive once a minute. Two inside this many seconds count once,
# so a restarting receiver cannot run a day's minutes up early.
FEED_MIN_GAP_SECONDS = 45


def is_feeder_license(lic) -> bool:
    return lic is not None and str(getattr(lic, "license_key", "") or "").startswith(FEEDER_KEY_PREFIX)


def feeder_key(user_id) -> str:
    """One feeder licence per account, named after it. Not a key anyone can buy or guess into."""
    return FEEDER_KEY_PREFIX + hashlib.sha256(str(user_id).encode()).hexdigest()[:12].upper()


def _day(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _clean(feed) -> dict:
    """The stored record, with anything unreadable dropped."""
    if not isinstance(feed, dict):
        return {"since": None, "days": {}, "last": None}
    days = {}
    raw = feed.get("days")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if _day(k) and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                days[k] = int(v)
    since = feed.get("since") if _day(feed.get("since")) else None
    last = feed.get("last") if isinstance(feed.get("last"), str) else None
    return {"since": since, "days": days, "last": last}


def record_feed_minute(feed, now: datetime) -> dict:
    """The record after one more heartbeat from a receiver that is feeding."""
    out = _clean(feed)
    if out["last"]:
        try:
            if (now - datetime.fromisoformat(out["last"])).total_seconds() < FEED_MIN_GAP_SECONDS:
                return out
        except ValueError:
            pass
    today = now.date()
    key = today.isoformat()
    out["days"][key] = min(out["days"].get(key, 0) + 1, 1440)
    out["since"] = out["since"] or key
    out["last"] = now.isoformat()
    oldest = today - timedelta(days=FEED_DAYS_KEPT)
    out["days"] = {k: v for k, v in out["days"].items() if _day(k) >= oldest}
    return out


def fed_days(feed) -> set:
    """The days that count as fed."""
    f = _clean(feed)
    days = {_day(k) for k, v in f["days"].items() if v >= FEED_MINUTES_PER_DAY}
    if f["since"] and f["days"].get(f["since"], 0) > 0:
        days.add(_day(f["since"]))      # the first day counts from its first minute
    return days


def window(on: date) -> list:
    """The days the rule looks at on `on`: the FEED_WINDOW_DAYS days before it."""
    return [on - timedelta(days=back) for back in range(1, FEED_WINDOW_DAYS + 1)]


def days_counted(feed, on: date) -> int:
    """Days in the window before `on` that count: fed, or credited as before the first feed."""
    f = _clean(feed)
    since = _day(f["since"])
    if since is None:
        return 0
    fed = fed_days(f)
    return sum(1 for d in window(on) if d in fed or d < since)


def qualifies(feed, on: date) -> bool:
    return days_counted(feed, on) >= FEED_DAYS_NEEDED


def valid_until(feed, today: date) -> Optional[date]:
    """The last day the rule still holds if the receiver never feeds again. None when it does not hold today."""
    if not qualifies(feed, today):
        return None
    last = today
    for ahead in range(1, FEED_WINDOW_DAYS + 1):
        d = today + timedelta(days=ahead)
        if not qualifies(feed, d):
            break
        last = d
    return last


def feeder_summary(feed, now: datetime) -> dict:
    """For the web app: where this account stands against the rule."""
    f = _clean(feed)
    today = now.date()
    fed = fed_days(f)
    until = valid_until(f, today)
    return {
        "days_fed": sum(1 for d in window(today) if d in fed),
        "days_counted": days_counted(f, today),
        "days_needed": FEED_DAYS_NEEDED,
        "window_days": FEED_WINDOW_DAYS,
        "minutes_today": f["days"].get(today.isoformat(), 0),
        "minutes_per_day": FEED_MINUTES_PER_DAY,
        "since": f["since"],
        "qualifies": until is not None,
        "valid_until": until.isoformat() if until else None,
    }


def _expiry(until: date) -> datetime:
    """Midnight UTC at the end of the last valid day."""
    return datetime.combine(until + timedelta(days=1), datetime.min.time())


def sync_feeder_license(db, user, feed, now: datetime = None) -> bool:
    """Attach, extend or remove this account's feeder licence to match its record.

    Leaves a live paid plan alone. Returns True when the account went from
    having a plan to not having one, or the other way, so the caller can start
    or stop tracking.
    """
    from plans import live_license      # here, not at the top: plans imports this module

    now = now or datetime.utcnow()
    current = db.query(License).filter(License.id == user.license_id).first() if user.license_id else None
    had_plan = live_license(db, user, now) is not None
    if current is not None and not is_feeder_license(current) and had_plan:
        return False                    # a paid plan wins

    until = valid_until(feed, now.date())
    if until is None:
        if is_feeder_license(current):
            user.license_id = None
            db.commit()
        return had_plan and is_feeder_license(current)

    key = feeder_key(user.id)
    lic = db.query(License).filter(License.license_key == key).first()
    expires = _expiry(until)
    if lic is None:
        lic = License(license_key=key, tier=FEEDER_TIER, status="active", activations_used=0,
                      activations_max=-1, activated_at=now, expires_at=expires, created_at=now)
        db.add(lic)
        db.flush()
    changed = lic.expires_at != expires or lic.status != "active" or lic.tier != FEEDER_TIER or user.license_id != lic.id
    if changed:
        lic.expires_at = expires
        lic.status = "active"
        lic.tier = FEEDER_TIER
        if lic.activated_at is None:
            lic.activated_at = now
        user.license_id = lic.id
        db.commit()
    return not had_plan
