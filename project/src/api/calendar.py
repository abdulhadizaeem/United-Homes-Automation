import os
import logging
import traceback
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google_auth_oauthlib.flow import Flow
from src.utils.auth import get_current_user
from src.utils.db import (
    get_technician_by_user_id,
    save_calendar_credentials,
    get_calendar_credentials,
    disconnect_calendar as db_disconnect
)
from src.services.google_calendar import GoogleCalendarService
from src.services.outlook_calendar import OutlookCalendarService
from src.utils.jwt_utils import create_oauth_state_token, verify_oauth_state_token
from dotenv import load_dotenv
import msal

load_dotenv()

router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
MICROSOFT_REDIRECT_URI = os.getenv("MICROSOFT_REDIRECT_URI")
MICROSOFT_SCOPES = ["Calendars.ReadWrite", "User.Read"]



@router.get("/google/connect")
async def google_connect(current_user: dict = Depends(get_current_user)):
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            raise HTTPException(status_code=404, detail="No technician profile found")
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"
                }
            },
            scopes=GOOGLE_SCOPES,
            redirect_uri=GOOGLE_REDIRECT_URI
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=create_oauth_state_token({
                "user_id": current_user["id"],
                "tech_id": tech["id"],
                "provider": "google"
            })
        )
        return JSONResponse(
            status_code=200,
            content={"success": True, "auth_url": auth_url}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Google connect error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to start Google OAuth")


@router.get("/google/callback")
async def google_callback(code: str = Query(...), state: str = Query(...)):
    try:
        state_data = verify_oauth_state_token(state)
        if not state_data:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"
                }
            },
            scopes=GOOGLE_SCOPES,
            redirect_uri=GOOGLE_REDIRECT_URI
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials

        calendar_email = ""
        try:
            from googleapiclient.discovery import build
            oauth2_service = build("oauth2", "v2", credentials=credentials)
            user_info = oauth2_service.userinfo().get().execute()
            calendar_email = user_info.get("email", "")
        except Exception:
            pass

        creds_dict = {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            "scopes": list(credentials.scopes) if credentials.scopes else GOOGLE_SCOPES
        }
        save_calendar_credentials(state_data["tech_id"], "google", calendar_email, creds_dict)

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/settings?calendar=connected")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Google callback error: {e}")
        traceback.print_exc()
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/settings?calendar=error")


@router.get("/outlook/connect")
async def outlook_connect(current_user: dict = Depends(get_current_user)):
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            raise HTTPException(status_code=404, detail="No technician profile found")
        app = msal.ConfidentialClientApplication(
            MICROSOFT_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}",
            client_credential=MICROSOFT_CLIENT_SECRET
        )
        state_token = create_oauth_state_token({
            "user_id": current_user["id"],
            "tech_id": tech["id"],
            "provider": "outlook"
        })
        auth_url = app.get_authorization_request_url(
            MICROSOFT_SCOPES,
            redirect_uri=MICROSOFT_REDIRECT_URI,
            state=state_token
        )
        return JSONResponse(
            status_code=200,
            content={"success": True, "auth_url": auth_url}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Outlook connect error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to start Outlook OAuth")


@router.get("/outlook/callback")
async def outlook_callback(code: str = Query(...), state: str = Query(...)):
    try:
        state_data = verify_oauth_state_token(state)
        if not state_data:
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

        app = msal.ConfidentialClientApplication(
            MICROSOFT_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}",
            client_credential=MICROSOFT_CLIENT_SECRET
        )
        result = app.acquire_token_by_authorization_code(
            code,
            scopes=MICROSOFT_SCOPES,
            redirect_uri=MICROSOFT_REDIRECT_URI
        )
        if "access_token" not in result:
            raise HTTPException(status_code=400, detail="Failed to get token")

        calendar_email = ""
        try:
            import requests as req
            headers = {"Authorization": f"Bearer {result['access_token']}"}
            me = req.get("https://graph.microsoft.com/v1.0/me", headers=headers).json()
            calendar_email = me.get("mail", me.get("userPrincipalName", ""))
        except Exception:
            pass

        from datetime import timedelta, datetime, timezone
        creds_dict = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "token_expiry": (
                datetime.now(timezone.utc) + timedelta(seconds=result.get("expires_in", 3600))
            ).isoformat(),
            "scopes": MICROSOFT_SCOPES
        }
        save_calendar_credentials(state_data["tech_id"], "outlook", calendar_email, creds_dict)

        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/settings?calendar=connected")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Outlook callback error: {e}")
        traceback.print_exc()
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(url=f"{frontend_url}/settings?calendar=error")


