import logging
import re
import uuid
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import date as date_type
from src.utils.distance import calculate_distance, estimate_tech_location
from src.utils.api_key_auth import verify_retell_api_key
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from src.services.google_calendar import GoogleCalendarService
from datetime import datetime as _dt
from src.utils.radar import geocode_address
from src.utils.db import (
    get_techs_with_appointments_for_day,
    get_technician,
    get_calendar_credentials,
    insert_appointment,
    delete_route_cache,
    update_appointment_calendar_event_id,
    get_job_scope_rules,
)
_DAY_OFF_RE = re.compile(
    r"\b(off|closed|no work|unavailable|is off|not available|holiday|vacation|personal)\b",
    re.IGNORECASE,
)

router = APIRouter()


def _find_tech_sub_calendar(admin_cal_service, tech_name: str) -> str:
    """Return the Google Calendar sub-calendar ID that belongs to *tech_name*.

    Falls back to ``'primary'`` if no match is found.
    """
    try:
        cal_list = admin_cal_service.calendarList().list().execute()
    except Exception as e:
        logging.warning("[CALENDAR] Could not list sub-calendars: %s", e)
        return "primary"

    tech_name_lower = tech_name.strip().lower()
    tech_first = tech_name_lower.split()[0] if tech_name_lower else ""

    for sc in cal_list.get("items", []):
        if sc.get("primary"):
            continue
        sc_name = (sc.get("summary") or "").strip().lower()
        sc_first = sc_name.split()[0] if sc_name else ""
        if (
            sc_name == tech_name_lower
            or tech_name_lower in sc_name
            or sc_name in tech_name_lower
            or (len(tech_first) >= 4 and tech_first == sc_first)
            or (len(tech_first) >= 4 and len(sc_first) >= 4 and tech_first[:4] == sc_first[:4])
        ):
            logging.info(
                "[CALENDAR] Matched tech '%s' -> sub-calendar '%s' (id=%s)",
                tech_name, sc.get("summary"), sc["id"],
            )
            return sc["id"]

    return "primary"


def delete_appointment_calendar_event(appt: dict):
    event_id = appt.get("calendar_event_id")
    if not event_id:
        logging.info("[CALENDAR] No calendar_event_id found on appointment %s; skipping deletion", appt.get("id"))
        return

    from src.utils.db import get_admin_calendar_credentials, get_technician
    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        logging.warning("[CALENDAR] Admin calendar credentials not connected; cannot delete event")
        return

    provider = admin_creds.get("provider")
    try:
        if provider == "google":
            from src.services.google_calendar import GoogleCalendarService
            admin_cal = GoogleCalendarService(admin_creds["credentials"])
            tech_name = "primary"
            if appt.get("technician_id"):
                tech = get_technician(appt["technician_id"])
                if tech:
                    tech_name = tech.get("name", "primary")
            target_calendar_id = _find_tech_sub_calendar(admin_cal.service, tech_name)
            logging.info(
                "[CALENDAR] Deleting event ID %s from Google sub-calendar %s for tech '%s'",
                event_id, target_calendar_id, tech_name
            )
            success = admin_cal.delete_event(event_id, calendar_id=target_calendar_id)
            if success:
                logging.info("[CALENDAR] Google Calendar event deleted successfully")
            else:
                logging.warning("[CALENDAR] Failed to delete Google Calendar event")
        elif provider == "outlook":
            from src.services.outlook_calendar import OutlookCalendarService
            admin_cal = OutlookCalendarService(admin_creds["credentials"])
            logging.info("[CALENDAR] Deleting event ID %s from Outlook Calendar", event_id)
            success = admin_cal.delete_event(event_id)
            if success:
                logging.info("[CALENDAR] Outlook Calendar event deleted successfully")
            else:
                logging.warning("[CALENDAR] Failed to delete Outlook Calendar event")
    except Exception as err:
        logging.error("[CALENDAR] Exception during calendar event deletion: %s", err)


@router.post("/get-current-datetime")
def get_current_datetime(_auth=Depends(verify_retell_api_key)):
    """Return the current date and time in Eastern Time for the agent."""
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    return {
        "current_date": now.strftime("%A, %B %d, %Y"),
        "current_time": now.strftime("%I:%M %p ET"),
        "iso_date": now.strftime("%Y-%m-%d"),
        "day_of_week": now.strftime("%A"),
    }


@router.post("/simulate-manager-check")
def simulate_manager_check(_auth=Depends(verify_retell_api_key)):
    """Simulate a manager approval check with an 8-second delay.

    This endpoint exists solely to create a realistic pause on the call
    while the agent pretends to check with a manager about a discount.
    """
    import time
    logging.info("[MANAGER CHECK] Starting 8-second simulated delay...")
    time.sleep(8)
    logging.info("[MANAGER CHECK] Delay complete, returning approval.")
    return {
        "approved": True,
        "message": "Manager approved an additional 10% discount.",
    }


class VerifyZipRequest(BaseModel):
    zip_code: str


