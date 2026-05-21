"""Retell webhook handler -- processes call events and injects dynamic variables."""
import os
import json
import logging
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

from src.utils.db import upsert_call_log
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

RETELL_API_KEY = os.getenv("RETELL_API_KEY", "")

retell_client = None
if RETELL_API_KEY:
    try:
        from retell import Retell
        retell_client = Retell(api_key=RETELL_API_KEY)
    except Exception as e:
        logging.warning("Retell SDK init failed: %s", e)


def _get_current_date_string():
    """Return the current date/time in Eastern Time as a human-readable string.

    Example: 'Tuesday, February 25, 2026. Current time: 3:45 PM ET'
    """
    eastern = ZoneInfo("America/New_York")
    now = datetime.now(eastern)
    return now.strftime("%A, %B %d, %Y. Current time: %I:%M %p ET")


def run_full_calendar_sync_bg():
    try:
        from src.api.calendar import run_full_calendar_sync
        result = run_full_calendar_sync()
        logging.info(f"[CALL SYNC] Calendar synced in background on call start: {result}")
    except Exception as e:
        logging.warning(f"[CALL SYNC] Background calendar sync failed: {e}")


@router.post("/retell")
async def retell_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Retell webhook events.

    On call_started: returns dynamic variables including current_date.
    On call_ended/call_analyzed: stores the call log in the database.
    """
    try:
        post_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    if retell_client and RETELL_API_KEY:
        try:
            valid_signature = retell_client.verify(
                json.dumps(post_data, separators=(",", ":"), ensure_ascii=False),
                api_key=str(RETELL_API_KEY),
                signature=str(request.headers.get("X-Retell-Signature", "")),
            )
            if not valid_signature:
                logging.warning("Invalid Retell webhook signature")
                return JSONResponse(status_code=401, content={"message": "Unauthorized"})
        except Exception as e:
            logging.warning("Signature verification error: %s", e)
            return JSONResponse(status_code=401, content={"message": "Signature verification failed"})

    event = post_data.get("event")
    call = post_data.get("call") or post_data.get("data", {})

    if not event or not call.get("call_id"):
        return JSONResponse(status_code=200, content={"message": "ok"})

    logging.info("Retell webhook: %s for call %s", event, call.get("call_id"))

    if event == "call_started":
        current_date = _get_current_date_string()
        logging.info(
            "Injecting current_date for call %s: %s",
            call.get("call_id"), current_date,
        )
        background_tasks.add_task(run_full_calendar_sync_bg)

        return JSONResponse(status_code=200, content={
            "retell_llm_dynamic_variables": {
                "current_date": current_date,
            }
        })

    call_data = {
        "call_id": call.get("call_id"),
        "agent_id": call.get("agent_id"),
        "call_type": call.get("call_type"),
        "direction": call.get("direction"),
        "from_number": call.get("from_number"),
        "to_number": call.get("to_number"),
        "call_status": call.get("call_status"),
        "disconnection_reason": call.get("disconnection_reason"),
        "start_timestamp": call.get("start_timestamp"),
        "end_timestamp": call.get("end_timestamp"),
        "recording_url": call.get("recording_url"),
        "transcript": call.get("transcript"),
        "transcript_object": call.get("transcript_object"),
        "metadata": call.get("metadata"),
        "retell_llm_dynamic_variables": call.get("retell_llm_dynamic_variables"),
    }

    if event == "call_analyzed":
        call_data["call_analysis"] = call.get("call_analysis")

    try:
        upsert_call_log(call_data)
    except Exception as e:
        logging.error("Failed to store call log: %s", e)
        traceback.print_exc()

    return JSONResponse(status_code=200, content={"message": "ok"})
