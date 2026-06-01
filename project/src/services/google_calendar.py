import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")


class GoogleCalendarService:

    def __init__(self, credentials_dict: Dict):
        self.credentials = Credentials(
            token=credentials_dict.get("access_token"),
            refresh_token=credentials_dict.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=credentials_dict.get("scopes", ["https://www.googleapis.com/auth/calendar"])
        )
        self._refresh_if_needed()
        self.service = build("calendar", "v3", credentials=self.credentials)

    def _refresh_if_needed(self):
        if self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())

    def list_events(self, time_min: datetime, time_max: datetime = None, max_results: int = 100):
        try:
            if not time_max:
                time_max = time_min + timedelta(days=7)
            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=time_min.isoformat() + "Z" if not time_min.tzinfo else time_min.isoformat(),
                timeMax=time_max.isoformat() + "Z" if not time_max.tzinfo else time_max.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime"
            ).execute()
            events = events_result.get("items", [])
            result = []
            for event in events:
                start = event["start"].get("dateTime", event["start"].get("date"))
                end = event["end"].get("dateTime", event["end"].get("date"))
                result.append({
                    "id": event["id"],
                    "summary": event.get("summary", ""),
                    "start": start,
                    "end": end,
                    "description": event.get("description", ""),
                    "location": event.get("location", ""),
                    "status": event.get("status", "confirmed")
                })
            return result
        except HttpError as e:
            logging.error(f"Google Calendar list error: {e}")
            return []

    def check_availability(self, start_datetime: datetime, end_datetime: datetime, service_type: str = "other"):
        try:
            from zoneinfo import ZoneInfo
            eastern = ZoneInfo("America/New_York")
            if start_datetime.tzinfo is None:
                start_datetime = start_datetime.replace(tzinfo=eastern)
            if end_datetime.tzinfo is None:
                end_datetime = end_datetime.replace(tzinfo=eastern)
            req_date = start_datetime.astimezone(eastern).date()
            day_start = datetime(req_date.year, req_date.month, req_date.day, 0, 0, 0, tzinfo=eastern)
            day_end = datetime(req_date.year, req_date.month, req_date.day, 23, 59, 59, tzinfo=eastern)

            _GAP = {
                "air_duct": 180,
                "power_washing": 150,
            }
            gap_minutes = _GAP.get(service_type, 120)

            events_result = self.service.events().list(
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
                if not s or not e:
                    continue
                from dateutil.parser import parse as _parse
                ev_start = _parse(s)
                ev_end = _parse(e)
                if ev_start.tzinfo is None:
                    ev_start = ev_start.replace(tzinfo=timezone.utc)
                if ev_end.tzinfo is None:
                    ev_end = ev_end.replace(tzinfo=timezone.utc)
                first_end = ev_end if ev_start <= start_datetime else end_datetime
                second_start = start_datetime if ev_start <= start_datetime else ev_start
                if (second_start - first_end) < timedelta(minutes=gap_minutes):
                    return False
            return True
        except Exception as e:
            logging.error(f"Google Calendar availability check error: {e}")
            return True

    def create_event(self, summary: str, start_datetime: datetime, end_datetime: datetime,
                     description: str = '', location: str = '', attendees: List[str] = None,
                     color_id: str = None, calendar_id: str = "primary"):
        try:
            event = {
                "summary": summary,
                "location": location,
                "description": description,
                "start": {
                    "dateTime": start_datetime.isoformat(),
                    "timeZone": "America/New_York"
                },
                "end": {
                    "dateTime": end_datetime.isoformat(),
                    "timeZone": "America/New_York"
                }
            }
            if color_id:
                event["colorId"] = str(color_id)
            if attendees:
                event["attendees"] = [{"email": email} for email in attendees]
            created = self.service.events().insert(
                calendarId=calendar_id,
                body=event,
                sendUpdates="all" if attendees else "none"
            ).execute()
            return {
                "id": created["id"],
                "link": created.get("htmlLink", ""),
                "status": created.get("status", "confirmed")
            }
        except HttpError as e:
            logging.error(f"Google Calendar create error: {e}")
            return None

    def delete_event(self, event_id: str, calendar_id: str = "primary"):
        try:
            self.service.events().delete(
                calendarId=calendar_id,
                eventId=event_id
            ).execute()
            return True
        except HttpError as e:
            logging.error(f"Google Calendar delete error: {e}")
            return False

    def get_updated_credentials(self):
        self._refresh_if_needed()
        return {
            "access_token": self.credentials.token,
            "refresh_token": self.credentials.refresh_token,
            "token_expiry": self.credentials.expiry.isoformat() if self.credentials.expiry else None,
            "scopes": list(self.credentials.scopes) if self.credentials.scopes else []
        }
