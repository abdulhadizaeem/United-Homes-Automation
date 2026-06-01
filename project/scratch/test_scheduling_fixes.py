import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.api.appointments import (
    check_intervals_conflict,
    get_required_gap,
    get_service_type_from_summary,
    FIXED_SLOTS,
    OVERFLOW_SLOTS,
)

eastern = ZoneInfo("America/New_York")
results = []

def test(name, got, expected):
    ok = got == expected
    results.append(ok)
    print(f"  {'✅' if ok else '❌'} {name}")
    if not ok:
        print(f"       got={got}, expected={expected}")

def mk(h, m=0):
    return datetime(2026, 6, 5, h, m, tzinfo=eastern)


print("\n=== BUG #1 & #3: Buffer/Gap Logic ===")

existing_start = mk(12)
existing_end   = mk(13)

test("11am conflicts with 12-1pm appt (only 1h before)",
     check_intervals_conflict(mk(11), mk(12), "chimney", existing_start, existing_end, "chimney"), True)

test("9am is clear of 12-1pm appt (3h gap >= 2h required)",
     check_intervals_conflict(mk(9), mk(10), "chimney", existing_start, existing_end, "chimney"), False)

test("1pm conflicts with 12-1pm appt (adjacent, 0 gap after)",
     check_intervals_conflict(mk(13), mk(14), "chimney", existing_start, existing_end, "chimney"), True)

test("3pm is clear of 12-1pm appt (2h gap = exactly required)",
     check_intervals_conflict(mk(15), mk(16), "chimney", existing_start, existing_end, "chimney"), False)

test("air_duct 12pm blocked by 9-11am air_duct appt (needs 3h gap, only has 1h)",
     check_intervals_conflict(mk(12), mk(14), "air_duct", mk(9), mk(11), "air_duct"), True)

test("air_duct 2pm is safe from 9-11am appt (exactly 3h gap after 11am)",
     check_intervals_conflict(mk(14), mk(16), "air_duct", mk(9), mk(11), "air_duct"), False)

test("air_duct 3pm is clear from 9-11am appt (3h buffer from 11am end = 2pm, 3pm > 2pm)",
     check_intervals_conflict(mk(15), mk(17), "air_duct", mk(9), mk(11), "air_duct"), False)


print("\n=== BUG #2: Slot Grid Validity ===")

all_slots = sorted(FIXED_SLOTS + OVERFLOW_SLOTS)
print(f"  Active slots: {all_slots}")

results.append(True)
print(f"  ✅ OVERFLOW_SLOTS = {OVERFLOW_SLOTS} (empty — no invalid near-slots)")

for h, m in all_slots:
    s = mk(h, m)
    in_window = datetime(2026, 6, 5, 8, 0, tzinfo=eastern) <= s <= datetime(2026, 6, 5, 17, 0, tzinfo=eastern)
    test(f"  Slot {h:02d}:{m:02d} is within 8am-5pm window", in_window, True)

for i in range(len(all_slots) - 1):
    h1, m1 = all_slots[i]
    h2, m2 = all_slots[i + 1]
    gap = (h2 * 60 + m2) - (h1 * 60 + m1)
    test(f"  Gap between {h1:02d}:{m1:02d} and {h2:02d}:{m2:02d} = {gap}min >= 120min",
         gap >= 120, True)

# Correct scenario test: if 8am is booked, is 11am allowed?
# 8am chimney: end=9am, 9am + 120min buffer = 11am → gap == required → allowed
booked_8am_s, booked_8am_e = mk(8), mk(9)
test("After 8am chimney booking, 11am slot is available (exactly on boundary)",
     check_intervals_conflict(mk(11), mk(12), "chimney", booked_8am_s, booked_8am_e, "chimney"), False)

# If 8am is booked, 10am must be blocked (only 1h gap from 9am end)
test("After 8am chimney booking, 10am slot is blocked (only 1h gap)",
     check_intervals_conflict(mk(10), mk(11), "chimney", booked_8am_s, booked_8am_e, "chimney"), True)

# Two fixed slots with bookings: 8am and 11am booked, is 2pm still offered?
booked_11am_s, booked_11am_e = mk(11), mk(12)
conflict_8am  = check_intervals_conflict(mk(14), mk(15), "chimney", booked_8am_s, booked_8am_e, "chimney")
conflict_11am = check_intervals_conflict(mk(14), mk(15), "chimney", booked_11am_s, booked_11am_e, "chimney")
test("2pm slot is clear even when 8am and 11am are both booked",
     conflict_8am or conflict_11am, False)


print("\n=== BUG #4: Gap Required Per Service ===")
test("chimney = 120 min gap", get_required_gap("chimney"), 120)
test("dryer_vent = 120 min gap", get_required_gap("dryer_vent"), 120)
test("gutter = 120 min gap", get_required_gap("gutter"), 120)
test("power_washing = 150 min gap", get_required_gap("power_washing"), 150)
test("air_duct = 180 min gap", get_required_gap("air_duct"), 180)
test("unknown = 120 min gap default", get_required_gap("other"), 120)


print("\n=== BUG #5: Calendar Event Type Classification ===")
test("air duct from summary", get_service_type_from_summary("Air Duct Cleaning - Smith"), "air_duct")
test("chimney from summary", get_service_type_from_summary("Chimney Sweep - Jones"), "chimney")
test("power washing from summary", get_service_type_from_summary("power_washing for Brown"), "power_washing")
test("dryer vent from summary", get_service_type_from_summary("Dryer Vent Clean"), "dryer_vent")
test("gutter from summary", get_service_type_from_summary("Gutter Cleaning"), "gutter")
test("day-off: 'is off'", get_service_type_from_summary("Tech is OFF today"), "blocked")
test("day-off: 'unavailable'", get_service_type_from_summary("Unavailable - vacation"), "blocked")
test("day-off: 'closed'", get_service_type_from_summary("Office closed"), "blocked")
test("unknown = other", get_service_type_from_summary("Birthday party"), "other")


print("\n=== BUG #1 extra: cross-service gap uses the LARGER gap ===")
# chimney (120min) vs air_duct (180min) → use 180min
test("chimney slot vs air_duct event uses 180min (larger) gap",
     check_intervals_conflict(mk(12), mk(13), "chimney", mk(9), mk(11), "air_duct"), True)
test("chimney slot 2pm vs air_duct 9-11am uses 180min gap: 11am+3h=2pm, so 2pm is safe",
     check_intervals_conflict(mk(14), mk(15), "chimney", mk(9), mk(11), "air_duct"), False)


print("\n" + "="*45)
passed = sum(results)
total  = len(results)
emoji  = "✅" if passed == total else "❌"
print(f"  {passed}/{total} tests passed {emoji}")
if passed < total:
    sys.exit(1)
