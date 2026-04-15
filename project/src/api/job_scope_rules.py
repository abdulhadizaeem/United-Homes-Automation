"""Admin API for managing job scope rules (heavy-job thresholds)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.utils.auth import require_admin
from src.utils.db import get_job_scope_rules, update_job_scope_rule

router = APIRouter()

VALID_SERVICES = {"air_duct", "chimney", "power_washing", "dryer_vent", "gutter"}


class UpdateRuleRequest(BaseModel):
    units_threshold: int


@router.get("/")
def list_job_scope_rules(current_user: dict = Depends(require_admin)):
    """Return all heavy-job threshold rules."""
    try:
        rules = get_job_scope_rules()
        return {"success": True, "data": rules}
    except Exception as e:
        logging.error("Error fetching job scope rules: %s", e)
        raise HTTPException(status_code=500, detail="Failed to fetch rules")


@router.put("/{service_type}")
def update_rule(
    service_type: str,
    body: UpdateRuleRequest,
    current_user: dict = Depends(require_admin),
):
    """Update or create the units_threshold for a service type.

    Example: PUT /api/admin/job-scope-rules/chimney  {"units_threshold": 3}
    """
    if service_type not in VALID_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service_type '{service_type}'. Valid: {sorted(VALID_SERVICES)}",
        )
    if body.units_threshold < 1:
        raise HTTPException(status_code=400, detail="units_threshold must be >= 1")

    try:
        update_job_scope_rule(service_type, body.units_threshold)
        logging.info(
            "[SCOPE RULES] Admin %s updated %s threshold to %d",
            current_user.get("email"), service_type, body.units_threshold,
        )
        return {
            "success": True,
            "data": {"service_type": service_type, "units_threshold": body.units_threshold},
        }
    except Exception as e:
        logging.error("Error updating job scope rule: %s", e)
        raise HTTPException(status_code=500, detail="Failed to update rule")