@router.post("/disconnect")
async def disconnect_calendar(current_user: dict = Depends(get_current_user)):
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            raise HTTPException(status_code=404, detail="No technician profile found")
        db_disconnect(tech["id"])
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "Calendar disconnected"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Disconnect error: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect calendar")


@router.get("/status")
async def calendar_status(current_user: dict = Depends(get_current_user)):
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "data": {"connected": False, "provider": None, "email": None}
                }
            )
        creds = get_calendar_credentials(tech["id"])
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": {
                    "connected": creds.get("calendar_connected", False) if creds else False,
                    "provider": creds.get("calendar_provider") if creds else None,
                    "email": creds.get("calendar_email") if creds else None
                }
            }
        )
    except Exception as e:
        logging.error(f"Calendar status error: {e}")
        raise HTTPException(status_code=500, detail="Failed to get calendar status")


@router.get("/events")
async def list_calendar_events(
    days: int = Query(7, ge=1, le=90),
    current_user: dict = Depends(get_current_user)
):
    try:
        tech = get_technician_by_user_id(current_user["id"])
        if not tech:
            raise HTTPException(status_code=404, detail="No technician profile found")
        creds = get_calendar_credentials(tech["id"])
        if not creds or not creds.get("calendar_connected"):
            raise HTTPException(status_code=400, detail="No calendar connected")

        from datetime import datetime, timedelta
        now = datetime.utcnow()
        end = now + timedelta(days=days)

        provider = creds["calendar_provider"]
        credentials_dict = creds["calendar_credentials"]

        if provider == "google":
            service = GoogleCalendarService(credentials_dict)
        elif provider == "outlook":
            service = OutlookCalendarService(credentials_dict)
        else:
            raise HTTPException(status_code=400, detail="Unknown calendar provider")

        events = service.list_events(now, end)

        updated_creds = service.get_updated_credentials()
        save_calendar_credentials(tech["id"], provider, creds["calendar_email"], updated_creds)

        return JSONResponse(
            status_code=200,
            content={"success": True, "data": events}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"List events error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch calendar events")


# ---------------------------------------------------------------------------
# Google Calendar Two-Way Sync (Push Notifications)
# ---------------------------------------------------------------------------

