"""Debug script v2: uses GoogleCalendarService properly."""
import json
import sys
import os

# Set env vars so GoogleCalendarService can use them
os.environ["GOOGLE_CLIENT_ID"] = "428835219835-25ltd6poeaarrki4d96ocu38fhpf0etv.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "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"

import psycopg2
from psycopg2.extras import RealDictCursor
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_HOST = "localhost"
DB_PORT = 5432
DB_NAME = "unitedhomes_db"
DB_USER = "unitedhomes_user"
DB_PASS = "Unitedhome1234"

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]


def main():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASS,
    )
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Get admin calendar credentials
    cur.execute("SELECT calendar_credentials FROM technicians WHERE id = 5")
    row = cur.fetchone()
    creds_dict = row["calendar_credentials"]
    if isinstance(creds_dict, str):
        creds_dict = json.loads(creds_dict)

    print(f"Credential keys: {list(creds_dict.keys())}")

    # Build credentials the same way GoogleCalendarService does
    creds = Credentials(
        token=creds_dict.get("access_token"),
        refresh_token=creds_dict.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=creds_dict.get("scopes", ["https://www.googleapis.com/auth/calendar"]),
    )

    # Refresh if needed
    if creds.expired or not creds.valid:
        creds.refresh(Request())
        print("Token refreshed")

    service = build("calendar", "v3", credentials=creds)

    # List ALL calendars
    cal_list = service.calendarList().list(showHidden=True).execute()
    items = cal_list.get("items", [])
    print(f"\nFound {len(items)} calendars total:")
    for c in items:
        print(f"  {'[PRIMARY] ' if c.get('primary') else ''}"
              f"'{c.get('summary', '?')}' (id={c['id'][:40]})")

    # Get techs
    cur.execute("SELECT id, name FROM technicians WHERE status = 'active'")
    techs = {t["name"].strip().lower(): t["id"] for t in cur.fetchall()}
    print(f"\nActive techs: {techs}")

    # Events for next 7 days on EACH calendar
    eastern = ZoneInfo("America/New_York")
    today = datetime.now(eastern).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = today + timedelta(days=7)
    print(f"\nChecking events: {today.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}")

    for cal in items:
        cal_id = cal["id"]
        cal_name = cal.get("summary", "?")
        is_primary = cal.get("primary", False)

        try:
            evts = service.events().list(
                calendarId=cal_id,
                timeMin=today.isoformat(),
                timeMax=week_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            ).execute().get("items", [])
        except Exception as e:
            print(f"\n--- '{cal_name}': ERROR: {e}")
            continue

        label = "[PRIMARY] " if is_primary else ""
        print(f"\n--- {label}'{cal_name}': {len(evts)} events ---")
        for e in evts:
            s = e.get("start", {})
            if s.get("dateTime"):
                st = s["dateTime"]
                etype = "TIMED"
            elif s.get("date"):
                st = s["date"]
                etype = "ALL-DAY"
            else:
                st = "?"
                etype = "?"
            end_s = e.get("end", {})
            et = end_s.get("dateTime") or end_s.get("date", "?")
            print(f"  [{etype}] '{e.get('summary', '?')}' "
                  f"| {st} -> {et} | status={e.get('status', '?')}")

    conn.close()
    print("\nDONE")


if __name__ == "__main__":
    main()
