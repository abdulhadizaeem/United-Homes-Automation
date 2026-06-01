from src.api.appointments import find_technician_availability
from pydantic import BaseModel
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import Request

class Req(BaseModel):
    requested_date: str
    service_type: str
    confirmed_latitude: float
    confirmed_longitude: float
    job_units: int = None

eastern = ZoneInfo("America/New_York")
date_str = datetime.now(eastern).strftime("%Y-%m-%d")

print(f"Testing availability for {date_str}...")

req = Req(
    requested_date=date_str,
    service_type="chimney",
    confirmed_latitude=35.0,
    confirmed_longitude=-80.0
)

# Call function
from src.api.appointments import FindTechnicianRequest
real_req = FindTechnicianRequest(**req.dict())
try:
    resp = find_technician_availability(real_req)
    print("Response:")
    print(f"Available: {resp.available}")
    print(f"Best Slot: {resp.time_slot}")
    print(f"Alternatives: {resp.alternative_slots}")
except Exception as e:
    print(e)
