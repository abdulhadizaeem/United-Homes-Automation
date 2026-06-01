import os
import sys
from dotenv import load_dotenv
from retell import Retell

# Load the Retell API Key from your .env file
load_dotenv()
api_key = os.getenv("RETELL_API_KEY")

if not api_key:
    print("Error: RETELL_API_KEY not found in .env file.")
    sys.exit(1)

client = Retell(api_key=api_key)

print("=== Make Retell Call You ===")
print("This script will tell Retell to call your personal phone number so you can talk to the AI.")
print("Note: You must have a phone number purchased and configured in your Retell dashboard.\n")

from_number = input("Enter your Retell Phone Number (e.g., +12345678900): ").strip()
to_number = input("Enter YOUR Cell Phone Number (e.g., +10987654321): ").strip()

if not from_number or not to_number:
    print("Both phone numbers are required.")
    sys.exit(1)

print(f"\nInitiating call from {from_number} to {to_number}...")

try:
    # Initiate the outbound phone call
    call = client.call.create_phone_call(
        from_number=from_number,
        to_number=to_number
    )
    
    print("\n✅ Success! Your phone should be ringing shortly.")
    print(f"Call ID: {call.call_id}")
    print("Pick up the phone to talk to your AI agent!")
    
except Exception as e:
    print(f"\n❌ Failed to initiate call: {e}")
