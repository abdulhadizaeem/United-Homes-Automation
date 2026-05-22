"""United Home Services API -- main application entry point."""
import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import (
    admin as admin_router,
    appointment_management,
    appointments,
    auth as auth_router,
    calendar as calendar_router,
    call_logs,
    job_scope_rules,
    retell_webhooks,
    technicians,
    webhooks,
)
from src.utils.db import create_tables


def send_daily_schedules():
    from src.utils.db import get_all_technicians, get_appointments_paginated
    from src.utils.mail_service import send_technician_daily_schedule

    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    tomorrow = (now + timedelta(days=1)).date()
    tomorrow_start = datetime.combine(tomorrow, datetime.min.time())
    tomorrow_end = datetime.combine(tomorrow, datetime.max.time())
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    schedule_url = f"{frontend_url}/signin"

    techs = get_all_technicians()
    logging.info(
        "Sending daily schedules to %d technicians for %s",
        len(techs), tomorrow,
    )

    for tech in techs:
        if not tech.get("email"):
            continue
        try:
            result = get_appointments_paginated(
                page=1,
                page_size=50,
                technician_id=tech["id"],
                date_from=str(tomorrow_start),
                date_to=str(tomorrow_end),
                time_filter="upcoming",
            )
            appt_list = []
            for appt in result["appointments"]:
                start_str = str(appt["start_time"])
                end_str = str(appt["end_time"])
                appt_list.append({
                    "start_time": start_str.split(" ")[1][:5] if " " in start_str else start_str,
                    "end_time": end_str.split(" ")[1][:5] if " " in end_str else end_str,
                    "service_type": appt["service_type"],
                    "customer_name": appt["customer_name"],
                    "customer_phone": appt.get("customer_phone", ""),
                    "address": appt.get("address", ""),
                })

            send_technician_daily_schedule(
                tech_email=tech["email"],
                tech_name=tech["name"],
                schedule_date=str(tomorrow),
                appointments=appt_list,
                schedule_url=schedule_url,
            )
            logging.info(
                "Schedule sent to %s (%s): %d appointments",
                tech["name"], tech["email"], len(appt_list),
            )
        except Exception as exc:
            logging.error("Failed to send schedule to %s: %s", tech["name"], exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_daily_schedules,
        CronTrigger(hour=18, minute=0, timezone=ZoneInfo("America/New_York")),
        id="daily_schedule_email",
        name="Send technician daily schedules at 6 PM ET",
        replace_existing=True,
    )
    scheduler.start()
    logging.info("Scheduler started: daily schedule emails at 6 PM ET")

    yield

    scheduler.shutdown()


app = FastAPI(title="United Home Services API", lifespan=lifespan)

_frontend = os.getenv("FRONTEND_URL", "https://aidash.unitedhomecarolina.com")
_allowed_origins = [
    "https://aidash.unitedhomecarolina.com",
    _frontend,
    "http://localhost:3000",
    "http://localhost:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
app.include_router(admin_router.router, prefix="/api/admin", tags=["admin"])
app.include_router(calendar_router.router, prefix="/api/calendar", tags=["calendar"])
app.include_router(
    appointment_management.router,
    prefix="/api/appointment-management",
    tags=["appointment-management"],
)
app.include_router(appointments.router, prefix="/api/appointments", tags=["appointments"])
app.include_router(technicians.router, prefix="/api/technicians", tags=["technicians"])
app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
app.include_router(retell_webhooks.router, prefix="/api/webhooks", tags=["retell-webhooks"])
app.include_router(call_logs.router, prefix="/api/call-logs", tags=["call-logs"])
app.include_router(
    job_scope_rules.router,
    prefix="/api/admin/job-scope-rules",
    tags=["admin"],
)


@app.get("/")
def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}
