import sys
import json
import os
import requests

# ── Config ───────────────────────────────────────────────────────────────────
PROD_BASE      = "https://aisystem.unitedhomecarolina.com"
ADMIN_EMAIL    = "unitedhometechportal@gmail.com"
ADMIN_PASSWORD = "admin123456"

# Where the cache file goes (project root)
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "calendar_creds_cache.json",
)
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("Step 1: Logging in to production API …")
    resp = requests.post(
        f"{PROD_BASE}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"❌  Login failed ({resp.status_code}): {resp.text}")
        sys.exit(1)

    token = resp.json().get("access_token")
    if not token:
        print(f"❌  No access_token in login response: {resp.json()}")
        sys.exit(1)
    print("   ✅ Logged in successfully.")

    print("Step 2: Fetching calendar credentials …")
    resp = requests.get(
        f"{PROD_BASE}/api/calendar/dev-seed-creds",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"❌  dev-seed-creds failed ({resp.status_code}): {resp.text}")
        print("    Make sure the latest code is deployed to production first.")
        sys.exit(1)

    creds = resp.json().get("data")
    if not creds:
        print("❌  Response missing 'data' field:", resp.json())
        sys.exit(1)
    print("   ✅ Got credentials.")

    print(f"Step 3: Writing local cache → {CACHE_PATH} …")
    with open(CACHE_PATH, "w") as f:
        json.dump(creds, f, indent=2)
    print(f"   ✅ Done! calendar_creds_cache.json written.")
    print()
    print("Local dev server will now use these credentials for Google Calendar")
    print("conflict checking without needing a direct DB connection.")


if __name__ == "__main__":
    main()