@router.post("/verify-zip")
def verify_zip(request: VerifyZipRequest, _auth=Depends(verify_retell_api_key)):
    import os
    import requests as http_requests

    api_key = os.getenv("RADAR_API_KEY", "")
    zip_input = request.zip_code.strip()

    CHARLOTTE_METRO_CITIES = {
        # Mecklenburg County
        "charlotte", "pineville", "matthews", "mint hill", "huntersville",
        "cornelius", "davidson", "ballantyne", "steele creek", "university city",
        # Cabarrus County
        "concord", "kannapolis", "harrisburg", "locust", "albemarle",
        # Union County
        "monroe", "indian trail", "stallings", "waxhaw", "weddington",
        "marvin", "wesley chapel", "wingate", "marshville",
        # Gaston County
        "gastonia", "belmont", "mount holly", "cramerton", "lowell",
        "bessemer city", "kings mountain", "dallas", "stanley",
        # Iredell County
        "mooresville", "statesville", "troutman", "love valley",
        # Lincoln County
        "lincolnton",
        # Rowan County
        "salisbury", "rockwell", "china grove",
        # Lake Norman / Denver area (Lincoln / Iredell)
        "denver", "lake norman", "sherrills ford",
        # York County SC
        "rock hill", "fort mill", "tega cay", "lake wylie", "clover",
        "york", "sharon",
        # Nearby communities
        "shelby", "mount holly", "cramerton",
    }

    # Secondary bounding box for edge cases where Radar returns an unusual
    # community name that is still geographically inside the Charlotte metro
    LAT_MIN, LAT_MAX = 34.75, 35.75
    LNG_MIN, LNG_MAX = -81.65, -80.10

    try:
        resp = http_requests.get(
            "https://api.radar.io/v1/geocode/forward",
            headers={"Authorization": api_key},
            params={"query": zip_input},
            timeout=8,
        )
        data = resp.json()
        addresses = data.get("addresses", [])

        if not addresses:
            logging.warning("[ZIP] No results for zip: %s", zip_input)
            return {
                "serviced": False,
                "zip_code": zip_input,
                "message": "We could not locate that zip code. Could you double-check the zip?",
            }

        addr = addresses[0]
        city = addr.get("city", "")
        state = addr.get("state", "")
        country = addr.get("countryCode", "")
        lat = addr.get("latitude", 0)
        lng = addr.get("longitude", 0)

        logging.info("[ZIP] %s -> %s, %s %s (%.4f, %.4f)", zip_input, city, state, country, lat, lng)

        city_match = city.lower() in CHARLOTTE_METRO_CITIES
        bbox_match = (
            country == "US"
            and LAT_MIN <= lat <= LAT_MAX
            and LNG_MIN <= lng <= LNG_MAX
        )
        in_area = city_match or bbox_match


        if in_area:
            return {
                "serviced": True,
                "zip_code": zip_input,
                "city": city,
                "state": state,
                "message": f"Great, we service the {city} area!",
            }
        else:
            return {
                "serviced": False,
                "zip_code": zip_input,
                "city": city,
                "state": state,
                "message": f"Unfortunately we don't currently service {city}, {state}. We cover the greater Charlotte, NC metro area.",
            }
    except Exception as e:
        logging.error("[ZIP] Error: %s", e)
        return {
            "serviced": True,
            "zip_code": zip_input,
            "message": "Zip code check is unavailable right now. Let's continue with your service request.",
        }


class VerifyAddressRequest(BaseModel):
    messy_input: str


class VerifyAddressResponse(BaseModel):
    formatted_address: str
    latitude: float
    longitude: float
    confidence: Optional[str] = None


class FindTechnicianRequest(BaseModel):
    service_type: str
    confirmed_latitude: float
    confirmed_longitude: float
    requested_date: str  # YYYY-MM-DD format
    preferred_time: Optional[str] = None  # HH:MM in 24-hour format, e.g. "13:00"
    job_units: Optional[int] = None  # number of systems/fireplaces/areas for heavy-job check


class TechnicianInfo(BaseModel):
    id: int
    name: str
    distance_miles: float


class FindTechnicianResponse(BaseModel):
    success: bool
    technician: TechnicianInfo = None
    available: bool
    time_slot: str = None
    alternative_slots: list = []
    message: str = None


def format_time_for_ai(dt: datetime) -> str:
    """Formats a datetime into a natural language string for the AI to read."""
    eastern = ZoneInfo("America/New_York")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=eastern)
    else:
        dt = dt.astimezone(eastern)
        
    now = datetime.now(eastern)
    if dt.date() == now.date():
        day_str = "today"
    elif dt.date() == (now + timedelta(days=1)).date():
        day_str = "tomorrow"
    else:
        day_str = dt.strftime("on %A, %B %d")
    
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    return f"{day_str} at {time_str}"


class BookAppointmentRequest(BaseModel):
    customer_name: str
    customer_phone: str
    customer_email: str
    technician_id: int
    service_type: str
    address: str
    latitude: float
    longitude: float
    start_time: datetime
    duration_minutes: int
    quoted_price: Optional[float] = None
    discount_applied: Optional[str] = None
    job_units: Optional[int] = None  # passed so backend can insert a blocked second slot


class BookAppointmentResponse(BaseModel):
    success: bool
    appointment_id: str = None
    technician: str = None
    time: str = None
    message: str


@router.post("/verify-address")
def verify_address(request: VerifyAddressRequest, _auth=Depends(verify_retell_api_key)):
    try:
        result = geocode_address(request.messy_input)
        if not result:
            logging.warning("[GEOCODE] No result for input: %s", request.messy_input)
            return {
                "verified": False,
                "formatted_address": None,
                "latitude": None,
                "longitude": None,
                "confidence": None,
                "low_confidence": False,
                "message": "I could not locate that address. Could you provide the street number, street name, and city or zip code?",
            }

        confidence = result.get("confidence", "")
        low_confidence = confidence == "fallback"

        logging.info(
            "[GEOCODE] Resolved '%s' -> '%s' (confidence=%s)",
            request.messy_input, result["formatted_address"], confidence,
        )

        return {
            "verified": True,
            "formatted_address": result["formatted_address"],
            "latitude": result["latitude"],
            "longitude": result["longitude"],
            "confidence": confidence,
            "low_confidence": low_confidence,
            "message": None,
        }
    except Exception as e:
        logging.error("[GEOCODE] Unexpected error: %s", e)
        return {
            "verified": False,
            "formatted_address": None,
            "latitude": None,
            "longitude": None,
            "confidence": None,
            "low_confidence": False,
            "message": "Address verification is temporarily unavailable. Please try again.",
        }