@router.post("/google/webhook")
async def google_calendar_webhook(request: Request):
    """Receive push notifications from Google Calendar.

    Google sends a POST here whenever an event is created, updated,
    or deleted on the watched calendar. We then fetch recent changes
    and sync them back to our appointments table.
    """
    channel_id = request.headers.get("X-Goog-Channel-ID", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    resource_id = request.headers.get("X-Goog-Resource-ID", "")

    logging.warning(
        "[CALENDAR WEBHOOK] state=%s channel=%s resource=%s",
        resource_state, channel_id, resource_id,
    )

    # 'sync' is the initial handshake -- just acknowledge
    if resource_state == "sync":
        return {"status": "sync acknowledged"}

    # 'exists' means something changed -- fetch and sync
    if resource_state == "exists":
        try:
            _sync_admin_calendar_changes()
        except Exception as e:
            logging.error("[CALENDAR WEBHOOK] Sync failed: %s", e)

    return {"status": "ok"}


def _sync_admin_calendar_changes():
    """Fetch recent events from admin calendar and sync to DB."""
    from src.utils.db import get_admin_calendar_credentials
    from datetime import datetime, timedelta, timezone

    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        logging.warning("[CALENDAR SYNC] Admin calendar not connected, skipping")
        return

    if admin_creds.get("provider") != "google":
        return

    cal = GoogleCalendarService(admin_creds["credentials"])

    # Fetch events from the last 1 hour to catch recent changes
    now = datetime.now(timezone.utc)
    updated_min = (now - timedelta(hours=1)).isoformat()

    try:
        events_result = cal.service.events().list(
            calendarId="primary",
            updatedMin=updated_min,
            singleEvents=True,
            orderBy="updated",
            maxResults=50,
            showDeleted=True,
        ).execute()
    except Exception as e:
        logging.error("[CALENDAR SYNC] Failed to list events: %s", e)
        return

    # Save refreshed token
    updated_creds = cal.get_updated_credentials()
    from src.utils.db import save_admin_calendar_credentials
    try:
        save_admin_calendar_credentials(
            admin_creds.get("provider", "google"),
            admin_creds.get("email", ""),
            updated_creds,
        )
    except Exception:
        pass

    events = events_result.get("items", [])
    logging.warning("[CALENDAR SYNC] Processing %d changed events", len(events))

    from src.utils.db import (
        get_appointment_by_calendar_event_id,
        update_appointment_status,
    )

    for event in events:
        event_id = event.get("id")
        status = event.get("status", "confirmed")

        appt = get_appointment_by_calendar_event_id(event_id)
        if not appt:
            continue

        if status == "cancelled" and appt["status"] != "cancelled":
            update_appointment_status(appt["id"], "cancelled")
            logging.warning(
                "[CALENDAR SYNC] Appointment %d cancelled via calendar", appt["id"]
            )
        elif status == "confirmed" and appt["status"] == "cancelled":
            update_appointment_status(appt["id"], "scheduled")
            logging.warning(
                "[CALENDAR SYNC] Appointment %d re-scheduled via calendar", appt["id"]
            )

        # Sync time changes
        if status != "cancelled":
            start_raw = event.get("start", {}).get("dateTime")
            end_raw = event.get("end", {}).get("dateTime")
            if start_raw and end_raw:
                from dateutil.parser import parse as dt_parse
                new_start = dt_parse(start_raw)
                new_end = dt_parse(end_raw)
                if (str(appt["start_time"]) != str(new_start)
                        or str(appt["end_time"]) != str(new_end)):
                    from src.utils.db import update_appointment_times
                    update_appointment_times(appt["id"], new_start, new_end)
                    logging.warning(
                        "[CALENDAR SYNC] Appointment %d rescheduled: %s -> %s",
                        appt["id"], new_start, new_end,
                    )


@router.post("/google/watch/start")
async def start_google_watch(current_user: dict = Depends(get_current_user)):
    """Register a Google Calendar push notification channel for the admin calendar."""
    import uuid

    admin_user = current_user
    if not admin_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")

    from src.utils.db import get_admin_calendar_credentials
    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        raise HTTPException(status_code=400, detail="Admin calendar not connected")

    cal = GoogleCalendarService(admin_creds["credentials"])

    base_url = os.getenv("BASE_URL", "https://aisystem.unitedhomecarolina.com")
    channel_id = str(uuid.uuid4())

    try:
        watch_response = cal.service.events().watch(
            calendarId="primary",
            body={
                "id": channel_id,
                "type": "web_hook",
                "address": f"{base_url}/api/calendar/google/webhook",
            },
        ).execute()

        from src.utils.db import save_calendar_watch_channel
        save_calendar_watch_channel(
            channel_id=watch_response["id"],
            resource_id=watch_response["resourceId"],
            expiration=watch_response.get("expiration"),
        )

        logging.warning(
            "[CALENDAR WATCH] Registered channel=%s, expires=%s",
            watch_response["id"], watch_response.get("expiration"),
        )

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "channel_id": watch_response["id"],
                "expiration": watch_response.get("expiration"),
            },
        )
    except Exception as e:
        logging.error("[CALENDAR WATCH] Failed to register: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to register watch: {e}")


@router.post("/google/watch/stop")
async def stop_google_watch(current_user: dict = Depends(get_current_user)):
    """Stop the Google Calendar push notification channel."""
    admin_user = current_user
    if not admin_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")

    from src.utils.db import (
        get_admin_calendar_credentials,
        get_calendar_watch_channel,
        delete_calendar_watch_channel,
    )
    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        raise HTTPException(status_code=400, detail="Admin calendar not connected")

    watch = get_calendar_watch_channel()
    if not watch:
        raise HTTPException(status_code=404, detail="No active watch channel")

    cal = GoogleCalendarService(admin_creds["credentials"])

    try:
        cal.service.channels().stop(body={
            "id": watch["channel_id"],
            "resourceId": watch["resource_id"],
        }).execute()
    except Exception as e:
        logging.warning("[CALENDAR WATCH] Stop error (may already be expired): %s", e)

    delete_calendar_watch_channel()

    return JSONResponse(
        status_code=200,
        content={"success": True, "message": "Watch channel stopped"},
    )


