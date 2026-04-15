import os
import json
import bcrypt
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()


def get_db_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def create_tables():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            first_name VARCHAR(255),
            last_name VARCHAR(255),
            phone VARCHAR(50),
            is_admin BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS technicians (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            name VARCHAR(255),
            email VARCHAR(255),
            phone VARCHAR(50),
            skills JSONB,
            home_address VARCHAR(500),
            home_latitude DECIMAL(10, 8),
            home_longitude DECIMAL(11, 8),
            max_radius_miles INTEGER DEFAULT 20,
            calendar_provider VARCHAR(50),
            calendar_email VARCHAR(255),
            calendar_credentials JSONB,
            calendar_connected BOOLEAN DEFAULT FALSE,
            status VARCHAR(50) DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id SERIAL PRIMARY KEY,
            calendar_event_id VARCHAR(255),
            technician_id INTEGER REFERENCES technicians(id),
            customer_name VARCHAR(255),
            customer_phone VARCHAR(50),
            customer_email VARCHAR(255),
            service_type VARCHAR(255),
            address TEXT,
            latitude DECIMAL(10, 8),
            longitude DECIMAL(11, 8),
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            duration_minutes INTEGER DEFAULT 60,
            quoted_price DECIMAL(10, 2),
            discount_applied VARCHAR(100),
            status VARCHAR(50) DEFAULT 'scheduled',
            notes TEXT,
            reminder_sent BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointments_cache (
            id SERIAL PRIMARY KEY,
            ghl_appointment_id VARCHAR(255) UNIQUE,
            technician_id INTEGER REFERENCES technicians(id),
            customer_name VARCHAR(255),
            customer_phone VARCHAR(50),
            service_type VARCHAR(255),
            address TEXT,
            latitude DECIMAL(10, 8),
            longitude DECIMAL(11, 8),
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            status VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS route_cache (
            id SERIAL PRIMARY KEY,
            technician_id INTEGER REFERENCES technicians(id),
            date DATE,
            route_data JSONB,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id SERIAL PRIMARY KEY,
            call_id VARCHAR(255) UNIQUE NOT NULL,
            agent_id VARCHAR(255),
            call_type VARCHAR(50),
            direction VARCHAR(20),
            from_number VARCHAR(50),
            to_number VARCHAR(50),
            call_status VARCHAR(50),
            disconnection_reason VARCHAR(100),
            start_timestamp BIGINT,
            end_timestamp BIGINT,
            duration_seconds INTEGER,
            recording_url TEXT,
            transcript TEXT,
            transcript_object JSONB,
            call_analysis JSONB,
            metadata JSONB,
            dynamic_variables JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_calendar_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            provider VARCHAR(50),
            email VARCHAR(255),
            credentials JSONB,
            connected BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_scope_rules (
            id SERIAL PRIMARY KEY,
            service_type VARCHAR(100) UNIQUE NOT NULL,
            units_threshold INTEGER NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Seed defaults only if table is empty
    cur.execute("SELECT COUNT(*) FROM job_scope_rules")
    if cur.fetchone()[0] == 0:
        cur.execute("""
            INSERT INTO job_scope_rules (service_type, units_threshold) VALUES
            ('air_duct', 3),
            ('chimney', 4),
            ('power_washing', 2)
            ON CONFLICT (service_type) DO NOTHING
        """)

    _ensure_schema_migration(cur)

    conn.commit()
    cur.close()
    conn.close()

    _seed_admin_user()


def save_admin_calendar_credentials(provider, email, creds_dict):
    import json
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO admin_calendar_config (id, provider, email, credentials, connected, updated_at)
            VALUES (1, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO UPDATE SET
                provider = EXCLUDED.provider,
                email = EXCLUDED.email,
                credentials = EXCLUDED.credentials,
                connected = TRUE,
                updated_at = CURRENT_TIMESTAMP
        """, (provider, email, json.dumps(creds_dict)))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_admin_calendar_credentials():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM admin_calendar_config WHERE id = 1")
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close()
        conn.close()


def disconnect_admin_calendar():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE admin_calendar_config
            SET connected = FALSE, credentials = NULL, provider = NULL, email = NULL
            WHERE id = 1
        """)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _ensure_schema_migration(cur):
    columns_to_add = {
        "technicians": [
            ("user_id", "INTEGER REFERENCES users(id)"),
            ("calendar_provider", "VARCHAR(50)"),
            ("calendar_email", "VARCHAR(255)"),
            ("calendar_credentials", "JSONB"),
            ("calendar_connected", "BOOLEAN DEFAULT FALSE"),
            ("home_address", "VARCHAR(500)"),
            ("calendar_color_id", "VARCHAR(5) DEFAULT '7'"),
        ],
        "appointments": [
            ("quoted_price", "DECIMAL(10, 2)"),
            ("discount_applied", "VARCHAR(100)"),
        ],
    }
    for table, columns in columns_to_add.items():
        for col_name, col_type in columns:
            try:
                cur.execute(f"""
                    ALTER TABLE {table}
                    ADD COLUMN IF NOT EXISTS {col_name} {col_type}
                """)
            except Exception:
                pass


def _seed_admin_user():
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id FROM users WHERE is_admin = TRUE LIMIT 1")
        if cur.fetchone():
            cur.close()
            conn.close()
            return
        admin_email = os.getenv("ADMIN_EMAIL", "admin@unitedhomeservices.com")
        admin_password = os.getenv("ADMIN_PASSWORD")
        if not admin_password:
            logging.warning("ADMIN_PASSWORD not set in .env -- skipping admin seed")
            cur.close()
            conn.close()
            return
        hashed = bcrypt.hashpw(admin_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        cur.execute("""
            INSERT INTO users (username, email, password_hash, first_name, is_admin)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (email) DO NOTHING
        """, ("admin", admin_email, hashed, "Admin"))
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"Admin user seeded: {admin_email}")
    except Exception as e:
        logging.error(f"Error seeding admin: {e}")


def register_user(user_data):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (user_data["email"],))
        if cur.fetchone():
            raise ValueError("Email already registered")
        cur.execute("SELECT id FROM users WHERE username = %s", (user_data["username"],))
        if cur.fetchone():
            raise ValueError("Username already taken")
        hashed = bcrypt.hashpw(
            user_data["password"].encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")
        cur.execute("""
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s)
            RETURNING id, username, email, created_at
        """, (user_data["username"], user_data["email"], hashed))
        user = cur.fetchone()
        conn.commit()
        return dict(user) if user else None
    finally:
        cur.close()
        conn.close()


def login_user(user_data):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, username, email, password_hash, first_name, last_name,
                   phone, is_admin, is_active, created_at
            FROM users WHERE email = %s
        """, (user_data["email"],))
        user = cur.fetchone()
        if not user:
            return None
        if user.get("is_active") is False:
            return "deactivated"
        if not bcrypt.checkpw(
            user_data["password"].encode("utf-8"),
            user["password_hash"].encode("utf-8")
        ):
            return None
        user = dict(user)
        del user["password_hash"]
        return user
    finally:
        cur.close()
        conn.close()


def get_user_by_id(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, username, email, first_name, last_name,
                   phone, is_admin, created_at
            FROM users WHERE id = %s
        """, (user_id,))
        user = cur.fetchone()
        return dict(user) if user else None
    finally:
        cur.close()
        conn.close()


def get_all_users_paginated(page=1, page_size=20, search=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        offset = (page - 1) * page_size
        if search:
            search_pattern = f"%{search}%"
            cur.execute("""
                SELECT u.id, u.username, u.email, u.first_name, u.last_name,
                       u.phone, u.is_admin, u.is_active, u.created_at,
                       t.id as technician_id, t.skills, t.calendar_connected,
                       t.calendar_provider, t.calendar_email,
                       t.home_address, t.home_latitude, t.home_longitude, t.status as tech_status
                FROM users u
                LEFT JOIN technicians t ON t.user_id = u.id
                WHERE u.username ILIKE %s OR u.email ILIKE %s
                   OR u.first_name ILIKE %s OR u.last_name ILIKE %s
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, (search_pattern, search_pattern, search_pattern, search_pattern,
                  page_size, offset))
        else:
            cur.execute("""
                SELECT u.id, u.username, u.email, u.first_name, u.last_name,
                       u.phone, u.is_admin, u.is_active, u.created_at,
                       t.id as technician_id, t.skills, t.calendar_connected,
                       t.calendar_provider, t.calendar_email,
                       t.home_address, t.home_latitude, t.home_longitude, t.status as tech_status
                FROM users u
                LEFT JOIN technicians t ON t.user_id = u.id
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, (page_size, offset))
        users = cur.fetchall()

        count_query = "SELECT COUNT(*) FROM users"
        if search:
            count_query += " WHERE username ILIKE %s OR email ILIKE %s"
            cur.execute(count_query, (search_pattern, search_pattern))
        else:
            cur.execute(count_query)
        total = cur.fetchone()["count"]

        return {
            "users": [dict(u) for u in users],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    finally:
        cur.close()
        conn.close()


# Google Calendar color IDs — ordered for maximum visual contrast
_TECH_COLORS = ['9', '11', '10', '6', '3', '1', '4', '2', '7']


def _next_tech_color(cur):
    """Return the first color ID not yet assigned to any technician.

    Uses the open cursor (inside an active transaction) to check existing
    assignments. Cycles through _TECH_COLORS so no two techs share a color
    unless there are more techs than available colors (then wraps).
    """
    cur.execute("SELECT calendar_color_id FROM technicians WHERE calendar_color_id IS NOT NULL")
    used = {row["calendar_color_id"] for row in cur.fetchall()}
    for color in _TECH_COLORS:
        if color not in used:
            return color
    # All colors taken — fall back to cycling by count
    cur.execute("SELECT COUNT(*) AS count FROM technicians")
    count = cur.fetchone()["count"]
    return _TECH_COLORS[count % len(_TECH_COLORS)]


def create_user_by_admin(user_data, temp_password):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id FROM users WHERE email = %s", (user_data["email"],))
        if cur.fetchone():
            raise ValueError("Email already registered")

        # Auto-generate username from email, deduplicate with suffix if needed
        base_username = user_data["email"].split("@")[0].lower()
        username = base_username
        suffix = 2
        while True:
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if not cur.fetchone():
                break
            username = f"{base_username}_{suffix}"
            suffix += 1

        hashed = bcrypt.hashpw(
            temp_password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")
        cur.execute("""
            INSERT INTO users (username, email, password_hash, first_name, last_name, phone)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, username, email, first_name, last_name, phone, created_at
        """, (
            username,
            user_data["email"],
            hashed,
            user_data.get("first_name"),
            user_data.get("last_name"),
            user_data.get("phone")
        ))
        user = cur.fetchone()

        if user and user_data.get("skills"):
            raw_skills = user_data["skills"]
            normalized = []
            for s in raw_skills:
                parts = [p.strip().lower() for p in s.split(",") if p.strip()]
                normalized.extend(parts)
            skills_json = json.dumps(normalized)
            tech_name = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip() or username
            logging.info(f"[USER CREATE] Creating tech for user {user['id']} with skills: {normalized}")

            cur.execute("""
                INSERT INTO technicians
                (user_id, name, email, phone, skills, home_latitude, home_longitude,
                 home_address, max_radius_miles, status, calendar_color_id)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'active', %s)
                RETURNING id
            """, (
                user["id"],
                tech_name,
                user_data["email"],
                user_data.get("phone"),
                skills_json,
                user_data.get("home_latitude"),
                user_data.get("home_longitude"),
                user_data.get("home_address"),
                50,
                _next_tech_color(cur),
            ))
            tech_id = cur.fetchone()
            logging.info(f"[USER CREATE] Created technician id={tech_id['id'] if tech_id else 'unknown'} for user {user['id']}")

        conn.commit()
        return dict(user) if user else None
    finally:
        cur.close()
        conn.close()


def update_user_password(email, new_password):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        hashed = bcrypt.hashpw(
            new_password.encode("utf-8"),
            bcrypt.gensalt()
        ).decode("utf-8")
        cur.execute("""
            UPDATE users SET password_hash = %s WHERE email = %s
        """, (hashed, email))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def deactivate_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
        cur.execute("""
            UPDATE technicians SET status = 'inactive' WHERE user_id = %s
        """, (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def activate_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET is_active = TRUE WHERE id = %s", (user_id,))
        cur.execute("""
            UPDATE technicians SET status = 'active' WHERE user_id = %s
        """, (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def delete_user(user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM technicians WHERE user_id = %s", (user_id,))
        tech = cur.fetchone()
        if tech:
            tech_id = tech[0]
            cur.execute("""
                UPDATE appointments SET status = 'cancelled'
                WHERE technician_id = %s AND status = 'scheduled'
                AND start_time > CURRENT_TIMESTAMP
            """, (tech_id,))
            cur.execute("DELETE FROM appointments WHERE technician_id = %s", (tech_id,))
            cur.execute("DELETE FROM technicians WHERE id = %s", (tech_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Delete user error: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def get_user_detail_with_calendar(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT u.id, u.username, u.email, u.first_name, u.last_name,
                   u.phone, u.is_admin, u.created_at,
                   t.id as technician_id, t.skills, t.calendar_connected,
                   t.calendar_provider, t.calendar_email,
                   t.home_address, t.home_latitude, t.home_longitude
            FROM users u
            LEFT JOIN technicians t ON t.user_id = u.id
            WHERE u.id = %s
        """, (user_id,))
        user = cur.fetchone()
        return dict(user) if user else None
    finally:
        cur.close()
        conn.close()


def update_user(user_id, updates):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        user_fields = {}
        tech_fields = {}
        for key in ["first_name", "last_name", "phone"]:
            if key in updates and updates[key] is not None:
                user_fields[key] = updates[key]
        if "skills" in updates:
            # Normalize skills: split comma-separated strings, trim, lowercase
            raw_skills = updates["skills"]
            normalized = []
            for s in raw_skills:
                parts = [p.strip().lower() for p in s.split(",") if p.strip()]
                normalized.extend(parts)
            tech_fields["skills"] = json.dumps(normalized)

        # Handle address update: geocode and store in technicians table
        if "address" in updates and updates["address"]:
            tech_fields["home_address"] = updates["address"]
            try:
                from src.utils.radar import geocode_address
                geo = geocode_address(updates["address"])
                if geo and geo.get("latitude") and geo.get("longitude"):
                    tech_fields["home_latitude"] = geo["latitude"]
                    tech_fields["home_longitude"] = geo["longitude"]
                    # Use the formatted address from geocoder if available
                    if geo.get("formatted_address"):
                        tech_fields["home_address"] = geo["formatted_address"]
                    logging.info(
                        "[USER UPDATE] Geocoded address for user %s: %s -> (%s, %s)",
                        user_id, updates["address"],
                        tech_fields["home_latitude"], tech_fields["home_longitude"],
                    )
                else:
                    logging.warning(
                        "[USER UPDATE] Geocoding failed for user %s address: %s",
                        user_id, updates["address"],
                    )
            except Exception as e:
                logging.error("[USER UPDATE] Geocode error for user %s: %s", user_id, e)

        if user_fields:
            set_clause = ", ".join(f"{k} = %s" for k in user_fields)
            values = list(user_fields.values()) + [user_id]
            cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)

        # If tech fields were provided (skills or address), upsert the technician row
        if tech_fields:
            cur.execute("SELECT id FROM technicians WHERE user_id = %s", (user_id,))
            tech = cur.fetchone()
            if tech:
                # Update existing tech row
                set_clause = ", ".join(f"{k} = %s" for k in tech_fields)
                values = list(tech_fields.values()) + [user_id]
                # Handle jsonb casting for skills
                set_parts = []
                vals = []
                for k, v in tech_fields.items():
                    if k == "skills":
                        set_parts.append(f"{k} = %s::jsonb")
                    else:
                        set_parts.append(f"{k} = %s")
                    vals.append(v)
                vals.append(user_id)
                cur.execute(
                    f"UPDATE technicians SET {', '.join(set_parts)} WHERE user_id = %s",
                    vals,
                )
                logging.info("[USER UPDATE] Updated tech fields for user %s: %s", user_id, list(tech_fields.keys()))
            else:
                # No tech row exists - create one
                cur.execute(
                    "SELECT username, email, first_name, last_name, phone FROM users WHERE id = %s",
                    (user_id,)
                )
                user_row = cur.fetchone()
                if user_row:
                    name = f"{user_row.get('first_name', '') or ''} {user_row.get('last_name', '') or ''}".strip()
                    name = name or user_row["username"]
                    cur.execute("""
                        INSERT INTO technicians
                        (user_id, name, email, phone, skills, home_address,
                         home_latitude, home_longitude, max_radius_miles, status)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'active')
                    """, (
                        user_id, name, user_row["email"], user_row.get("phone"),
                        tech_fields.get("skills", "[]"),
                        tech_fields.get("home_address"),
                        tech_fields.get("home_latitude"),
                        tech_fields.get("home_longitude"),
                        50,
                    ))
                    logging.info("[USER UPDATE] Created new tech row for user %s", user_id)

        # Sync name/phone changes to technicians table
        if user_fields and any(k in user_fields for k in ["first_name", "last_name", "phone"]):
            cur.execute("SELECT id FROM technicians WHERE user_id = %s", (user_id,))
            tech = cur.fetchone()
            if tech:
                sync_updates = {}
                if "first_name" in user_fields or "last_name" in user_fields:
                    cur.execute(
                        "SELECT first_name, last_name, username FROM users WHERE id = %s",
                        (user_id,)
                    )
                    u = cur.fetchone()
                    if u:
                        name = f"{u.get('first_name', '') or ''} {u.get('last_name', '') or ''}".strip()
                        sync_updates["name"] = name or u["username"]
                if "phone" in user_fields:
                    sync_updates["phone"] = user_fields["phone"]
                if sync_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in sync_updates)
                    values = list(sync_updates.values()) + [user_id]
                    cur.execute(f"UPDATE technicians SET {set_clause} WHERE user_id = %s", values)

        conn.commit()
        return get_user_detail_with_calendar(user_id)
    finally:
        cur.close()
        conn.close()


def save_calendar_credentials(tech_id, provider, email, credentials):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE technicians
            SET calendar_provider = %s,
                calendar_email = %s,
                calendar_credentials = %s::jsonb,
                calendar_connected = TRUE
            WHERE id = %s
        """, (provider, email, json.dumps(credentials), tech_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def get_calendar_credentials(tech_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT calendar_provider, calendar_email, calendar_credentials, calendar_connected
            FROM technicians WHERE id = %s
        """, (tech_id,))
        result = cur.fetchone()
        return dict(result) if result else None
    finally:
        cur.close()
        conn.close()


def disconnect_calendar(tech_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE technicians
            SET calendar_provider = NULL,
                calendar_email = NULL,
                calendar_credentials = NULL,
                calendar_connected = FALSE
            WHERE id = %s
        """, (tech_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def create_appointment(appointment_data):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO appointments
            (calendar_event_id, technician_id, customer_name, customer_phone,
             customer_email, service_type, address, latitude, longitude,
             start_time, end_time, duration_minutes, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            appointment_data.get("calendar_event_id"),
            appointment_data["technician_id"],
            appointment_data["customer_name"],
            appointment_data.get("customer_phone"),
            appointment_data.get("customer_email"),
            appointment_data["service_type"],
            appointment_data.get("address"),
            appointment_data.get("latitude"),
            appointment_data.get("longitude"),
            appointment_data["start_time"],
            appointment_data["end_time"],
            appointment_data.get("duration_minutes", 60),
            appointment_data.get("status", "scheduled"),
            appointment_data.get("notes")
        ))
        appt = cur.fetchone()
        conn.commit()
        return dict(appt) if appt else None
    finally:
        cur.close()
        conn.close()


def get_appointments_paginated(page=1, page_size=20, technician_id=None,
                                status_filter=None, date_from=None, date_to=None,
                                search=None, time_filter=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        offset = (page - 1) * page_size
        conditions = []
        params = []

        if technician_id:
            conditions.append("a.technician_id = %s")
            params.append(technician_id)
        if status_filter:
            conditions.append("a.status = %s")
            params.append(status_filter)
        if date_from:
            conditions.append("a.start_time >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("a.start_time <= %s")
            params.append(date_to)
        if search:
            conditions.append(
                "(a.customer_name ILIKE %s OR a.customer_phone ILIKE %s OR a.customer_email ILIKE %s)"
            )
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        if time_filter == "upcoming":
            conditions.append("a.start_time > NOW()")
        elif time_filter == "past":
            conditions.append("a.start_time <= NOW()")

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        cur.execute(f"""
            SELECT a.*, t.name as technician_name
            FROM appointments a
            LEFT JOIN technicians t ON a.technician_id = t.id
            WHERE {where_clause}
            ORDER BY a.start_time DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        appointments = cur.fetchall()

        cur.execute(f"""
            SELECT COUNT(*) FROM appointments a WHERE {where_clause}
        """, params)
        total = cur.fetchone()["count"]

        return {
            "appointments": [dict(a) for a in appointments],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    finally:
        cur.close()
        conn.close()


def get_appointment_by_id(appointment_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT a.*, t.name as technician_name
            FROM appointments a
            LEFT JOIN technicians t ON a.technician_id = t.id
            WHERE a.id = %s
        """, (appointment_id,))
        appt = cur.fetchone()
        return dict(appt) if appt else None
    finally:
        cur.close()
        conn.close()


def update_appointment_status(appointment_id, status):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE appointments
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (status, appointment_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def get_appointment_stats():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'scheduled') as scheduled,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
                COUNT(*) FILTER (WHERE status = 'no_show') as no_show,
                COUNT(*) FILTER (WHERE start_time > NOW() AND status = 'scheduled') as upcoming
            FROM appointments
        """)
        return dict(cur.fetchone())
    finally:
        cur.close()
        conn.close()


def get_pending_reminders(hours_before=2):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT a.*, t.name as technician_name
            FROM appointments a
            LEFT JOIN technicians t ON a.technician_id = t.id
            WHERE a.status = 'scheduled'
              AND a.reminder_sent = FALSE
              AND a.start_time BETWEEN NOW() AND NOW() + INTERVAL '%s hours'
        """, (hours_before,))
        return [dict(a) for a in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def mark_reminder_sent(appointment_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE appointments SET reminder_sent = TRUE WHERE id = %s
        """, (appointment_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_technician(tech_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM technicians WHERE id = %s", (tech_id,))
        tech = cur.fetchone()
        return dict(tech) if tech else None
    finally:
        cur.close()
        conn.close()


def get_technician_by_user_id(user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM technicians WHERE user_id = %s", (user_id,))
        tech = cur.fetchone()
        return dict(tech) if tech else None
    finally:
        cur.close()
        conn.close()


def get_techs_with_skill(service_type):
    """Find technicians with a matching skill. Uses fuzzy matching to handle
    variations like 'chimney', 'chimney cleaning', 'Chimney Cleaning', etc."""
    import logging

    # Normalize service type to lowercase keyword
    service_lower = service_type.lower().strip()

    # Map common variations to canonical service keywords
    service_keywords = {
        "chimney": ["chimney"],
        "chimney cleaning": ["chimney"],
        "dryer_vent": ["dryer", "vent"],
        "dryer vent": ["dryer", "vent"],
        "dryer vent cleaning": ["dryer", "vent"],
        "gutter": ["gutter"],
        "gutter cleaning": ["gutter"],
        "power_washing": ["power", "wash", "pressure"],
        "power washing": ["power", "wash", "pressure"],
        "pressure washing": ["power", "wash", "pressure"],
        "air_duct": ["duct", "air"],
        "air duct": ["duct", "air"],
        "air duct cleaning": ["duct", "air"],
        "duct cleaning": ["duct", "air"],
    }

    # Get the keywords to match against
    match_keywords = service_keywords.get(service_lower, [service_lower.split()[0] if service_lower else service_lower])

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # First try: exact JSONB match
        cur.execute("""
            SELECT * FROM technicians
            WHERE status = 'active'
            AND skills @> %s::jsonb
        """, (f'["{service_type}"]',))
        results = [dict(tech) for tech in cur.fetchall()]

        if results:
            logging.info(f"[SKILL MATCH] Exact match for '{service_type}': found {len(results)} techs: {[t['name'] for t in results]}")
            return results

        # Second try: case-insensitive text search in skills array
        keyword_conditions = " OR ".join(["skills::text ILIKE %s" for _ in match_keywords])
        keyword_params = [f'%{kw}%' for kw in match_keywords]

        cur.execute(f"""
            SELECT * FROM technicians
            WHERE status = 'active'
            AND ({keyword_conditions})
        """, keyword_params)
        results = [dict(tech) for tech in cur.fetchall()]

        if results:
            logging.info(f"[SKILL MATCH] Fuzzy match for '{service_type}' (keywords: {match_keywords}): found {len(results)} techs: {[t['name'] for t in results]}")
            return results

        # Fallback: return ALL active techs (better to suggest someone than no one)
        logging.warning(f"[SKILL MATCH] No skill match for '{service_type}'. Falling back to ALL active techs.")
        cur.execute("SELECT * FROM technicians WHERE status = 'active'")
        results = [dict(tech) for tech in cur.fetchall()]
        logging.info(f"[SKILL MATCH] Fallback: returning {len(results)} active techs: {[t['name'] for t in results]}")
        return results
    finally:
        cur.close()
        conn.close()



def get_techs_with_appointments_for_day(service_type, date):
    service_lower = service_type.lower().strip()

    service_keywords = {
        "chimney": ["chimney"],
        "chimney cleaning": ["chimney"],
        "dryer_vent": ["dryer", "vent"],
        "dryer vent": ["dryer", "vent"],
        "dryer vent cleaning": ["dryer", "vent"],
        "gutter": ["gutter"],
        "gutter cleaning": ["gutter"],
        "power_washing": ["power", "wash", "pressure"],
        "power washing": ["power", "wash", "pressure"],
        "pressure washing": ["power", "wash", "pressure"],
        "air_duct": ["duct", "air"],
        "air duct": ["duct", "air"],
        "air duct cleaning": ["duct", "air"],
        "duct cleaning": ["duct", "air"],
    }

    match_keywords = service_keywords.get(
        service_lower,
        [service_lower.split()[0] if service_lower else service_lower],
    )

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        keyword_conditions = " OR ".join(
            ["t.skills::text ILIKE %s" for _ in match_keywords]
        )
        keyword_params = [f"%{kw}%" for kw in match_keywords]

        cur.execute(
            f"""
            SELECT
                t.id, t.name, t.email, t.phone, t.skills,
                t.home_address, t.home_latitude, t.home_longitude,
                t.max_radius_miles, t.status,
                t.calendar_provider, t.calendar_email, t.calendar_connected,
                a.id          AS appt_id,
                a.start_time  AS appt_start_time,
                a.end_time    AS appt_end_time,
                a.latitude    AS appt_latitude,
                a.longitude   AS appt_longitude
            FROM technicians t
            LEFT JOIN appointments a
                ON a.technician_id = t.id
               AND DATE(a.start_time) = %s
               AND a.status = 'scheduled'
            WHERE t.status = 'active'
              AND (
                  t.skills @> %s::jsonb
                  OR ({keyword_conditions})
              )
            ORDER BY t.id, a.start_time
            """,
            [date, f'["{service_type}"]'] + keyword_params,
        )
        rows = cur.fetchall()

        if not rows:
            logging.warning(
                "[SKILL MATCH] No skill match for '%s'. Falling back to all active techs.",
                service_type,
            )
            cur.execute(
                """
                SELECT
                    t.id, t.name, t.email, t.phone, t.skills,
                    t.home_address, t.home_latitude, t.home_longitude,
                    t.max_radius_miles, t.status,
                    t.calendar_provider, t.calendar_email, t.calendar_connected,
                    a.id          AS appt_id,
                    a.start_time  AS appt_start_time,
                    a.end_time    AS appt_end_time,
                    a.latitude    AS appt_latitude,
                    a.longitude   AS appt_longitude
                FROM technicians t
                LEFT JOIN appointments a
                    ON a.technician_id = t.id
                   AND DATE(a.start_time) = %s
                   AND a.status = 'scheduled'
                WHERE t.status = 'active'
                ORDER BY t.id, a.start_time
                """,
                (date,),
            )
            rows = cur.fetchall()

        tech_map = {}
        for row in rows:
            row = dict(row)
            tid = row["id"]
            if tid not in tech_map:
                tech_map[tid] = {k: v for k, v in row.items() if not k.startswith("appt_")}
                tech_map[tid]["appointments"] = []
            if row["appt_id"] is not None:
                tech_map[tid]["appointments"].append({
                    "start_time": row["appt_start_time"],
                    "end_time": row["appt_end_time"],
                    "latitude": row["appt_latitude"],
                    "longitude": row["appt_longitude"],
                })

        logging.info(
            "[SKILL MATCH] Single-query found %d techs for '%s'", len(tech_map), service_type
        )
        return list(tech_map.values())
    finally:
        cur.close()
        conn.close()


def get_tech_appointments_for_day(tech_id, date):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT * FROM appointments
            WHERE technician_id = %s
            AND DATE(start_time) = %s
            ORDER BY start_time
        """, (tech_id, date))
        return [dict(appt) for appt in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def insert_appointment(calendar_event_id, technician_id, customer_name,
                       customer_phone, customer_email, service_type, address,
                       latitude, longitude, start_time, end_time,
                       duration_minutes, status, quoted_price=None,
                       discount_applied=None, notes=None):
    """Insert a new appointment into the MAIN appointments table."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO appointments
            (calendar_event_id, technician_id, customer_name, customer_phone,
             customer_email, service_type, address, latitude, longitude,
             start_time, end_time, duration_minutes, quoted_price,
             discount_applied, status, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (calendar_event_id, technician_id, customer_name, customer_phone,
              customer_email, service_type, address, latitude, longitude,
              start_time, end_time, duration_minutes, quoted_price,
              discount_applied, status, notes))
        result = cur.fetchone()
        conn.commit()
        logging.info(f"[DB] Inserted appointment id={result[0] if result else 'unknown'} price={quoted_price} discount={discount_applied}")
        return result[0] if result else None
    except Exception as e:
        conn.rollback()
        logging.error(f"[DB] Failed to insert appointment: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def insert_appointment_cache(ghl_appointment_id, technician_id, customer_name,
                            customer_phone, service_type, address, latitude,
                            longitude, start_time, end_time, status):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO appointments_cache
            (ghl_appointment_id, technician_id, customer_name, customer_phone,
             service_type, address, latitude, longitude, start_time, end_time, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ghl_appointment_id) DO UPDATE SET
            technician_id = EXCLUDED.technician_id,
            customer_name = EXCLUDED.customer_name,
            customer_phone = EXCLUDED.customer_phone,
            service_type = EXCLUDED.service_type,
            address = EXCLUDED.address,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            status = EXCLUDED.status
        """, (ghl_appointment_id, technician_id, customer_name, customer_phone,
              service_type, address, latitude, longitude, start_time, end_time, status))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_appointment_cache(ghl_appointment_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM appointments_cache WHERE ghl_appointment_id = %s",
                    (ghl_appointment_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_route_cache(tech_id, date):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM route_cache
            WHERE technician_id = %s AND date = %s
        """, (tech_id, date))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def insert_technician(name, email, phone, skills, home_latitude, home_longitude,
                     ghl_user_id=None, ghl_calendar_id=None, user_id=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO technicians
            (name, email, phone, skills, home_latitude, home_longitude,
             ghl_user_id, ghl_calendar_id, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (name, email, phone, skills, home_latitude, home_longitude,
              ghl_user_id, ghl_calendar_id, user_id))
        tech = cur.fetchone()
        conn.commit()
        return dict(tech) if tech else None
    finally:
        cur.close()
        conn.close()


def get_all_technicians():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM technicians WHERE status = 'active'")
        return [dict(tech) for tech in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def get_technician_by_ghl_user_id(ghl_user_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM technicians WHERE ghl_user_id = %s", (ghl_user_id,))
        tech = cur.fetchone()
        return dict(tech) if tech else None
    finally:
        cur.close()
        conn.close()


def upsert_technician_from_ghl(ghl_user_id, ghl_calendar_id, name, email, phone,
                               skills=None, home_latitude=None, home_longitude=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO technicians
            (ghl_user_id, ghl_calendar_id, name, email, phone, skills, home_latitude, home_longitude)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ghl_user_id) DO UPDATE SET
            ghl_calendar_id = EXCLUDED.ghl_calendar_id,
            name = EXCLUDED.name,
            email = EXCLUDED.email,
            phone = EXCLUDED.phone,
            skills = COALESCE(EXCLUDED.skills, technicians.skills),
            home_latitude = COALESCE(EXCLUDED.home_latitude, technicians.home_latitude),
            home_longitude = COALESCE(EXCLUDED.home_longitude, technicians.home_longitude)
            RETURNING *
        """, (ghl_user_id, ghl_calendar_id, name, email, phone, skills, home_latitude, home_longitude))
        tech = cur.fetchone()
        conn.commit()
        return dict(tech) if tech else None
    finally:
        cur.close()
        conn.close()


def upsert_call_log(call_data):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        duration = None
        if call_data.get("start_timestamp") and call_data.get("end_timestamp"):
            duration = int((call_data["end_timestamp"] - call_data["start_timestamp"]) / 1000)

        cur.execute("""
            INSERT INTO call_logs
            (call_id, agent_id, call_type, direction, from_number, to_number,
             call_status, disconnection_reason, start_timestamp, end_timestamp,
             duration_seconds, recording_url, transcript, transcript_object,
             call_analysis, metadata, dynamic_variables)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (call_id) DO UPDATE SET
                call_status = EXCLUDED.call_status,
                disconnection_reason = COALESCE(EXCLUDED.disconnection_reason, call_logs.disconnection_reason),
                end_timestamp = COALESCE(EXCLUDED.end_timestamp, call_logs.end_timestamp),
                duration_seconds = COALESCE(EXCLUDED.duration_seconds, call_logs.duration_seconds),
                recording_url = COALESCE(EXCLUDED.recording_url, call_logs.recording_url),
                transcript = COALESCE(EXCLUDED.transcript, call_logs.transcript),
                transcript_object = COALESCE(EXCLUDED.transcript_object, call_logs.transcript_object),
                call_analysis = COALESCE(EXCLUDED.call_analysis, call_logs.call_analysis)
            RETURNING *
        """, (
            call_data["call_id"],
            call_data.get("agent_id"),
            call_data.get("call_type"),
            call_data.get("direction"),
            call_data.get("from_number"),
            call_data.get("to_number"),
            call_data.get("call_status"),
            call_data.get("disconnection_reason"),
            call_data.get("start_timestamp"),
            call_data.get("end_timestamp"),
            duration,
            call_data.get("recording_url"),
            call_data.get("transcript"),
            json.dumps(call_data.get("transcript_object")) if call_data.get("transcript_object") else None,
            json.dumps(call_data.get("call_analysis")) if call_data.get("call_analysis") else None,
            json.dumps(call_data.get("metadata")) if call_data.get("metadata") else None,
            json.dumps(call_data.get("retell_llm_dynamic_variables")) if call_data.get("retell_llm_dynamic_variables") else None
        ))
        result = cur.fetchone()
        conn.commit()
        return dict(result) if result else None
    finally:
        cur.close()
        conn.close()


def get_call_logs_paginated(page=1, page_size=20, direction=None,
                             call_status=None, date_from=None, date_to=None,
                             search=None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        offset = (page - 1) * page_size
        conditions = []
        params = []

        if direction:
            conditions.append("direction = %s")
            params.append(direction)
        if call_status:
            conditions.append("call_status = %s")
            params.append(call_status)
        if date_from:
            conditions.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= %s")
            params.append(date_to)
        if search:
            conditions.append("(from_number ILIKE %s OR to_number ILIKE %s OR transcript ILIKE %s)")
            s = f"%{search}%"
            params.extend([s, s, s])

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        cur.execute(f"""
            SELECT * FROM call_logs {where}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, params + [page_size, offset])
        logs = cur.fetchall()

        cur.execute(f"SELECT COUNT(*) FROM call_logs {where}", params)
        total = cur.fetchone()["count"]

        return {
            "logs": [dict(l) for l in logs],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    finally:
        cur.close()
        conn.close()


def get_call_log_by_call_id(call_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM call_logs WHERE call_id = %s", (call_id,))
        log = cur.fetchone()
        return dict(log) if log else None
    finally:
        cur.close()
        conn.close()


def get_call_stats():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE direction = 'inbound') as inbound,
                COUNT(*) FILTER (WHERE direction = 'outbound') as outbound,
                COUNT(*) FILTER (WHERE disconnection_reason = 'user_hangup') as user_hangup,
                COUNT(*) FILTER (WHERE disconnection_reason = 'agent_hangup') as agent_hangup,
                COUNT(*) FILTER (WHERE disconnection_reason LIKE 'dial_%') as failed,
                COALESCE(AVG(duration_seconds) FILTER (WHERE duration_seconds > 0), 0) as avg_duration_seconds,
                COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE) as today,
                COUNT(*) FILTER (WHERE created_at >= CURRENT_DATE - INTERVAL '7 days') as last_7_days
            FROM call_logs
        """)
        stats = cur.fetchone()
        return dict(stats) if stats else {}
    finally:
        cur.close()
        conn.close()


def get_job_scope_rules():
    """Return all job scope rules as a dict keyed by service_type."""
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT service_type, units_threshold, updated_at FROM job_scope_rules ORDER BY service_type")
        rows = cur.fetchall()
        return {r["service_type"]: dict(r) for r in rows}
    finally:
        cur.close()
        conn.close()


def update_job_scope_rule(service_type: str, units_threshold: int):
    """Insert or update a job scope rule threshold."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO job_scope_rules (service_type, units_threshold, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (service_type) DO UPDATE SET
                units_threshold = EXCLUDED.units_threshold,
                updated_at = CURRENT_TIMESTAMP
        """, (service_type, units_threshold))
        conn.commit()
    finally:
        cur.close()
        conn.close()
