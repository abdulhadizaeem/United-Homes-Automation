import os
import json
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

_availability_cache: dict = {}
# Structure:
# {
#   tech_id (int): {
#     "synced_at": "ISO datetime string",
#     "tech_name": "string",
#     "days": {
#       "YYYY-MM-DD": {
#         "available": bool,
#         "day_off": bool,
#         "earliest_start": "HH:MM",   # 24h ET
#         "latest_end": "HH:MM",       # 24h ET
#         "blocked_ranges": [
#           {"start": "HH:MM", "end": "HH:MM", "reason": "string"}
#         ]
#       }
#     }
#   }
# }


def parse_calendar_rules(events: list, tech_name: str, dates_to_parse: list) -> dict:
    """
    Call gpt-4o-mini to parse raw Google Calendar events into structured availability.
    Returns dict keyed by ISO date string.
    On any failure, returns empty dict (caller uses defaults).
    """
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    SYSTEM_PROMPT = """
You are a scheduling parser for a home services company. Given raw Google Calendar events
for a technician, return per-day availability as JSON.

CRITICAL TIMEZONE RULE:
All event times are stored in UTC. The company operates in Eastern Time (ET = UTC-4 in summer).
Convert ALL event times from UTC to ET before assigning to a date.
Examples:
  Event at 02:00 UTC = 10pm ET previous day -> assign to PREVIOUS date
  Event at 06:00 UTC = 2am ET previous day -> assign to PREVIOUS date
  Event at 13:00 UTC = 9am ET same day -> assign to SAME date
  Event at 22:00 UTC = 6pm ET same day -> assign to SAME date

Return ONLY valid JSON. No explanation. No markdown. No code fences.

Output format:
{
  "YYYY-MM-DD": {
    "available": true,
    "day_off": false,
    "earliest_start": "09:00",
    "latest_end": "18:00",
    "blocked_ranges": [
      {"start": "HH:MM", "end": "HH:MM", "reason": "short label"}
    ]
  }
}

CLASSIFICATION RULES - apply in this exact order:

FULL DAY OFF -> available: false, day_off: true
  Titles: "OFF OFF OFF", "OFF", "CLOSED FOR WORK" (alone), "Holiday", "Vacation", "Day Off"

DAY-OF-WEEK RESTRICTION -> mark ALL matching weekdays in the input date list:
  "CLOSED FOR WORK - DO NOT SCHEDULE TUESDAY" -> all Tuesdays: available: false
  "DO NOT SCHEDULE [DAYNAME]" -> all that weekday: available: false
  "FRIDAY ONLY - [name]" -> all non-Fridays: available: false

TIME RESTRICTIONS:
  "DO NOT USE THIS TIME SLOT" -> blocked_range on its ET date/time
  "START BOOKING AFTER [TIME]" -> earliest_start = that time
  "AVAILABLE UNTIL [TIME]" -> latest_end = that time

REAL JOBS (never block the whole day, only add blocked_range):
  Any title with a street address (digits + street name): "2430 Sloop Rd", "5714 Barrington Dr"
  "REDO - [address]", "RESCADULED - [address]", "RESCHEDULED - [address]"
  "TENTATIVE - [address]"
  "QUOTE - [address]" (assume 60 min)

IGNORE COMPLETELY (do not affect availability):
  "Buddha Purnima", "Labour Day", "Eid al-Adha", "Bakrid", "Youm-i-Takbeer",
  "Birthday of ...", any public/religious holiday, "(No title)", "Consultation on ...",
  "FREE GUTTER CLEA...", "BDC DEV", "Flexito", empty titles

DEFAULT for dates not covered by any event:
  available: true, earliest_start: "09:00", latest_end: "18:00", blocked_ranges: []

When in doubt: be permissive. Only restrict what is explicitly stated.
"""

    events_payload = json.dumps([
        {
            "summary": e.get("summary", ""),
            "description": e.get("description", ""),
            "start": e.get("start", {}),
            "end": e.get("end", {}),
        }
        for e in events
    ], indent=2)

    user_msg = f"Technician: {tech_name}\nDates to evaluate: {dates_to_parse}\n\nEvents:\n{events_payload}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0,
            max_tokens=2000,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logging.error("[AVAIL CACHE] OpenAI parse failed for %s: %s", tech_name, e)
        return {}