@router.post("/google/sync")
async def full_calendar_sync(current_user: dict = Depends(get_current_user)):
    """Pull all future events from admin calendar into the DB.

    - Skips events already in the DB (matched by Google event ID)
    - Imports new events as 'scheduled' appointments
    - Updates times/status on existing events if they changed
    Call this once after connecting the calendar, or anytime to force a full sync.
    """
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")

    result = run_full_calendar_sync()
    return JSONResponse(status_code=200, content={"success": True, **result})


def _match_calendar_to_tech(cal_name, techs):
    """Fuzzy-match a Google sub-calendar name to a technician.

    Matching rules (case-insensitive):
    1. Exact full name match
    2. Calendar name is contained in tech name or vice versa
    3. First word of calendar name matches first word of tech name

    Returns the matched tech dict or None.
    """
    if not cal_name or not techs:
        return None

    cal_lower = cal_name.strip().lower()
    cal_first = cal_lower.split()[0] if cal_lower else ""

    # Pass 1: exact match
    for tech in techs:
        tech_name = (tech.get("name") or "").strip().lower()
        if tech_name and tech_name == cal_lower:
            return tech

    # Pass 2: one contains the other
    for tech in techs:
        tech_name = (tech.get("name") or "").strip().lower()
        if tech_name and (cal_lower in tech_name or tech_name in cal_lower):
            return tech

    # Pass 3: first-word match (handles "Holland" calendar -> "Holland Darcy" tech)
    for tech in techs:
        tech_name = (tech.get("name") or "").strip().lower()
        tech_first = tech_name.split()[0] if tech_name else ""
        if cal_first and tech_first and cal_first == tech_first:
            return tech

    # Pass 4: first 3 chars match (handles typos like Chimeny)
    for tech in techs:
        tech_name = (tech.get("name") or "").strip().lower()
        tech_first = tech_name.split()[0] if tech_name else ""
        if len(cal_first) >= 3 and len(tech_first) >= 3 and cal_first[:3] == tech_first[:3]:
            return tech

    return None


def _sync_events_for_calendar(cal_service, calendar_id, calendar_name,
                              tech_id, now, future):
    """Fetch events from one sub-calendar and sync them into the DB.

    Returns (synced_count, skipped_count).
    """
    from src.utils.db import (
        get_appointment_by_calendar_event_id,
        update_appointment_status,
        update_appointment_times,
        insert_appointment,
    )
    from dateutil.parser import parse as dt_parse

    try:
        events_result = cal_service.events().list(
            calendarId=calendar_id,
            timeMin=now.isoformat(),
            timeMax=future.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=500,
            showDeleted=True,
        ).execute()
    except Exception as e:
        logging.error(
            "[CALENDAR SYNC] Failed to list events for '%s': %s",
            calendar_name, e,
        )
        return 0, 0

    events = events_result.get("items", [])
    synced = 0
    skipped = 0

    for event in events:
        google_event_id = event.get("id")
        status = event.get("status", "confirmed")

        existing = get_appointment_by_calendar_event_id(google_event_id)

        if existing:
            changed = False
            if status == "cancelled" and existing["status"] != "cancelled":
                update_appointment_status(existing["id"], "cancelled")
                changed = True
            elif status == "confirmed" and existing["status"] == "cancelled":
                update_appointment_status(existing["id"], "scheduled")
                changed = True

            if status != "cancelled":
                start_raw = event.get("start", {}).get("dateTime")
                end_raw = event.get("end", {}).get("dateTime")
                if start_raw and end_raw:
                    new_start = dt_parse(start_raw)
                    new_end = dt_parse(end_raw)
                    if (str(existing["start_time"]) != str(new_start)
                            or str(existing["end_time"]) != str(new_end)):
                        update_appointment_times(existing["id"], new_start, new_end)
                        changed = True

            if changed:
                synced += 1
            else:
                skipped += 1
            continue

        # New event -- skip cancelled
        if status == "cancelled":
            skipped += 1
            continue

        start_raw = event.get("start", {}).get("dateTime")
        end_raw = event.get("end", {}).get("dateTime")
        if not start_raw or not end_raw:
            skipped += 1
            continue

        start_dt = dt_parse(start_raw)
        end_dt = dt_parse(end_raw)
        duration = int((end_dt - start_dt).total_seconds() / 60)

        summary = event.get("summary", "")

        customer_name = "Calendar Import"
        if " - " in summary:
            customer_name = summary.split(" - ", 1)[1].strip()
        elif summary:
            customer_name = summary

        service_type = "other"
        summary_lower = summary.lower()
        for svc in ["air_duct", "chimney", "dryer_vent", "gutter", "power_washing"]:
            if svc.replace("_", " ") in summary_lower or svc in summary_lower:
                service_type = svc
                break

        try:
            insert_appointment(
                calendar_event_id=google_event_id,
                technician_id=tech_id,
                customer_name=customer_name,
                customer_phone=None,
                customer_email=None,
                service_type=service_type,
                address=event.get("location"),
                latitude=None,
                longitude=None,
                start_time=start_dt,
                end_time=end_dt,
                duration_minutes=duration,
                status="scheduled",
            )
            synced += 1
            logging.warning(
                "[CALENDAR SYNC] Imported: '%s' -> tech_id=%s at %s",
                summary, tech_id, start_dt,
            )
        except Exception as e:
            logging.error("[CALENDAR SYNC] Failed to import event: %s", e)
            skipped += 1

    return synced, skipped


