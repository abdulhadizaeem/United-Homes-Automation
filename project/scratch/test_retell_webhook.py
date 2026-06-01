import requests
import json
import uuid
import time
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
RETELL_API_KEY = os.getenv("RETELL_API_KEY")

# You can test against localhost directly or your ngrok URL
BASE_URL = "http://localhost:8000"
WEBHOOK_URL = f"{BASE_URL}/api/webhooks/retell"

def send_webhook(event_type, call_id):
    print(f"\n--- Testing '{event_type}' event ---")
    
    payload = {
        "event": event_type,
        "call": {
            "call_id": call_id,
            "agent_id": "test_agent_123",
            "call_type": "inbound",
            "direction": "inbound",
            "from_number": "+1234567890",
            "to_number": "+0987654321",
            "call_status": "ongoing" if event_type == "call_started" else "ended",
            "start_timestamp": int(datetime.now().timestamp() * 1000),
        }
    }
    
    if event_type == "call_analyzed":
        payload["call"]["call_analysis"] = {
            "user_sentiment": "Positive",
            "call_successful": True,
            "custom_analysis_data": {"appointment_booked": True}
        }
        payload["call"]["end_timestamp"] = int(datetime.now().timestamp() * 1000)
        payload["call"]["transcript"] = "User: Hello\nAgent: Hi there!"

    try:
        # Generate the payload exactly as FastAPI will receive it
        payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        
        headers = {"Content-Type": "application/json"}
        
        if RETELL_API_KEY:
            # Generate the correct cryptographic signature using the API key
            import hmac
            import hashlib
            signature = hmac.new(
                key=RETELL_API_KEY.encode('utf-8'),
                msg=payload_str.encode('utf-8'),
                digestmod=hashlib.sha256
            ).hexdigest()
            headers["X-Retell-Signature"] = signature
            print("  [✓] Generated X-Retell-Signature header")
        
        response = requests.post(
            WEBHOOK_URL, 
            data=payload_str, # Use data instead of json to keep exact string matching
            headers=headers
        )
        
        print(f"Status Code: {response.status_code}")
        print("Response Body:")
        try:
            print(json.dumps(response.json(), indent=2))
        except ValueError:
            print(response.text)
            
    except requests.exceptions.ConnectionError:
        print(f"ERROR: Could not connect to {BASE_URL}.")
        print("Make sure your FastAPI server is running! (uvicorn main:app --reload)")

if __name__ == "__main__":
    # Generate a unique call ID for this test run
    test_call_id = f"test_call_{uuid.uuid4().hex[:8]}"
    
    print(f"Testing Retell Webhook at {WEBHOOK_URL}")
    print("=" * 50)
    
    # 1. Test call_started (this should return dynamic variables like current_date)
    send_webhook("call_started", test_call_id)
    
    time.sleep(1) # small pause
    
    # 2. Test call_analyzed (this should trigger saving the call log to the DB)
    send_webhook("call_analyzed", test_call_id)
    
    print("\nDone! Check your FastAPI server logs to see if it printed 'Retell webhook: call_started' and stored the call log.")
