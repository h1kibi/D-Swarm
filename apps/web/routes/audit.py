"""Audit logging API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional

from apps.web.db.session import get_db
from apps.web.db.models import AccountModel
from apps.web.services.audit_service import AuditService
from apps.web.accounts.db_service import DbAccountService
from pydantic import BaseModel
from datetime import datetime


# Pydantic models
class AuditLogResponse(BaseModel):
    """Audit log entry response."""
    id: str
    action: str
    actor: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    details: dict
    timestamp: float
    ip_address: Optional[str]
    status: str


class AuditStatistics(BaseModel):
    """Account audit statistics."""
    account_id: str
    total_actions: int
    successful_actions: int
    failed_actions: int
    failure_rate: float
    action_breakdown: dict
    last_action_timestamp: Optional[str]


# Router
router = APIRouter(prefix="/api/audit", tags=["audit"])


def get_current_account(
    authorization: Optional[str] = None,
    db: Session = Depends(get_db)
) -> AccountModel:
    """Get current authenticated account."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    token = authorization[7:]
    service = DbAccountService(db)
    payload = service.verify_access_token(token, "access")
    
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    account = db.query(AccountModel).filter_by(id=payload["account_id"]).first()
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    return account


def require_admin(account: AccountModel = Depends(get_current_account)) -> AccountModel:
    """Require admin role."""
    if account.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return account


@router.get("/logs")
async def get_audit_logs(
    action: Optional[str] = None,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> dict:
    """Get audit logs (admin only)."""
    service = AuditService(db)
    logs, total = service.get_logs(
        action=action,
        actor_id=actor_id,
        resource_type=resource_type,
        resource_id=resource_id,
        status=status_filter,
        skip=skip,
        limit=limit
    )
    
    return {
        "logs": [
            AuditLogResponse(
                id=log.id,
                action=log.action,
                actor=log.actor,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                timestamp=log.timestamp.timestamp(),
                ip_address=log.ip_address,
                status=log.status
            )
            for log in logs
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/account/{account_id}/actions")
async def get_account_actions(
    account_id: str,
    skip: int = 0,
    limit: int = 100,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> dict:
    """Get all actions by a specific account (admin only)."""
    service = AuditService(db)
    logs, total = service.get_account_actions(account_id, skip, limit)
    
    return {
        "account_id": account_id,
        "logs": [
            AuditLogResponse(
                id=log.id,
                action=log.action,
                actor=log.actor,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                timestamp=log.timestamp.timestamp(),
                ip_address=log.ip_address,
                status=log.status
            )
            for log in logs
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/account/{account_id}/statistics")
async def get_account_statistics(
    account_id: str,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> AuditStatistics:
    """Get audit statistics for an account (admin only)."""
    service = AuditService(db)
    stats = service.get_account_statistics(account_id)
    
    return AuditStatistics(**stats)


@router.get("/resource/{resource_type}/{resource_id}")
async def get_resource_history(
    resource_type: str,
    resource_id: str,
    skip: int = 0,
    limit: int = 100,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> dict:
    """Get audit history for a specific resource (admin only)."""
    service = AuditService(db)
    logs, total = service.get_resource_history(resource_type, resource_id, skip, limit)
    
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "logs": [
            AuditLogResponse(
                id=log.id,
                action=log.action,
                actor=log.actor,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                timestamp=log.timestamp.timestamp(),
                ip_address=log.ip_address,
                status=log.status
            )
            for log in logs
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/failures")
async def get_failed_actions(
    skip: int = 0,
    limit: int = 100,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> dict:
    """Get all failed actions for security review (admin only)."""
    service = AuditService(db)
    logs, total = service.get_failed_actions(skip, limit)
    
    return {
        "logs": [
            AuditLogResponse(
                id=log.id,
                action=log.action,
                actor=log.actor,
                resource_type=log.resource_type,
                resource_id=log.resource_id,
                details=log.details,
                timestamp=log.timestamp.timestamp(),
                ip_address=log.ip_address,
                status=log.status
            )
            for log in logs
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }
