import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
import requests
import msal
from dotenv import load_dotenv

load_dotenv()

MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")


class OutlookCalendarService:

    GRAPH_API_ENDPOINT = "https://graph.microsoft.com/v1.0"

    def __init__(self, credentials_dict: Dict):
        self.access_token = credentials_dict.get("access_token")
        self.refresh_token = credentials_dict.get("refresh_token")
        self.token_expiry = credentials_dict.get("token_expiry")
        self.scopes = credentials_dict.get("scopes", ["Calendars.ReadWrite"])
        self._app = msal.ConfidentialClientApplication(
            MICROSOFT_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}",
            client_credential=MICROSOFT_CLIENT_SECRET
        )
        self._refresh_if_needed()

    def _refresh_if_needed(self):
        if not self.token_expiry:
            return
        try:
            expiry = datetime.fromisoformat(self.token_expiry.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) >= expiry - timedelta(minutes=5):
                result = self._app.acquire_token_by_refresh_token(
                    self.refresh_token,
                    scopes=self.scopes
                )
                if "access_token" in result:
                    self.access_token = result["access_token"]
                    if "refresh_token" in result:
                        self.refresh_token = result["refresh_token"]
                    if "expires_in" in result:
                        self.token_expiry = (
                            datetime.now(timezone.utc) + timedelta(seconds=result["expires_in"])
                        ).isoformat()
        except Exception as e:
            logging.error(f"Outlook token refresh error: {e}")

    def _make_request(self, method: str, endpoint: str, **kwargs):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        url = f"{self.GRAPH_API_ENDPOINT}{endpoint}"
        try:
            response = requests.request(method, url, headers=headers, **kwargs)
            if response.status_code in [200, 201]:
                return response.json()
            logging.error(f"Outlook API error {response.status_code}: {response.text}")
            return None
        except Exception as e:
            logging.error(f"Outlook request error: {e}")
            return None

    def list_events(self, time_min: datetime, time_max: datetime = None, max_results: int = 100):
        if not time_max:
            time_max = time_min + timedelta(days=7)
        params = {
            "$filter": f"start/dateTime ge '{time_min.isoformat()}' and end/dateTime le '{time_max.isoformat()}'",
            "$top": max_results,
            "$orderby": "start/dateTime"
        }
        result = self._make_request("GET", "/me/calendar/events", params=params)
        if not result:
            return []
        events = []
        for event in result.get("value", []):
            events.append({
                "id": event["id"],
                "summary": event.get("subject", ""),
                "start": event["start"]["dateTime"],
                "end": event["end"]["dateTime"],
                "description": event.get("bodyPreview", ""),
                "location": event.get("location", {}).get("displayName", ""),
                "status": "confirmed" if not event.get("isCancelled") else "cancelled"
            })
        return events

    def check_availability(self, start_datetime: datetime, end_datetime: datetime):
        try:
            events = self.list_events(start_datetime, end_datetime)
            return len(events) == 0
        except Exception as e:
            logging.error(f"Outlook availability check error: {e}")
            return True

    def create_event(self, summary: str, start_datetime: datetime, end_datetime: datetime,
                     description: str = '', location: str = '', attendees: List[str] = None):
        event = {
            "subject": summary,
            "body": {"contentType": "text", "content": description},
            "start": {"dateTime": start_datetime.isoformat(), "timeZone": "Eastern Standard Time"},
            "end": {"dateTime": end_datetime.isoformat(), "timeZone": "Eastern Standard Time"}
        }
        if location:
            event["location"] = {"displayName": location}
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": email}, "type": "required"}
                for email in attendees
            ]
        result = self._make_request("POST", "/me/calendar/events", json=event)
        if result:
            return {
                "id": result["id"],
                "link": result.get("webLink", ""),
                "status": "confirmed"
            }
        return None

    def delete_event(self, event_id: str):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        url = f"{self.GRAPH_API_ENDPOINT}/me/calendar/events/{event_id}"
        try:
            response = requests.request("DELETE", url, headers=headers)
            if response.status_code in [200, 204]:
                return True
            logging.error(f"Outlook API delete error {response.status_code}: {response.text}")
            return False
        except Exception as e:
            logging.error(f"Outlook delete request error: {e}")
            return False

    def get_updated_credentials(self):
        self._refresh_if_needed()
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expiry": self.token_expiry,
            "scopes": self.scopes
        }
