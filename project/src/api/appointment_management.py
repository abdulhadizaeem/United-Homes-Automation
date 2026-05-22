import logging
import traceback
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from src.utils.auth import get_current_user, require_admin
from src.utils.db import (
    get_appointments_paginated,
    get_appointment_by_id,
    update_appointment_status as db_update_status,
    get_appointment_stats,
    create_appointment as db_create_appointment,
    get_technician_by_user_id
)
from src.api.models import UpdateAppointmentStatus, CreateAppointmentRequest

router = APIRouter()




@router.get("/admin/list")
async def admin_list_appointments(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    technician_id: int = Query(None),
    status: str = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    search: str = Query(None),
    time_filter: str = Query(None),
    current_user: dict = Depends(require_admin)
):
    try:
        result = get_appointments_paginated(
            page=page,
            page_size=page_size,
            technician_id=technician_id,
            status_filter=status,
            date_from=date_from,
            date_to=date_to,
            search=search,
            time_filter=time_filter
        )
        appointments_out = {}
        for a in result["appointments"]:
            appointments_out[str(a["id"])] = {
                "id": a["id"],
                "calendar_event_id": a.get("calendar_event_id"),
                "status": a["status"],
                "service_type": a["service_type"],
                "customer": {
                    "name": a["customer_name"],
                    "phone": a.get("customer_phone"),
                    "email": a.get("customer_email")
                },
                "technician": {
                    "id": a["technician_id"],
                    "name": a.get("technician_name")
                },
                "schedule": {
                    "start_time": str(a["start_time"]),
                    "end_time": str(a["end_time"]),
                    "duration_minutes": a.get("duration_minutes")
                },
                "location": {
                    "address": a.get("address"),
                    "latitude": float(a["latitude"]) if a.get("latitude") else None,
                    "longitude": float(a["longitude"]) if a.get("longitude") else None
                },
                "pricing": {
                    "quoted_price": float(a["quoted_price"]) if a.get("quoted_price") else None,
                    "discount_applied": a.get("discount_applied")
                },
                "notes": a.get("notes"),
                "created_at": str(a.get("created_at", ""))
            }
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": appointments_out,
                "pagination": {
                    "total": result["total"],
                    "page": result["page"],
                    "page_size": result["page_size"],
                    "total_pages": result["total_pages"]
                }
            }
        )
    except Exception as e:
        logging.error(f"List appointments error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch appointments")


@router.get("/admin/stats")
async def admin_appointment_stats(current_user: dict = Depends(require_admin)):
    try:
        stats = get_appointment_stats()
        return JSONResponse(
            status_code=200,
            content={"success": True, "data": stats}
        )
    except Exception as e:
        logging.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.put("/admin/{appointment_id}/status")
async def admin_update_status(
    appointment_id: int,
    request: UpdateAppointmentStatus,
    current_user: dict = Depends(require_admin)
):
    try:
        success = db_update_status(appointment_id, request.status)
        if not success:
            raise HTTPException(status_code=404, detail="Appointment not found")

        if request.status == "cancelled":
            appt = get_appointment_by_id(appointment_id)
            if appt:
                # Delete from Google/Outlook Calendar
                try:
                    from src.api.appointments import delete_appointment_calendar_event
                    delete_appointment_calendar_event(appt)
                except Exception as cal_err:
                    logging.error(f"Calendar event deletion failed: {cal_err}")

                try:
                    if appt.get("customer_email"):
                        from src.utils.mail_service import send_cancellation_email
                        send_cancellation_email(
                            customer_email=appt["customer_email"],
                            customer_name=appt["customer_name"],
                            service_type=appt["service_type"],
                            start_time=str(appt["start_time"])
                        )
                except Exception as mail_err:
                    logging.error(f"Cancellation email failed: {mail_err}")

        return JSONResponse(
            status_code=200,
            content={"success": True, "message": f"Appointment status updated to {request.status}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Update status error: {e}")
        raise HTTPException(status_code=500, detail="Failed to update status")



# ============================================================
# TECHNICIAN ENDPOINTS (next-day schedule only, released at 6 PM)
# ============================================================

@router.get("/my/schedule")
async def my_next_day_schedule(
    current_user: dict = Depends(get_current_user)
):
    """
    Technicians see today's full schedule + tomorrow's schedule at all times.
    Sorted chronologically by start_time.
    """
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            raise HTTPException(status_code=400, detail="No technician profile found")

        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
        now = datetime.now(eastern)

        today = now.date()
        tomorrow = (now + timedelta(days=1)).date()

        # Fetch today + tomorrow window
        window_start = datetime.combine(today, datetime.min.time())
        window_end = datetime.combine(tomorrow, datetime.max.time())

        result = get_appointments_paginated(
            page=1,
            page_size=100,
            technician_id=tech["id"],
            date_from=str(window_start),
            date_to=str(window_end),
        )

        appointments_out = {}
        for a in result["appointments"]:
            appointments_out[str(a["id"])] = {
                "id": a["id"],
                "service_type": a["service_type"],
                "customer": {
                    "name": a["customer_name"],
                    "phone": a.get("customer_phone"),
                    "email": a.get("customer_email")
                },
                "schedule": {
                    "start_time": str(a["start_time"]),
                    "end_time": str(a["end_time"]),
                    "duration_minutes": a.get("duration_minutes")
                },
                "location": {
                    "address": a.get("address"),
                    "latitude": float(a["latitude"]) if a.get("latitude") else None,
                    "longitude": float(a["longitude"]) if a.get("longitude") else None
                },
                "status": a["status"],
                "pricing": {
                    "quoted_price": float(a["quoted_price"]) if a.get("quoted_price") else None,
                    "discount_applied": a.get("discount_applied")
                },
                "notes": a.get("notes")
            }

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "schedule_available": True,
                "schedule_date": str(today),
                "total_appointments": len(appointments_out),
                "data": appointments_out
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"My schedule error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch schedule")




# ============================================================
# SHARED ENDPOINTS (both admin and user can access)
# ============================================================

@router.get("/{appointment_id}")
async def get_appointment_detail(
    appointment_id: int,
    current_user: dict = Depends(get_current_user)
):
    try:
        appt = get_appointment_by_id(appointment_id)
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")

        if not current_user.get("is_admin"):
            tech = get_technician_by_user_id(current_user["id"])
            if not tech or appt["technician_id"] != tech["id"]:
                raise HTTPException(status_code=403, detail="Access denied")

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": {
                    "id": appt["id"],
                    "calendar_event_id": appt.get("calendar_event_id"),
                    "technician_id": appt["technician_id"],
                    "technician_name": appt.get("technician_name"),
                    "customer_name": appt["customer_name"],
                    "customer_phone": appt.get("customer_phone"),
                    "customer_email": appt.get("customer_email"),
                    "service_type": appt["service_type"],
                    "address": appt.get("address"),
                    "latitude": float(appt["latitude"]) if appt.get("latitude") else None,
                    "longitude": float(appt["longitude"]) if appt.get("longitude") else None,
                    "start_time": str(appt["start_time"]),
                    "end_time": str(appt["end_time"]),
                    "duration_minutes": appt.get("duration_minutes"),
                    "status": appt["status"],
                    "pricing": {
                        "quoted_price": float(appt["quoted_price"]) if appt.get("quoted_price") else None,
                        "discount_applied": appt.get("discount_applied")
                    },
                    "notes": appt.get("notes"),
                    "created_at": str(appt.get("created_at", ""))
                }
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Get appointment error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch appointment")
