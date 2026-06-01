import os
import json
import logging

logging.basicConfig(level=logging.INFO)

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calendar_creds_cache.json")

def main():
    if not os.path.exists(CACHE_PATH):
        print("Cache not found!")
        return
        
    with open(CACHE_PATH, "r") as f:
        creds_data = json.load(f)
        
    from src.services.google_calendar import GoogleCalendarService
    cal = GoogleCalendarService(creds_data["credentials"])
    
    cal_list = cal.service.calendarList().list().execute()
    print("--- Admin's Connected Calendars ---")
    for sc in cal_list.get("items", []):
        print(f"Name: {sc.get('summary')} | ID: {sc.get('id')} | Primary: {sc.get('primary')}")

if __name__ == "__main__":
    main()
