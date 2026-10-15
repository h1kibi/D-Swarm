"""Dependency injection for FastAPI routes."""

from __future__ import annotations

from typing import Optional
from fastapi import HTTPException, Header, Depends
from sqlalchemy.orm import Session

from apps.web.db.session import get_db
from apps.web.accounts.db_service import DbAccountService
from apps.web.accounts.models import Account


def get_account_service(db: Session = Depends(get_db)) -> DbAccountService:
    """Get a database-backed account service bound to this request's session."""
    return DbAccountService(db)


async def get_current_account(
    authorization: str = Header(...),
    service: DbAccountService = Depends(get_account_service)
) -> Account:
    """Get current authenticated account from Authorization header.

    Expects: Authorization: Bearer <token>
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization[7:]  # Remove "Bearer " prefix
    payload = service.verify_access_token(token, expected_type="access")

    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    account = service.get_account_by_id(payload["account_id"])
    if not account or not account.is_active:
        raise HTTPException(status_code=401, detail="Account not found or inactive")

    return account


async def require_admin(
    current_account: Account = Depends(get_current_account)
) -> Account:
    """Dependency to require admin role."""
    if current_account.role.value != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_account


async def get_current_account_optional(
    authorization: Optional[str] = Header(None),
    service: DbAccountService = Depends(get_account_service)
) -> Optional[Account]:
    """Get current account if authenticated, otherwise None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization[7:]
    payload = service.verify_access_token(token, expected_type="access")

    if not payload:
        return None

    account = service.get_account_by_id(payload["account_id"])
    if account and account.is_active:
        return account

    return None
