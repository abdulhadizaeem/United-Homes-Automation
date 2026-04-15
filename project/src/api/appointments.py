import logging
import uuid
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta

from src.utils.radar import geocode_address
from src.utils.db import (
    get_techs_with_appointments_for_day,
    get_technician,
    get_calendar_credentials,
    insert_appointment,
    delete_route_cache,
)
from src.utils.distance import calculate_distance, estimate_tech_location
from src.utils.api_key_auth import verify_retell_api_key

router = APIRouter()


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
    message: str = None


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


# -- Service durations in minutes --
SERVICE_DURATIONS = {
    "chimney": 60,
    "dryer_vent": 60,
    "gutter": 60,
    "power_washing": 90,
    "air_duct": 120,
}

# Fixed booking slots (hour, minute) in Eastern Time
FIXED_SLOTS = [(9, 0), (12, 0), (15, 0), (17, 0)]
# Overflow -- offered as 5th slot only when all 4 main slots are taken
OVERFLOW_SLOTS = [(11, 0), (13, 0)]


@router.post("/find-technician-availability", response_model=FindTechnicianResponse)
def find_technician_availability(request: FindTechnicianRequest, _auth=Depends(verify_retell_api_key)):
    """Find the best available technician for a given date.

    Algorithm:
    1. Get all techs with the matching skill
    2. For each tech, find the earliest available slot on the requested date
    3. Calculate distance from the tech's last job site (or home if first job)
    4. Return the closest available tech with the assigned time slot
    """
    try:
        # Parse the requested date
        try:
            req_date = date_type.fromisoformat(request.requested_date)
        except ValueError:
            return FindTechnicianResponse(
                success=False,
                available=False,
                message="Invalid date format. Please use YYYY-MM-DD.",
            )

        eastern = ZoneInfo("America/New_York")
        service_duration = SERVICE_DURATIONS.get(request.service_type, 60)

        logging.info(
            "[AVAILABILITY] Request: service=%s, date=%s, lat=%s, lng=%s",
            request.service_type, req_date, request.confirmed_latitude,
            request.confirmed_longitude,
        )

        # Single query: techs with the right skill + their appointments for the day
        techs = get_techs_with_appointments_for_day(request.service_type, req_date)
        logging.info(
            "[AVAILABILITY] Found %d techs for '%s': %s",
            len(techs), request.service_type,
            [(t["id"], t["name"]) for t in techs],
        )

        if not techs:
            return FindTechnicianResponse(
                success=False,
                available=False,
                message="No technicians available for this service type.",
            )

        candidates = []

        # Load job scope rules once and determine if this is a heavy job
        from src.utils.db import get_job_scope_rules
        _scope_rules = get_job_scope_rules()
        is_heavy = bool(
            request.job_units
            and request.service_type in _scope_rules
            and request.job_units >= _scope_rules[request.service_type]["units_threshold"]
        )
        if is_heavy:
            logging.info(
                "[AVAILABILITY] Heavy job: %s with %d units (threshold=%d)",
                request.service_type, request.job_units,
                _scope_rules[request.service_type]["units_threshold"],
            )

        for tech in techs:
            # Skip techs without home coordinates
            if not tech.get("home_latitude") or not tech.get("home_longitude"):
                logging.warning(
                    "Tech %s (id=%d) has no home coordinates, skipping",
                    tech["name"], tech["id"],
                )
                continue

            # Appointments already loaded from the combined query
            appointments = sorted(tech["appointments"], key=lambda a: a["start_time"])

            # Step 3: Find best available fixed time slot
            slot_duration = timedelta(minutes=service_duration)
            found_slot = None
            depart_from_lat = float(tech["home_latitude"])
            depart_from_lon = float(tech["home_longitude"])

            # Build fixed slot datetimes for this day
            main_slots = [
                datetime(req_date.year, req_date.month, req_date.day, h, m, tzinfo=eastern)
                for h, m in FIXED_SLOTS
            ]
            overflow_slots = [
                datetime(req_date.year, req_date.month, req_date.day, h, m, tzinfo=eastern)
                for h, m in OVERFLOW_SLOTS
            ]

            def _slot_conflicts(candidate, appts):
                """Return True if candidate overlaps any existing appointment."""
                candidate_end = candidate + slot_duration
                for a in appts:
                    a_start = a["start_time"]
                    a_end = a["end_time"]
                    if hasattr(a_start, 'tzinfo') and a_start.tzinfo is None:
                        a_start = a_start.replace(tzinfo=eastern)
                    if hasattr(a_end, 'tzinfo') and a_end.tzinfo is None:
                        a_end = a_end.replace(tzinfo=eastern)
                    if max(candidate, a_start) < min(candidate_end, a_end):
                        return True
                return False

            def _departure_point(slot, appts):
                """Return (lat, lon) of last completed job before slot, or home."""
                prev = [
                    a for a in appts
                    if (a["end_time"].replace(tzinfo=eastern)
                        if hasattr(a["end_time"], 'tzinfo') and a["end_time"].tzinfo is None
                        else a["end_time"]) <= slot
                ]
                if prev:
                    last = max(prev, key=lambda a: a["end_time"])
                    return float(last["latitude"]), float(last["longitude"])
                return float(tech["home_latitude"]), float(tech["home_longitude"])

            # Sort by customer preference: nearest fixed slot to preferred time
            if request.preferred_time:
                try:
                    ph, pm = map(int, request.preferred_time.split(":"))
                    pref_dt = datetime(req_date.year, req_date.month, req_date.day,
                                       ph, pm, tzinfo=eastern)
                    slot_order = sorted(main_slots,
                                        key=lambda s: abs((s - pref_dt).total_seconds()))
                except Exception:
                    slot_order = main_slots
            else:
                slot_order = main_slots

            blocked_slot = None

            if is_heavy:
                # Heavy job: find first pair of consecutive free main slots
                for i in range(len(main_slots) - 1):
                    s1, s2 = main_slots[i], main_slots[i + 1]
                    if not _slot_conflicts(s1, appointments) and not _slot_conflicts(s2, appointments):
                        found_slot = s1
                        blocked_slot = s2
                        depart_from_lat, depart_from_lon = _departure_point(s1, appointments)
                        logging.info(
                            "[AVAILABILITY] Tech %s: heavy job %s + %s blocked",
                            tech["name"], s1.strftime("%I:%M %p"), s2.strftime("%I:%M %p"),
                        )
                        break
            else:
                # Standard: try main slots in preference order
                for slot in slot_order:
                    if not _slot_conflicts(slot, appointments):
                        found_slot = slot
                        depart_from_lat, depart_from_lon = _departure_point(slot, appointments)
                        logging.info(
                            "[AVAILABILITY] Tech %s: main slot %s free",
                            tech["name"], found_slot.strftime("%I:%M %p"),
                        )
                        break

                # Overflow: all 4 main slots taken -> try 11am then 1pm
                if not found_slot:
                    taken = sum(1 for s in main_slots if _slot_conflicts(s, appointments))
                    if taken >= len(FIXED_SLOTS):
                        for slot in overflow_slots:
                            if not _slot_conflicts(slot, appointments):
                                found_slot = slot
                                depart_from_lat, depart_from_lon = _departure_point(slot, appointments)
                                logging.info(
                                    "[AVAILABILITY] Tech %s: overflow slot %s",
                                    tech["name"], found_slot.strftime("%I:%M %p"),
                                )
                                break

            if not found_slot:
                logging.info(
                    "[AVAILABILITY] Tech %s (id=%d) is FULL on %s",
                    tech["name"], tech["id"], req_date,
                )
                continue

            # Step 4: Check admin calendar for conflicts at this slot (non-fatal)
            from src.utils.calendar_conflict import check_admin_calendar_conflict
            slot_end = found_slot + timedelta(minutes=service_duration)
            if check_admin_calendar_conflict(found_slot, slot_end):
                logging.info(
                    "[AVAILABILITY] Tech %s: slot %s blocked by admin calendar, skipping",
                    tech["name"], found_slot.strftime("%I:%M %p"),
                )
                continue

            # Step 5: Calculate distance from departure point to customer
            distance = calculate_distance(
                depart_from_lat, depart_from_lon,
                request.confirmed_latitude, request.confirmed_longitude,
            )

            max_radius = tech.get("max_radius_miles") or 50

            if distance > max_radius:
                logging.warning(
                    "[AVAILABILITY] Tech %s (id=%d): %.1f mi from job site, max=%dmi -- TOO FAR",
                    tech["name"], tech["id"], distance, max_radius,
                )
                continue

            logging.info(
                "[AVAILABILITY] Tech %s (id=%d): slot=%s, %.1f mi from departure point",
                tech["name"], tech["id"], found_slot.strftime("%I:%M %p"), distance,
            )

            candidates.append({
                "tech": tech,
                "slot": found_slot,
                "distance": distance,
                "blocked_slot": blocked_slot,
            })

        if not candidates:
            # Log all distances for debugging
            logging.warning("[AVAILABILITY] No techs available for %s on %s", request.service_type, req_date)
            return FindTechnicianResponse(
                success=False,
                available=False,
                message="No technicians available on this date. All technicians are either fully booked or outside service range.",
            )

        # Step 5: Sort by earliest slot, then shortest distance
        candidates.sort(key=lambda c: (c["slot"], c["distance"]))
        best = candidates[0]

        logging.info(
            "[AVAILABILITY] BEST: %s at %s (%.1f mi)",
            best["tech"]["name"],
            best["slot"].strftime("%I:%M %p"),
            best["distance"],
        )

        return FindTechnicianResponse(
            success=True,
            technician=TechnicianInfo(
                id=best["tech"]["id"],
                name=best["tech"]["name"],
                distance_miles=round(best["distance"], 2),
            ),
            available=True,
            time_slot=best["slot"].isoformat(),
            message=f"{best['tech']['name']} available at {best['slot'].strftime('%I:%M %p')}",
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
            from src.utils.db import get_job_scope_rules
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

        # Push to admin calendar (non-fatal) — shows ALL tech appointments on one calendar
        try:
            from src.utils.db import get_admin_calendar_credentials, save_admin_calendar_credentials
            admin_creds = get_admin_calendar_credentials()
            if admin_creds and admin_creds.get("connected"):
                admin_provider = admin_creds.get("provider")
                admin_creds_dict = admin_creds.get("credentials", {})
                service_label = request.service_type.replace("_", " ").title()
                admin_event_summary = f"[{tech['name']}] {service_label} - {request.customer_name}"
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
                    admin_cal.create_event(
                        summary=admin_event_summary,
                        start_datetime=request.start_time,
                        end_datetime=end_time,
                        description=admin_event_description,
                        location=request.address,
                        attendees=attendees,
                        color_id=tech.get("calendar_color_id"),
                    )
                    save_admin_calendar_credentials(
                        "google",
                        admin_creds.get("email", ""),
                        admin_cal.get_updated_credentials(),
                    )
                    logging.info("[BOOKING] Admin Google Calendar event created")
                elif admin_provider == "outlook":
                    from src.services.outlook_calendar import OutlookCalendarService
                    admin_cal = OutlookCalendarService(admin_creds_dict)
                    admin_cal.create_event(
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
            SELECT id, customer_name, service_type, start_time, status
            FROM appointments
            WHERE customer_phone = %s AND status = 'scheduled'
            AND start_time > CURRENT_TIMESTAMP
            ORDER BY start_time ASC LIMIT 1
        """, (request.phone_number,))
        appt = cur.fetchone()

        if not appt:
            cur.execute("""
                SELECT id, customer_name, service_type, start_time, status
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