def get_required_gap(service_type):
    svc = (service_type or "").lower().strip()
    if svc == "air_duct":
        return 180
    elif svc == "power_washing":
        return 150
    return 120

def to_eastern(dt_val):
    if isinstance(dt_val, str):
        from dateutil.parser import parse
        dt = parse(dt_val)
    else:
        dt = dt_val
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(ZoneInfo("America/New_York"))

def get_service_type_from_summary(summary):
    summary_lower = (summary or "").lower()
    for svc in ["air_duct", "chimney", "dryer_vent", "gutter", "power_washing"]:
        if svc.replace("_", " ") in summary_lower or svc in summary_lower:
            return svc
    _BLOCK_KEYWORDS = ["off", "closed", "do not", "no work", "unavailable", "is off", "don't schedule", "not available"]
    if any(kw in summary_lower for kw in _BLOCK_KEYWORDS):
        return "blocked"
    return "other"

def check_intervals_conflict(start_1, end_1, type_1, start_2, end_2, type_2):
    s1 = to_eastern(start_1)
    e1 = to_eastern(end_1)
    s2 = to_eastern(start_2)
    e2 = to_eastern(end_2)
    if s1 <= s2:
        first_end = e1
        second_start = s2
    else:
        first_end = e2
        second_start = s1
    gap_required = max(get_required_gap(type_1), get_required_gap(type_2))
    return (second_start - first_end) < timedelta(minutes=gap_required)

