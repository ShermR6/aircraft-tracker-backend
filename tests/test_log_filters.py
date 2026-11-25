"""Logs filters take one value or several (comma-separated)."""
from datetime import datetime, timedelta

import test_api_integration as api
from test_api_integration import _fresh_db  # noqa: F401  (autouse: clean database per test)

client = api.client
db = api._shared


def _log(tail, kind, channel, minutes_ago):
    db.add(api.NotificationLog(user_id=api.USER_ID, aircraft_tail=tail, alert_type=kind, message="x",
                               integration_type=channel, status="sent", sent_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))


def _tails(query=""):
    return sorted({n["aircraft_tail"] + ":" + n["alert_type"] + ":" + n["integration_type"]
                   for n in client.get("/api/notifications/logs" + query).json()["logs"]})


def test_filters_take_several_values_and_one_still_works():
    _log("N1", "10nm", "discord", 1); _log("N2", "landing", "email", 2); _log("N3", "5nm", "discord", 3); _log("N1", "landing", "sms", 4)
    db.commit()
    assert len(_tails()) == 4
    assert _tails("?aircraft=N1") == ["N1:10nm:discord", "N1:landing:sms"]
    assert _tails("?aircraft=N1,N2") == ["N1:10nm:discord", "N1:landing:sms", "N2:landing:email"]
    assert _tails("?aircraft=N1,N3&alert_type=10nm,5nm") == ["N1:10nm:discord", "N3:5nm:discord"]
    assert _tails("?integration=email,sms") == ["N1:landing:sms", "N2:landing:email"]
    assert _tails("?aircraft=,") == _tails()          # empty pieces are ignored