def build_availability_cache() -> dict:
    """
    Fetch all technician sub-calendars, parse rules via OpenAI,
    store results in _availability_cache. Called every 30 seconds by background loop.
    Returns summary dict for logging.
    """
    from src.utils.db import get_admin_calendar_credentials, get_all_technicians, save_admin_calendar_credentials
    from src.api.calendar import _match_calendar_to_tech, _is_skip_calendar
    from src.services.google_calendar import GoogleCalendarService

    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).date()
    dates_to_parse = [(today + timedelta(days=i)).isoformat() for i in range(15)]

    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        logging.warning("[AVAIL CACHE] Admin calendar not connected")
        return {}

    cal = GoogleCalendarService(admin_creds["credentials"])

    # Refresh token
    try:
        updated = cal.get_updated_credentials()
        save_admin_calendar_credentials(
            admin_creds.get("provider", "google"),
            admin_creds.get("email", ""),
            updated
        )
    except Exception:
        pass

    now_utc = datetime.now(ZoneInfo("UTC"))
    future_utc = now_utc + timedelta(days=15)

    try:
        cal_list = cal.service.calendarList().list().execute()
        sub_calendars = cal_list.get("items", [])
    except Exception as e:
        logging.error("[AVAIL CACHE] Failed to list calendars: %s", e)
        return {}

    techs = get_all_technicians()
    results = {}
    _availability_cache.clear()

    for sub_cal in sub_calendars:
        cal_id = sub_cal.get("id", "")
        cal_name = sub_cal.get("summary", "")

        if sub_cal.get("primary") or _is_skip_calendar(cal_name):
            continue

        matched_tech = _match_calendar_to_tech(cal_name, techs)
        if not matched_tech:
            continue

        tech_id = matched_tech["id"]
        tech_name = matched_tech["name"]

        try:
            events_result = cal.service.events().list(
                calendarId=cal_id,
                timeMin=now_utc.isoformat(),
                timeMax=future_utc.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=200,
            ).execute()
            events = events_result.get("items", [])
        except Exception as e:
            logging.error("[AVAIL CACHE] Event fetch failed for '%s': %s", cal_name, e)
            continue

        parsed = parse_calendar_rules(events, tech_name, dates_to_parse)

        _availability_cache[tech_id] = {
            "synced_at": datetime.now(eastern).isoformat(),
            "tech_name": tech_name,
            "days": parsed,
        }
        results[tech_name] = {"tech_id": tech_id, "days_parsed": len(parsed)}
        logging.info("[AVAIL CACHE] Built cache for '%s': %d days", tech_name, len(parsed))

    return results


def get_tech_day_availability(tech_id: int, date_str: str) -> dict:
    """
    Returns availability for a tech on a specific date.
    Applies business-hour clamping regardless of what cache says.
    """
    eastern = ZoneInfo("America/New_York")
    d = date.fromisoformat(date_str)
    weekday = d.weekday()  # 0=Mon, 6=Sun

    # Sunday: always closed
    if weekday == 6:
        return {"available": False, "day_off": True, "earliest_start": "09:00",
                "latest_end": "18:00", "blocked_ranges": []}

    # Business hours by day
    biz_start = "09:00" if weekday == 5 else "08:00"   # Sat=9am, Mon-Fri=8am
    biz_end = "16:00" if weekday == 5 else "18:00"   # Sat=4pm, Mon-Fri=6pm

    default = {
        "available": True,
        "day_off": False,
        "earliest_start": biz_start,
        "latest_end": biz_end,
        "blocked_ranges": [],
    }

    day_data = _availability_cache.get(tech_id, {}).get("days", {}).get(date_str, {})
    merged = {**default, **day_data}

    # Clamp to business hours - calendar cannot extend past business close
    def _max_t(t1, t2):
        return t1 if t1 > t2 else t2

    def _min_t(t1, t2):
        return t1 if t1 < t2 else t2

    merged["earliest_start"] = _max_t(merged.get("earliest_start", biz_start), biz_start)
    merged["latest_end"] = _min_t(merged.get("latest_end", biz_end), biz_end)

    return merged


def get_all_availability_summary() -> str:
    """
    Human-readable availability summary for all techs for the next 7 days.
    Injected into Retell as {{technician_availability}} on call_started.
    """
    if not _availability_cache:
        return "Technician schedules are loading. Ask the customer for their preferred date and time and I will check availability."

    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).date()
    lines = []

    for tech_id, cache in _availability_cache.items():
        tech_name = cache.get("tech_name", f"Tech {tech_id}")
        tech_lines = [f"{tech_name}:"]
        for i in range(7):
            d = today + timedelta(days=i)
            date_str = d.isoformat()
            avail = get_tech_day_availability(tech_id, date_str)
            if avail.get("day_off") or not avail.get("available", True):
                tech_lines.append(f"  {d.strftime('%a %b %d')}: NOT AVAILABLE")
            else:
                start = avail.get("earliest_start", "09:00")
                end = avail.get("latest_end", "18:00")
                tech_lines.append(f"  {d.strftime('%a %b %d')}: available {start}-{end} ET")
        lines.append("\n".join(tech_lines))

    return "\n\n".join(lines)
