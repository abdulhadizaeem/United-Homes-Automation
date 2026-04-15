import os
import logging
import traceback
import secrets
import string
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

load_dotenv()
from src.utils.auth import require_admin
from src.utils.db import (
    get_all_users_paginated,
    create_user_by_admin,
    get_user_detail_with_calendar,
    update_user,
    get_appointments_paginated,
    get_appointment_stats,
    deactivate_user,
    activate_user,
    delete_user
)
from src.api.models import CreateUserRequest, UpdateUserRequest

router = APIRouter()


def _generate_temp_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(None),
    current_user: dict = Depends(require_admin)
):
    try:
        result = get_all_users_paginated(page, page_size, search)
        users_out = []
        for u in result["users"]:
            skills = u.get("skills")
            if isinstance(skills, str):
                import json
                skills = json.loads(skills)
            users_out.append({
                "id": u["id"],
                "username": u["username"],
                "email": u["email"],
                "first_name": u.get("first_name"),
                "last_name": u.get("last_name"),
                "phone": u.get("phone"),
                "is_admin": u.get("is_admin", False),
                "is_active": u.get("is_active", True),
                "skills": skills,
                "address": {
                    "formatted_address": u.get("home_address"),
                    "latitude": float(u["home_latitude"]) if u.get("home_latitude") else None,
                    "longitude": float(u["home_longitude"]) if u.get("home_longitude") else None
                },
                "calendar": {
                    "connected": u.get("calendar_connected", False),
                    "provider": u.get("calendar_provider"),
                    "email": u.get("calendar_email")
                },
                "created_at": str(u.get("created_at", ""))
            })
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": users_out,
                "pagination": {
                    "total": result["total"],
                    "page": result["page"],
                    "page_size": result["page_size"],
                    "total_pages": result["total_pages"]
                }
            }
        )
    except Exception as e:
        logging.error(f"List users error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch users")


@router.post("/users/create")
async def create_user(
    request: CreateUserRequest,
    current_user: dict = Depends(require_admin)
):
    try:
        home_latitude = None
        home_longitude = None
        home_address = request.address  # fallback to raw input
        if request.address:
            from src.utils.radar import geocode_address
            geo_result = geocode_address(request.address)
            if geo_result:
                home_latitude = geo_result["latitude"]
                home_longitude = geo_result["longitude"]
                home_address = geo_result.get("formatted_address", request.address)

        temp_password = _generate_temp_password()
        user = create_user_by_admin(
            {
                "email": request.email.lower().strip(),
                "first_name": request.first_name,
                "last_name": request.last_name,
                "phone": request.phone,
                "address": request.address,
                "skills": request.skills,
                "home_latitude": home_latitude,
                "home_longitude": home_longitude,
                "home_address": home_address,
            },
            temp_password
        )
        if not user:
            raise HTTPException(status_code=400, detail="Failed to create user")

        try:
            from src.utils.mail_service import send_welcome_email
            frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
            send_welcome_email(
                user_email=request.email,
                user_name=request.username,
                temp_password=temp_password,
                login_url=f"{frontend_url}/signin"
            )
        except Exception as mail_err:
            logging.error(f"Welcome email failed: {mail_err}")

        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "message": "User created and welcome email sent",
                "data": {
                    "id": user["id"],
                    "username": user["username"],
                    "email": user["email"]
                }
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Create user error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to create user")


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: int,
    current_user: dict = Depends(require_admin)
):
    try:
        user = get_user_detail_with_calendar(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        skills = user.get("skills")
        if isinstance(skills, str):
            import json
            skills = json.loads(skills)

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": {
                    "id": user["id"],
                    "username": user["username"],
                    "email": user["email"],
                    "first_name": user.get("first_name"),
                    "last_name": user.get("last_name"),
                    "phone": user.get("phone"),
                    "is_admin": user.get("is_admin", False),
                    "skills": skills,
                    "technician_id": user.get("technician_id"),
                    "address": {
                        "formatted_address": user.get("home_address"),
                        "latitude": float(user["home_latitude"]) if user.get("home_latitude") else None,
                        "longitude": float(user["home_longitude"]) if user.get("home_longitude") else None
                    },
                    "calendar": {
                        "connected": user.get("calendar_connected", False),
                        "provider": user.get("calendar_provider"),
                        "email": user.get("calendar_email")
                    },
                    "created_at": str(user.get("created_at", ""))
                }
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Get user detail error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch user")


@router.put("/users/{user_id}")
async def update_user_endpoint(
    user_id: int,
    request: UpdateUserRequest,
    current_user: dict = Depends(require_admin)
):
    try:
        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        user = update_user(user_id, updates)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "User updated"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Update user error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to update user")


@router.get("/users/{user_id}/appointments")
async def get_user_appointments(
    user_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str = Query(None),
    time_filter: str = Query(None),
    current_user: dict = Depends(require_admin)
):
    try:
        user = get_user_detail_with_calendar(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        tech_id = user.get("technician_id")
        if not tech_id:
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "data": [],
                    "pagination": {"total": 0, "page": 1, "page_size": page_size, "total_pages": 0}
                }
            )
        result = get_appointments_paginated(
            page=page,
            page_size=page_size,
            technician_id=tech_id,
            status_filter=status_filter,
            time_filter=time_filter
        )
        appointments_out = []
        for a in result["appointments"]:
            appointments_out.append({
                "id": a["id"],
                "customer_name": a["customer_name"],
                "customer_phone": a.get("customer_phone"),
                "customer_email": a.get("customer_email"),
                "service_type": a["service_type"],
                "address": a.get("address"),
                "start_time": str(a["start_time"]),
                "end_time": str(a["end_time"]),
                "status": a["status"],
                "technician_name": a.get("technician_name"),
                "created_at": str(a.get("created_at", ""))
            })
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "data": appointments_out,
                "pagination": {
                    "total": result["total"],
                    "page": result["page"],
                    "page_size": result["page_size"],
                    "total_pages": result["total_pages"]
                }
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Get user appointments error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch appointments")


