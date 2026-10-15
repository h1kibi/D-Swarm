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
) -> Account:
    """Current account from the bearer token (account JWT or API key).

    Resolves through the shared principal path, so revocation (logout denylist)
    and password-rotation checks apply here exactly like in the middleware and
    the role policy.
    """
    from apps.web.principal import resolve_account_from_token

    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    account = resolve_account_from_token(token)
    if account is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
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
) -> Optional[Account]:
    """Get current account if authenticated, otherwise None."""
    from apps.web.principal import resolve_account_from_token

    if not authorization or not authorization.startswith("Bearer "):
        return None
    return resolve_account_from_token(authorization[7:])
