import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()
db_url = os.getenv("DATABASE_URL")
print(f"Connecting to database...")

conn = psycopg2.connect(db_url)
cur = conn.cursor(cursor_factory=RealDictCursor)

try:
    # 1. Print appointments table schema
    print("\n=== Schema of appointments table ===")
    cur.execute("""
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_name = 'appointments'
    """)
    for row in cur.fetchall():
        print(f"  {row['column_name']}: {row['data_type']}")
        
    # 2. Print recent appointments
    print("\n=== Recent appointments in DB ===")
    cur.execute("""
        SELECT id, technician_id, customer_name, service_type, start_time, end_time, status 
        FROM appointments 
        ORDER BY start_time DESC 
        LIMIT 10
    """)
    for row in cur.fetchall():
        print(f"  ID: {row['id']} | Tech: {row['technician_id']} | Cust: {row['customer_name']} | Svc: {row['service_type']} | Start: {row['start_time']} | End: {row['end_time']} | Status: {row['status']}")

except Exception as e:
    print("Error:", e)
finally:
    cur.close()
    conn.close()