@router.get("/stats")
async def dashboard_stats(current_user: dict = Depends(require_admin)):
    try:
        stats = get_appointment_stats()
        return JSONResponse(
            status_code=200,
            content={"success": True, "data": stats}
        )
    except Exception as e:
        logging.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stats")


@router.post("/users/{user_id}/deactivate")
async def deactivate_user_endpoint(
    user_id: int,
    current_user: dict = Depends(require_admin)
):
    try:
        if user_id == current_user["id"]:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        user = get_user_detail_with_calendar(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        deactivate_user(user_id)
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "User deactivated"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Deactivate user error: {e}")
        raise HTTPException(status_code=500, detail="Failed to deactivate user")


@router.post("/users/{user_id}/activate")
async def activate_user_endpoint(
    user_id: int,
    current_user: dict = Depends(require_admin)
):
    try:
        user = get_user_detail_with_calendar(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        activate_user(user_id)
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "User activated"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Activate user error: {e}")
        raise HTTPException(status_code=500, detail="Failed to activate user")


@router.delete("/users/{user_id}")
async def delete_user_endpoint(
    user_id: int,
    current_user: dict = Depends(require_admin)
):
    try:
        if user_id == current_user["id"]:
            raise HTTPException(status_code=400, detail="Cannot delete yourself")
        user = get_user_detail_with_calendar(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.get("is_admin"):
            raise HTTPException(status_code=400, detail="Cannot delete admin users")
        success = delete_user(user_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete user")
        return JSONResponse(
            status_code=200,
            content={"success": True, "message": "User permanently deleted"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Delete user error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete user")


# ---------------------------------------------------------------------------
# Admin calendar endpoints
# ---------------------------------------------------------------------------

# No global state store needed for stateless OAuth


@router.get("/calendar/google/connect")
async def admin_calendar_google_connect(current_user: dict = Depends(require_admin)):
    import uuid
    from fastapi.responses import JSONResponse
    from google_auth_oauthlib.flow import Flow

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    # Bulletproof fallback in case .env doesn't reload properly
    tech_uri = os.getenv("GOOGLE_REDIRECT_URI", "")
    smart_fallback = tech_uri.replace("/api/calendar/", "/api/admin/calendar/") if tech_uri else ""
    redirect_uri = os.getenv("ADMIN_GOOGLE_REDIRECT_URI") or smart_fallback
    
    scopes = ["https://www.googleapis.com/auth/calendar"]

    flow = Flow.from_client_config(
        {"web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [redirect_uri],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=scopes,
        redirect_uri=redirect_uri,
    )
    from src.utils.jwt_utils import create_oauth_state_token
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=create_oauth_state_token({"admin_id": current_user["id"]})
    )
    return JSONResponse(status_code=200, content={"success": True, "auth_url": auth_url})


@router.get("/calendar/google/callback")
async def admin_calendar_google_callback(
    code: str,
    state: str,
):
    from fastapi.responses import RedirectResponse
    from google_auth_oauthlib.flow import Flow
    from src.utils.db import save_admin_calendar_credentials

    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    
    # Bulletproof fallback
    tech_uri = os.getenv("GOOGLE_REDIRECT_URI", "")
    smart_fallback = tech_uri.replace("/api/calendar/", "/api/admin/calendar/") if tech_uri else ""
    redirect_uri = os.getenv("ADMIN_GOOGLE_REDIRECT_URI") or smart_fallback
    
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    if not frontend_url.startswith("http"):
        frontend_url = f"https://{frontend_url}"
    scopes = ["https://www.googleapis.com/auth/calendar"]

    from src.utils.jwt_utils import verify_oauth_state_token

    state_data = verify_oauth_state_token(state)
    if not state_data:
        return RedirectResponse(url=f"{frontend_url}/dashboard?calendar_status=error&reason=invalid_state")

    try:
        flow = Flow.from_client_config(
            {"web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": [redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }},
            scopes=scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        credentials = flow.credentials

        calendar_email = ""
        try:
            from googleapiclient.discovery import build
            svc = build("oauth2", "v2", credentials=credentials)
            calendar_email = svc.userinfo().get().execute().get("email", "")
        except Exception:
            pass

        creds_dict = {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            "scopes": list(credentials.scopes) if credentials.scopes else scopes,
        }
        save_admin_calendar_credentials("google", calendar_email, creds_dict)
        return RedirectResponse(url=f"{frontend_url}/dashboard?calendar_status=connected")
    except Exception as e:
        logging.error(f"Admin calendar callback error: {e}")
        traceback.print_exc()
        return RedirectResponse(url=f"{frontend_url}/dashboard?calendar_status=error")


@router.get("/calendar/status")
async def admin_calendar_status(current_user: dict = Depends(require_admin)):
    from src.utils.db import get_admin_calendar_credentials
    creds = get_admin_calendar_credentials()
    return JSONResponse(status_code=200, content={
        "success": True,
        "data": {
            "connected": bool(creds and creds.get("connected")),
            "provider": creds.get("provider") if creds else None,
            "email": creds.get("email") if creds else None,
        }
    })


@router.post("/calendar/disconnect")
async def admin_calendar_disconnect(current_user: dict = Depends(require_admin)):
    from src.utils.db import disconnect_admin_calendar
    disconnect_admin_calendar()
    return JSONResponse(status_code=200, content={"success": True, "message": "Admin calendar disconnected"})