def run_full_calendar_sync():
    """Sync all sub-calendars from the admin's Google account into the DB.

    For each sub-calendar:
    1. Fuzzy-match the calendar name to a technician
    2. Fetch future events (next 90 days)
    3. Import new events with the matched technician_id
    4. Update existing events if times/status changed
    5. Skip duplicates (matched by Google event ID)
    """
    from src.utils.db import (
        get_admin_calendar_credentials,
        get_all_technicians,
        save_admin_calendar_credentials,
    )
    from datetime import datetime, timedelta, timezone

    admin_creds = get_admin_calendar_credentials()
    if not admin_creds or not admin_creds.get("connected"):
        return {"synced": 0, "skipped": 0, "message": "Admin calendar not connected"}

    if admin_creds.get("provider") != "google":
        return {"synced": 0, "skipped": 0, "message": "Only Google supported"}

    cal = GoogleCalendarService(admin_creds["credentials"])

    # Refresh token early
    updated_creds = cal.get_updated_credentials()
    try:
        save_admin_calendar_credentials(
            admin_creds.get("provider", "google"),
            admin_creds.get("email", ""),
            updated_creds,
        )
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    future = now + timedelta(days=90)

    # Get all sub-calendars from admin's Google account
    try:
        calendar_list = cal.service.calendarList().list().execute()
        sub_calendars = calendar_list.get("items", [])
    except Exception as e:
        logging.error("[CALENDAR SYNC] Failed to list sub-calendars: %s", e)
        return {"synced": 0, "skipped": 0, "error": str(e)}

    logging.warning(
        "[CALENDAR SYNC] Found %d sub-calendars: %s",
        len(sub_calendars),
        [c.get("summary", "?") for c in sub_calendars],
    )

    # Load all active technicians for name matching
    techs = get_all_technicians()
    logging.warning(
        "[CALENDAR SYNC] Active techs: %s",
        [(t["id"], t["name"]) for t in techs],
    )

    total_synced = 0
    total_skipped = 0
    matched_calendars = []

    for sub_cal in sub_calendars:
        cal_id = sub_cal.get("id", "")
        cal_name = sub_cal.get("summary", "")
        cal_primary = sub_cal.get("primary", False)

        # Skip the primary calendar (that's the admin's own calendar)
        if cal_primary:
            logging.info("[CALENDAR SYNC] Skipping primary calendar: %s", cal_name)
            continue

        # Try to match this sub-calendar to a technician
        matched_tech = _match_calendar_to_tech(cal_name, techs)
        tech_id = matched_tech["id"] if matched_tech else None
        tech_name = matched_tech["name"] if matched_tech else "UNMATCHED"

        logging.warning(
            "[CALENDAR SYNC] Sub-calendar '%s' -> tech: %s (id=%s)",
            cal_name, tech_name, tech_id,
        )
        matched_calendars.append({
            "calendar": cal_name,
            "tech": tech_name,
            "tech_id": tech_id,
        })

        synced, skipped = _sync_events_for_calendar(
            cal.service, cal_id, cal_name, tech_id, now, future,
        )
        total_synced += synced
        total_skipped += skipped

    logging.warning(
        "[CALENDAR SYNC] Done: %d synced, %d skipped, %d calendars processed",
        total_synced, total_skipped, len(matched_calendars),
    )
    return {
        "synced": total_synced,
        "skipped": total_skipped,
        "calendars": matched_calendars,
    }