def get_tech_calendar_events(tech_id, tech_name, day_date, _cache=None):
    if _cache is None:
        _cache = {}
    cache_key = (tech_id, day_date)
    if cache_key in _cache:
        return _cache[cache_key]
    try:
        creds = get_calendar_credentials(tech_id)
        day_start = _dt(day_date.year, day_date.month, day_date.day, 0, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        day_end = _dt(day_date.year, day_date.month, day_date.day, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
        
        intervals = []
        
        if creds and creds.get("calendar_connected"):
            provider = creds.get("calendar_provider")
            creds_dict = creds.get("calendar_credentials", {})
            logging.info(f"[CAL-CHECK] Tech {tech_id} ({tech_name}): using DIRECT {provider} calendar")
            if provider == "google":
                from src.services.google_calendar import GoogleCalendarService
                cal = GoogleCalendarService(creds_dict)
                events_result = cal.service.events().list(
                    calendarId="primary",
                    timeMin=day_start.isoformat(),
                    timeMax=day_end.isoformat(),
                    singleEvents=True,
                    maxResults=50,
                ).execute()
                raw_items = events_result.get("items", [])
                logging.info(f"[CAL-CHECK] Tech {tech_id}: Google returned {len(raw_items)} raw events")
                for ev in raw_items:
                    if ev.get("status") == "cancelled":
                        continue
                    s = ev.get("start", {}).get("dateTime")
                    e = ev.get("end", {}).get("dateTime")
                    summary = ev.get("summary", "")
                    if s and e:
                        from dateutil.parser import parse as _parse
                        parsed_s, parsed_e = _parse(s), _parse(e)
                        logging.info(f"[CAL-CHECK] Tech {tech_id}: event '{summary}' {parsed_s} -> {parsed_e}")
                        intervals.append((parsed_s, parsed_e, summary))
                    else:
                        logging.info(f"[CAL-CHECK] Tech {tech_id}: skipped all-day event '{summary}'")
            elif provider == "outlook":
                from src.services.outlook_calendar import OutlookCalendarService
                cal = OutlookCalendarService(creds_dict)
                events = cal.list_events(day_start, day_end)
                logging.info(f"[CAL-CHECK] Tech {tech_id}: Outlook returned {len(events)} events")
                for ev in events:
                    if ev.get("status") == "cancelled":
                        continue
                    s = ev.get("start")
                    e = ev.get("end")
                    if s and e:
                        from dateutil.parser import parse as _parse
                        intervals.append((_parse(s), _parse(e), ev.get("summary", "")))
        else:
            logging.info(f"[CAL-CHECK] Tech {tech_id} ({tech_name}): no direct calendar, trying ADMIN sub-calendar")
            from src.utils.db import get_admin_calendar_credentials
            admin_creds = get_admin_calendar_credentials()
            if admin_creds and admin_creds.get("connected") and admin_creds.get("provider") == "google":
                from src.services.google_calendar import GoogleCalendarService
                admin_cal = GoogleCalendarService(admin_creds["credentials"])
                cal_id = _find_tech_sub_calendar(admin_cal.service, tech_name)
                logging.info(f"[CAL-CHECK] Tech {tech_id}: admin sub-calendar ID = '{cal_id}'")
                if cal_id != "primary":
                    events_result = admin_cal.service.events().list(
                        calendarId=cal_id,
                        timeMin=day_start.isoformat(),
                        timeMax=day_end.isoformat(),
                        singleEvents=True,
                        maxResults=50,
                    ).execute()
                    raw_items = events_result.get("items", [])
                    logging.info(f"[CAL-CHECK] Tech {tech_id}: admin calendar returned {len(raw_items)} raw events")
                    for ev in raw_items:
                        if ev.get("status") == "cancelled":
                            continue
                        s = ev.get("start", {}).get("dateTime")
                        e = ev.get("end", {}).get("dateTime")
                        summary = ev.get("summary", "")
                        if s and e:
                            from dateutil.parser import parse as _parse
                            parsed_s, parsed_e = _parse(s), _parse(e)
                            logging.info(f"[CAL-CHECK] Tech {tech_id}: event '{summary}' {parsed_s} -> {parsed_e}")
                            intervals.append((parsed_s, parsed_e, summary))
                else:
                    logging.warning(f"[CAL-CHECK] Tech {tech_id}: no sub-calendar found for '{tech_name}', skipping calendar check")
            else:
                logging.warning(f"[CAL-CHECK] Tech {tech_id}: no admin calendar credentials available")
        logging.info(f"[CAL-CHECK] Tech {tech_id}: total calendar events found = {len(intervals)}")
        _cache[cache_key] = intervals
        return intervals
    except Exception as e:
        logging.error(f"[CAL-CHECK] Error fetching calendar events for tech {tech_id} ({tech_name}): {e}", exc_info=True)
        _cache[cache_key] = []
        return []

SERVICE_DURATIONS = {
    "chimney": 60,
    "dryer_vent": 60,
    "gutter": 60,
    "power_washing": 90,
    "air_duct": 120,
}

FIXED_SLOTS = [(8, 0), (11, 0), (14, 0), (17, 0)]
OVERFLOW_SLOTS = []


@router.post("/find-technician-availability", response_model=FindTechnicianResponse)
def find_technician_availability(request: FindTechnicianRequest, _auth=Depends(verify_retell_api_key)):
    try:
        try:
            req_date = date_type.fromisoformat(request.requested_date)
        except ValueError:
            return FindTechnicianResponse(
                success=False, available=False,
                message="Invalid date format. Please use YYYY-MM-DD."
            )

        eastern = ZoneInfo("America/New_York")
        service_duration = SERVICE_DURATIONS.get(request.service_type, 60)
        now_et = datetime.now(eastern)

        techs = get_techs_with_appointments_for_day(request.service_type, req_date)
        if not techs:
            return FindTechnicianResponse(
                success=False, available=False,
                message="I'm sorry, I couldn't find any technicians available for that service type."
            )

        _cal_cache = {}
        candidates = []
        _scope_rules = get_job_scope_rules()
        is_heavy = bool(
            request.job_units
            and request.service_type in _scope_rules
            and request.job_units >= _scope_rules[request.service_type]["units_threshold"]
        )

        def _ensure_dt(val):
            if isinstance(val, str): return datetime.fromisoformat(val)
            return val

        def _slot_conflicts(candidate, appts):
            candidate_end = candidate + timedelta(minutes=service_duration)
            for a in appts:
                a_start = _ensure_dt(a["start_time"])
                a_end = _ensure_dt(a["end_time"])
                if check_intervals_conflict(candidate, candidate_end, request.service_type, a_start, a_end, a.get("service_type")):
                    logging.info(f"[SLOT-CHECK] {candidate.strftime('%I:%M %p')} BLOCKED by DB appt: {a.get('customer_name','')} {a_start}-{a_end} ({a.get('service_type','')})")
                    return True
            return False

        def _cal_slot_conflicts(tech_id, tech_name, slot, duration_mins, day_date):
            slot_end = slot + timedelta(minutes=duration_mins)
            intervals = get_tech_calendar_events(tech_id, tech_name, day_date, _cal_cache)
            for ev_start, ev_end, ev_summary in intervals:
                if check_intervals_conflict(slot, slot_end, request.service_type, ev_start, ev_end, get_service_type_from_summary(ev_summary)):
                    logging.info(f"[SLOT-CHECK] {slot.strftime('%I:%M %p')} BLOCKED by calendar event: '{ev_summary}' {ev_start}-{ev_end}")
                    return True
            return False

        def _departure_point(slot, appts, tech):
            prev = []
            for a in appts:
                ae = _ensure_dt(a["end_time"])
                if ae.tzinfo is None: ae = ae.replace(tzinfo=ZoneInfo("America/New_York"))
                if ae.astimezone(eastern) <= slot:
                    prev.append(a)
            if prev:
                last = max(prev, key=lambda a: _ensure_dt(a["end_time"]))
                lat, lon = last.get("latitude"), last.get("longitude")
                if lat and lon: return float(lat), float(lon)
            return float(tech["home_latitude"]), float(tech["home_longitude"])

        for tech in techs:
            if not tech.get("home_latitude") or not tech.get("home_longitude"): continue

            appointments = sorted(tech["appointments"], key=lambda a: _ensure_dt(a["start_time"]))
            logging.info(f"[AVAILABILITY] Checking tech {tech['id']} ({tech['name']}): {len(appointments)} DB appts on {req_date}")
            for a in appointments:
                logging.info(f"[AVAILABILITY]   DB appt: {a.get('customer_name','')} | {a.get('service_type','')} | {_ensure_dt(a['start_time'])} -> {_ensure_dt(a['end_time'])} | status={a.get('status','')}")
            
            cal_events = get_tech_calendar_events(tech["id"], tech["name"], req_date, _cal_cache)
            is_off = False
            for a in appointments:
                if _DAY_OFF_RE.search(a.get("customer_name") or "") or _DAY_OFF_RE.search(a.get("service_type") or ""):
                    is_off = True
                    break
            for ev_start, ev_end, ev_summary in cal_events:
                if _DAY_OFF_RE.search(ev_summary):
                    is_off = True
                    break
            if is_off:
                logging.info(f"[AVAILABILITY] Tech {tech['id']} ({tech['name']}): SKIPPED (day off)")
                continue

            for h, m in FIXED_SLOTS + OVERFLOW_SLOTS:
                slot = datetime(req_date.year, req_date.month, req_date.day, h, m, tzinfo=eastern)
                if slot <= now_et: continue

                db_conflict = _slot_conflicts(slot, appointments)
                if not db_conflict:
                    cal_conflict = _cal_slot_conflicts(tech["id"], tech["name"], slot, service_duration, req_date)
                    if not cal_conflict:
                        if is_heavy:
                            next_slot = slot + timedelta(hours=3)
                            if _slot_conflicts(next_slot, appointments) or _cal_slot_conflicts(tech["id"], tech["name"], next_slot, service_duration, req_date):
                                continue

                        d_lat, d_lon = _departure_point(slot, appointments, tech)
                        dist = calculate_distance(d_lat, d_lon, request.confirmed_latitude, request.confirmed_longitude)
                        logging.info(f"[AVAILABILITY] Tech {tech['id']} ({tech['name']}): slot {slot.strftime('%I:%M %p')} ✅ AVAILABLE (dist={dist:.1f}mi)")
                        
                        candidates.append({
                            "tech": tech,
                            "slot": slot,
                            "distance": dist
                        })

        if not candidates:
            return FindTechnicianResponse(
                success=False, available=False,
                message="I'm sorry, all our technicians are fully booked for that day. Would you like to try another date?"
            )

        candidates.sort(key=lambda c: (c["distance"], c["slot"]))
        best = candidates[0]

        tech_alts = [
            format_time_for_ai(c["slot"]) 
            for c in candidates 
            if c["tech"]["id"] == best["tech"]["id"] and c["slot"] != best["slot"]
        ]

        time_str = format_time_for_ai(best["slot"])
        alt_phrase = f" I also have {tech_alts[0]} available if that works better." if tech_alts else ""

        response_msg = f"Yes, {best['tech']['name']} is available {time_str}.{alt_phrase} Would you like me to book that for you?"
        logging.info(f"[RETELL-OUTPUT] Sending to Retell: {response_msg}")

        return FindTechnicianResponse(
            success=True,
            technician=TechnicianInfo(
                id=best["tech"]["id"],
                name=best["tech"]["name"],
                distance_miles=round(best["distance"], 2),
            ),
            available=True,
            time_slot=best["slot"].isoformat(),
            alternative_slots=tech_alts[:3],
            message=response_msg,
        )

    except Exception as e:
        logging.error("[AVAILABILITY] Error: %s", e, exc_info=True)
        return FindTechnicianResponse(
            success=False,
            available=False,
            message="Error checking availability. Please try again.",
        )

@router.post("/book-appointment", response_model=BookAppointmentResponse)
def book_appointment(request: BookAppointmentRequest, _auth=Depends(verify_retell_api_key)):
    logging.info(f"[BOOKING] Request: customer={request.customer_name}, phone={request.customer_phone}, tech_id={request.technician_id}, service={request.service_type}, time={request.start_time}, address={request.address}")

    try:
        tech = get_technician(request.technician_id)
        logging.info(f"[BOOKING] Tech lookup: {'found ' + tech['name'] if tech else 'NOT FOUND'} (id={request.technician_id})")

        if not tech:
            raise HTTPException(status_code=404, detail="Technician not found")

        end_time = request.start_time + timedelta(minutes=request.duration_minutes)

        try:
            is_available = True
            req_date = request.start_time.date()
            from src.utils.db import get_db_connection
            from psycopg2.extras import RealDictCursor
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT * FROM appointments
                WHERE technician_id = %s
                AND start_time >= %s - INTERVAL '1 day'
                AND start_time < %s + INTERVAL '2 days'
                AND status IN ('scheduled', 'blocked')
            """, (request.technician_id, req_date, req_date))
            db_appts = cur.fetchall()
            cur.close()
            conn.close()

            for a in db_appts:
                a_start = a["start_time"]
                a_end = a["end_time"]
                if check_intervals_conflict(request.start_time, end_time, request.service_type, a_start, a_end, a.get("service_type")):
                    is_available = False
                    break

            if is_available:
                creds = get_calendar_credentials(request.technician_id)
                if creds and creds.get("calendar_connected"):
                    provider = creds.get("calendar_provider")
                    creds_dict = creds.get("calendar_credentials", {})
                    if provider == "google":
                        from src.services.google_calendar import GoogleCalendarService
                        cal = GoogleCalendarService(creds_dict)
                        day_start = datetime(req_date.year, req_date.month, req_date.day, 0, 0, 0, tzinfo=ZoneInfo("America/New_York"))
                        day_end = datetime(req_date.year, req_date.month, req_date.day, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
                        events_result = cal.service.events().list(
                            calendarId="primary",
                            timeMin=day_start.isoformat(),
                            timeMax=day_end.isoformat(),
                            singleEvents=True,
                            maxResults=50,
                        ).execute()
                        for ev in events_result.get("items", []):
                            if ev.get("status") == "cancelled":
                                continue
                            s = ev.get("start", {}).get("dateTime")
                            e = ev.get("end", {}).get("dateTime")
                            if s and e:
                                from dateutil.parser import parse as _parse
                                ev_start, ev_end = _parse(s), _parse(e)
                                if check_intervals_conflict(request.start_time, end_time, request.service_type, ev_start, ev_end, get_service_type_from_summary(ev.get("summary"))):
                                    is_available = False
                                    break
                    elif provider == "outlook":
                        from src.services.outlook_calendar import OutlookCalendarService
                        cal = OutlookCalendarService(creds_dict)
                        day_start = datetime(req_date.year, req_date.month, req_date.day, 0, 0, 0, tzinfo=ZoneInfo("America/New_York"))
                        day_end = datetime(req_date.year, req_date.month, req_date.day, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
                        events = cal.list_events(day_start, day_end)
                        for ev in events:
                            if ev.get("status") == "cancelled":
                                continue
                            s = ev.get("start")
                            e = ev.get("end")
                            if s and e:
                                from dateutil.parser import parse as _parse
                                ev_start, ev_end = _parse(s), _parse(e)
                                if check_intervals_conflict(request.start_time, end_time, request.service_type, ev_start, ev_end, get_service_type_from_summary(ev.get("summary"))):
                                    is_available = False
                                    break
                else:
                    from src.utils.db import get_admin_calendar_credentials
                    admin_creds = get_admin_calendar_credentials()
                    if admin_creds and admin_creds.get("connected") and admin_creds.get("provider") == "google":
                        from src.services.google_calendar import GoogleCalendarService
                        admin_cal = GoogleCalendarService(admin_creds["credentials"])
                        target_calendar_id = _find_tech_sub_calendar(admin_cal.service, tech["name"])
                        if target_calendar_id != "primary":
                            day_start = datetime(req_date.year, req_date.month, req_date.day, 0, 0, 0, tzinfo=ZoneInfo("America/New_York"))
                            day_end = datetime(req_date.year, req_date.month, req_date.day, 23, 59, 59, tzinfo=ZoneInfo("America/New_York"))
                            events_result = admin_cal.service.events().list(
                                calendarId=target_calendar_id,
                                timeMin=day_start.isoformat(),
                                timeMax=day_end.isoformat(),
                                singleEvents=True,
                                maxResults=50,
                            ).execute()
                            for ev in events_result.get("items", []):
                                if ev.get("status") == "cancelled":
                                    continue
                                s = ev.get("start", {}).get("dateTime")
                                e = ev.get("end", {}).get("dateTime")
                                if s and e:
                                    from dateutil.parser import parse as _parse
                                    ev_start, ev_end = _parse(s), _parse(e)
                                    if check_intervals_conflict(request.start_time, end_time, request.service_type, ev_start, ev_end, get_service_type_from_summary(ev.get("summary"))):
                                        is_available = False
                                        break
            if not is_available:
                raise HTTPException(
                    status_code=409,
                    detail="The technician is no longer available at this time slot due to a scheduling conflict."
                )
        except HTTPException:
            raise
        except Exception as e:
            logging.warning(f"[BOOKING] Calendar availability check failed: {e}")

        appointment_id = str(uuid.uuid4())
        logging.info(f"[BOOKING] Generated appointment_id={appointment_id}")

        insert_appointment(
            calendar_event_id=appointment_id,
            technician_id=request.technician_id,
            customer_name=request.customer_name,
            customer_phone=request.customer_phone,
            customer_email=request.customer_email,
            service_type=request.service_type,
            address=request.address,
            latitude=request.latitude,
            longitude=request.longitude,
            start_time=request.start_time,
            end_time=end_time,
            duration_minutes=request.duration_minutes,
            status="scheduled",
            quoted_price=request.quoted_price,
            discount_applied=request.discount_applied,
        )

        # Heavy job: block the next fixed slot for this technician
        if request.job_units:
            _rules = get_job_scope_rules()
            _is_heavy = (
                request.service_type in _rules
                and request.job_units >= _rules[request.service_type]["units_threshold"]
            )
            if _is_heavy:
                eastern = ZoneInfo("America/New_York")
                appt_date = request.start_time.date()
                all_fixed = [
                    datetime(appt_date.year, appt_date.month, appt_date.day, h, m, tzinfo=eastern)
                    for h, m in FIXED_SLOTS
                ]
                next_slots = [s for s in all_fixed if s > request.start_time]
                if next_slots:
                    blocked_start = min(next_slots)
                    blocked_end = blocked_start + timedelta(minutes=request.duration_minutes)
                    insert_appointment(
                        calendar_event_id=str(uuid.uuid4()),
                        technician_id=request.technician_id,
                        customer_name=request.customer_name,
                        customer_phone=request.customer_phone,
                        customer_email=request.customer_email,
                        service_type=request.service_type,
                        address=request.address,
                        latitude=request.latitude,
                        longitude=request.longitude,
                        start_time=blocked_start,
                        end_time=blocked_end,
                        duration_minutes=request.duration_minutes,
                        status="blocked",
                        quoted_price=None,
                        discount_applied=None,
                    )
                    logging.info(
                        "[BOOKING] Blocked second slot %s for heavy job (tech %d)",
                        blocked_start.strftime("%I:%M %p"), request.technician_id,
                    )

        delete_route_cache(request.technician_id, request.start_time.date())

        # Push event to technician's connected Google or Outlook calendar (non-fatal)
        try:
            creds = get_calendar_credentials(request.technician_id)
            if creds and creds.get("calendar_connected"):
                provider = creds.get("calendar_provider")
                creds_dict = creds.get("calendar_credentials", {})
                service_label = request.service_type.replace("_", " ").title()
                event_summary = f"{service_label} - {request.customer_name}"
                event_description = (
                    f"Customer: {request.customer_name}\n"
                    f"Phone: {request.customer_phone}\n"
                    f"Email: {request.customer_email or 'N/A'}\n"
                    f"Service: {service_label}\n"
                    f"Price: ${request.quoted_price}\n"
                    f"Discount: {request.discount_applied or 'none'}\n"
                    f"Appointment ID: {appointment_id}"
                )
                attendees = [request.customer_email] if request.customer_email else []
                if provider == "google":
                    from src.services.google_calendar import GoogleCalendarService
                    from src.utils.db import save_calendar_credentials
                    cal = GoogleCalendarService(creds_dict)
                    cal.create_event(
                        summary=event_summary,
                        start_datetime=request.start_time,
                        end_datetime=end_time,
                        description=event_description,
                        location=request.address,
                        attendees=attendees,
                        color_id=tech.get("calendar_color_id"),
                    )
                    save_calendar_credentials(
                        request.technician_id, "google",
                        creds.get("calendar_email", ""),
                        cal.get_updated_credentials(),
                    )
                    logging.info("[BOOKING] Google Calendar event created for tech %d", request.technician_id)
                elif provider == "outlook":
                    from src.services.outlook_calendar import OutlookCalendarService
                    from src.utils.db import save_calendar_credentials
                    cal = OutlookCalendarService(creds_dict)
                    cal.create_event(
                        summary=event_summary,
                        start_datetime=request.start_time,
                        end_datetime=end_time,
                        description=event_description,
                        location=request.address,
                        attendees=attendees,
                    )
                    save_calendar_credentials(
                        request.technician_id, "outlook",
                        creds.get("calendar_email", ""),
                        cal.get_updated_credentials(),
                    )
                    logging.info("[BOOKING] Outlook Calendar event created for tech %d", request.technician_id)
        except Exception as cal_err:
            logging.warning("[BOOKING] Calendar push failed (non-fatal): %s", cal_err)
        try:
            from src.utils.db import get_admin_calendar_credentials, save_admin_calendar_credentials
            admin_creds = get_admin_calendar_credentials()
            if admin_creds and admin_creds.get("connected"):
                admin_provider = admin_creds.get("provider")
                admin_creds_dict = admin_creds.get("credentials", {})
                service_label = request.service_type.replace("_", " ").title()
                admin_event_summary = f"{service_label} - {request.customer_name}"
                admin_event_description = (
                    f"Technician: {tech['name']}\n"
                    f"Customer: {request.customer_name}\n"
                    f"Phone: {request.customer_phone}\n"
                    f"Email: {request.customer_email or 'N/A'}\n"
                    f"Service: {service_label}\n"
                    f"Price: ${request.quoted_price}\n"
                    f"Discount: {request.discount_applied or 'none'}\n"
                    f"Appointment ID: {appointment_id}"
                )
                attendees = []
                if admin_provider == "google":
                    from src.services.google_calendar import GoogleCalendarService
                    admin_cal = GoogleCalendarService(admin_creds_dict)

                    target_calendar_id = _find_tech_sub_calendar(admin_cal.service, tech["name"])

                    res = None
                    try:
                        res = admin_cal.create_event(
                            summary=admin_event_summary,
                            start_datetime=request.start_time,
                            end_datetime=end_time,
                            description=admin_event_description,
                            location=request.address,
                            attendees=attendees,
                            color_id=tech.get("calendar_color_id"),
                            calendar_id=target_calendar_id,
                        )
                    except Exception as e:
                        logging.warning(
                            "[BOOKING] Failed to push to sub-calendar %s: %s",
                            target_calendar_id, e,
                        )

                    if not res and target_calendar_id != "primary":
                        logging.warning("[BOOKING] Falling back to primary calendar...")
                        try:
                            res = admin_cal.create_event(
                                summary=admin_event_summary,
                                start_datetime=request.start_time,
                                end_datetime=end_time,
                                description=admin_event_description,
                                location=request.address,
                                attendees=attendees,
                                color_id=tech.get("calendar_color_id"),
                                calendar_id="primary",
                            )
                            if res:
                                target_calendar_id = "primary"
                        except Exception as prim_err:
                            logging.error("[BOOKING] Fallback to primary calendar also failed: %s", prim_err)

                    save_admin_calendar_credentials(
                        "google",
                        admin_creds.get("email", ""),
                        admin_cal.get_updated_credentials(),
                    )
                    logging.info(
                        "[BOOKING] Admin calendar event created on calendar=%s",
                        target_calendar_id,
                    )
                    if res and "id" in res:
                        update_appointment_calendar_event_id(appointment_id, res["id"])
                        logging.info(
                            "[BOOKING] Updated database appointment Event ID from %s to Google Event ID %s",
                            appointment_id, res["id"],
                        )
                elif admin_provider == "outlook":
                    from src.services.outlook_calendar import OutlookCalendarService
                    admin_cal = OutlookCalendarService(admin_creds_dict)
                    res = admin_cal.create_event(
                        summary=admin_event_summary,
                        start_datetime=request.start_time,
                        end_datetime=end_time,
                        description=admin_event_description,
                        location=request.address,
                        attendees=attendees,
                    )
                    save_admin_calendar_credentials(
                        "outlook",
                        admin_creds.get("email", ""),
                        admin_cal.get_updated_credentials(),
                    )
                    logging.info("[BOOKING] Admin Outlook Calendar event created")
                    if res and "id" in res:
                        update_appointment_calendar_event_id(appointment_id, res["id"])
                        logging.info(
                            "[BOOKING] Updated database appointment Event ID from %s to Outlook Event ID %s",
                            appointment_id, res["id"],
                        )
        except Exception as admin_cal_err:
            logging.warning("[BOOKING] Admin calendar push failed (non-fatal): %s", admin_cal_err)

        logging.info(f"[BOOKING] SUCCESS: {request.customer_name} booked with {tech['name']} for {request.service_type} at {request.start_time}")


        return BookAppointmentResponse(
            success=True,
            appointment_id=appointment_id,
            technician=tech["name"],
            time=request.start_time.isoformat(),
            message=f"Appointment booked with {tech['name']}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"[BOOKING] ERROR: {e}")
        return BookAppointmentResponse(
            success=False,
            message=f"Failed to book appointment: {str(e)}"
        )


class CancelByPhoneRequest(BaseModel):
    phone_number: str
    cancellation_reason: str = None


@router.post("/cancel-appointment")
def cancel_appointment_by_phone(request: CancelByPhoneRequest, _auth=Depends(verify_retell_api_key)):
    from src.utils.db import get_db_connection
    from psycopg2.extras import RealDictCursor

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, customer_name, service_type, start_time, status, calendar_event_id, technician_id
            FROM appointments
            WHERE customer_phone = %s AND status = 'scheduled'
            AND start_time > CURRENT_TIMESTAMP
            ORDER BY start_time ASC LIMIT 1
        """, (request.phone_number,))
        appt = cur.fetchone()

        if not appt:
            cur.execute("""
                SELECT id, customer_name, service_type, start_time, status, calendar_event_id, technician_id
                FROM appointments_cache
                WHERE customer_phone = %s AND status IN ('scheduled', 'confirmed')
                AND start_time > CURRENT_TIMESTAMP
                ORDER BY start_time ASC LIMIT 1
            """, (request.phone_number,))
            appt = cur.fetchone()

        if not appt:
            return {"success": False, "message": "No upcoming appointment found for this phone number"}

        table = "appointments"
        cur.execute(f"""
            UPDATE {table} SET status = 'cancelled' WHERE id = %s
        """, (appt["id"],))
        conn.commit()

        try:
            delete_appointment_calendar_event(dict(appt))
        except Exception as cal_err:
            logging.error(f"[CALENDAR] Non-fatal error deleting calendar event: {cal_err}")

        return {
            "success": True,
            "message": f"Appointment for {appt['customer_name']} on {appt['start_time']} has been cancelled",
            "cancelled_appointment": {
                "customer_name": appt["customer_name"],
                "service_type": appt["service_type"],
                "start_time": str(appt["start_time"])
            }
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Failed to cancel: {str(e)}"}
    finally:
        cur.close()
        conn.close()



class BookRedoRequest(BaseModel):
    order_id: str
    issue_description: str


@router.post("/book-redo-appointment")
def book_redo_appointment(request: BookRedoRequest, _auth=Depends(verify_retell_api_key)):
    from src.utils.db import get_db_connection
    from psycopg2.extras import RealDictCursor

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT a.id, a.customer_name, a.customer_phone, a.customer_email,
                   a.service_type, a.address, a.latitude, a.longitude,
                   a.technician_id, t.name as technician_name
            FROM appointments a
            LEFT JOIN technicians t ON t.id = a.technician_id
            WHERE a.customer_phone = %s OR CAST(a.id AS TEXT) = %s
            ORDER BY a.created_at DESC LIMIT 1
        """, (request.order_id, request.order_id))
        original = cur.fetchone()

        if not original:
            cur.execute("""
                SELECT id, customer_name, customer_phone, service_type,
                       address, latitude, longitude, technician_id
                FROM appointments_cache
                WHERE customer_phone = %s OR CAST(id AS TEXT) = %s
                ORDER BY created_at DESC LIMIT 1
            """, (request.order_id, request.order_id))
            original = cur.fetchone()

        if not original:
            return {"success": False, "message": "No previous appointment found with that ID or phone number"}

        redo_time = datetime.now() + timedelta(days=2)
        redo_time = redo_time.replace(hour=10, minute=0, second=0, microsecond=0)
        end_time = redo_time + timedelta(hours=1)

        cur.execute("""
            INSERT INTO appointments
            (calendar_event_id, technician_id, customer_name, customer_phone,
             customer_email, service_type, address, latitude, longitude,
             start_time, end_time, duration_minutes, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            str(uuid.uuid4()),
            original["technician_id"],
            original["customer_name"],
            original["customer_phone"],
            original.get("customer_email"),
            original["service_type"],
            original["address"],
            original.get("latitude"),
            original.get("longitude"),
            redo_time,
            end_time,
            60,
            "scheduled",
            f"REDO - {request.issue_description}"
        ))
        redo_appt = cur.fetchone()
        conn.commit()

        return {
            "success": True,
            "message": f"Redo appointment booked for {original['customer_name']} at {original['address']} on {redo_time.strftime('%B %d at %I:%M %p')}",
            "redo_appointment": {
                "id": redo_appt["id"],
                "customer_name": original["customer_name"],
                "address": original["address"],
                "service_type": original["service_type"],
                "date": redo_time.strftime("%B %d at %I:%M %p"),
                "technician": original.get("technician_name", "Same technician")
            }
        }
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": f"Failed to book redo: {str(e)}"}
    finally:
        cur.close()
        conn.close()
