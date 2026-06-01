import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

# Get DB timezone setting
cur.execute("SHOW TIMEZONE;")
tz = cur.fetchone()
print("DB Timezone:", tz)

# Get table schema for appointments
cur.execute("""
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_name = 'appointments';
""")
print("\nAppointments schema:")
for col in cur.fetchall():
    print(col)

# Get some appointments
cur.execute("SELECT id, start_time, end_time, service_type, status FROM appointments LIMIT 5;")
print("\nSample appointments:")
for appt in cur.fetchall():
    print(appt)
conn.close()
