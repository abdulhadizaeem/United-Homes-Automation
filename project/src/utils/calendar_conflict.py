"""Check the admin Google Calendar for scheduling conflicts."""
import logging
from datetime import datetime


def check_admin_calendar_conflict(start_dt: datetime, end_dt: datetime) -> bool:
    """Return True if the admin calendar is busy during [start_dt, end_dt].

    Uses Google Calendar freebusy API. Returns False (assume free) if:
    - Admin calendar credentials are not configured
    - Any error occurs (non-fatal -- do not block bookings on calendar errors)
    """
    try:
        from src.utils.db import get_admin_calendar_credentials
        admin_creds = get_admin_calendar_credentials()

        if not admin_creds or not admin_creds.get("connected"):
            return False

        if admin_creds.get("provider") != "google":
            return False

        creds_dict = admin_creds.get("credentials")
        if not creds_dict:
            return False

        from src.services.google_calendar import GoogleCalendarService
        cal = GoogleCalendarService(creds_dict)

        time_min = start_dt.isoformat() if start_dt.tzinfo else start_dt.isoformat() + "Z"
        time_max = end_dt.isoformat() if end_dt.tzinfo else end_dt.isoformat() + "Z"

        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }
        result = cal.service.freebusy().query(body=body).execute()
        busy_periods = result.get("calendars", {}).get("primary", {}).get("busy", [])

        if busy_periods:
            logging.info(
                "[CONFLICT] Admin calendar is BUSY %s-%s: %d overlap(s)",
                start_dt.strftime("%I:%M %p"), end_dt.strftime("%I:%M %p"), len(busy_periods),
            )
            return True

        return False

    except Exception as e:
        logging.warning("[CONFLICT] Admin calendar check failed (non-fatal): %s", e)
        return False
