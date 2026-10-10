"""Dependency injection for FastAPI routes."""

from __future__ import annotations

from typing import Optional
from fastapi import HTTPException, Header, Depends
from apps.web.accounts.service import AccountService
from apps.web.accounts.models import Account

# Global service instance
_account_service: Optional[AccountService] = None


def get_account_service() -> AccountService:
    """Get the account service instance (dependency injection)."""
    global _account_service
    if _account_service is None:
        _account_service = AccountService()
    return _account_service


async def get_current_account(
    authorization: str = Header(...),
    service: AccountService = Depends(get_account_service)
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
    authorization: str = Header(None),
    service: AccountService = Depends(get_account_service)
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
