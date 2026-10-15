"""Principal resolution + role policy for the web API.

One place answers "who is calling, and may they do this?" for surfaces the P3
midldeware alone cannot protect:

- Gate disabled (no ``DSWARM_WEB_PASSWORD``, loopback dev box): policy is
  lenient, exactly like the middleware — local development and the scripted
  test suite keep their anonymous contracts.
- Gate enabled: admin surfaces (settings / credentials / probes / worker
  image) require an ADMIN principal — the P3 operator token or an admin
  account JWT; a member JWT gets 403. Everything else on the rail only needs
  *some* authenticated principal (the middleware already 401s anonymous
  callers before handlers run).

Account JWTs resolve through the shared DbAccountService, so a deactivated
account's token stops working immediately (the middleware uses the same check).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import Header, HTTPException, Request, status

from apps.web.auth import AuthConfig, bearer_from_header, verify_token

ACCOUNT_ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    kind: str  # "operator" (P3 password token) | "account"
    role: str  # "admin" | "member"; the P3 operator is always admin
    account_id: Optional[str] = None


def load_account(account_id: str) -> Any:
    """DB lookup seam (monkeypatched in tests)."""
    from apps.web.accounts.db_service import DbAccountService
    from apps.web.db.session import SessionLocal

    db = SessionLocal()
    try:
        return DbAccountService(db).get_account_by_id(account_id)
    finally:
        db.close()


def _role_of(account: Any) -> str:
    role = getattr(account, "role", None)
    value = getattr(role, "value", role)
    return str(value or "member")


def resolve_account_from_token(token: Optional[str]) -> Optional[Any]:
    """The active account behind an account JWT, else None. Signature + type + DB."""
    if not token:
        return None
    from apps.web.accounts.db_service import DbAccountService
    from apps.web.db.session import SessionLocal

    db = SessionLocal()
    try:
        payload = DbAccountService(db).verify_access_token(token, "access")
    finally:
        db.close()
    if not payload:
        return None
    account = load_account(str(payload.get("account_id") or ""))
    if account is None or not getattr(account, "is_active", False):
        return None
    return account


def account_token_is_active(token: Optional[str]) -> bool:
    """Middleware helper: an account JWT is a valid credential while active."""
    try:
        return resolve_account_from_token(token) is not None
    except Exception:
        return False


def resolve_principal(request: Request, authorization: Optional[str]) -> Optional[Principal]:
    cfg: AuthConfig = request.app.state.auth
    token = bearer_from_header(authorization)
    if cfg.enabled and token and verify_token(cfg, token):
        return Principal(kind="operator", role="admin")
    account = resolve_account_from_token(token)
    if account is not None:
        return Principal(kind="account", role=_role_of(account), account_id=account.id)
    return None


async def require_operator(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Optional[Principal]:
    """Any authenticated principal when the gate is on; None in dev/open mode."""
    principal = resolve_principal(request, authorization)
    if principal is not None:
        return principal
    if not request.app.state.auth.enabled:
        return None
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def require_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> Optional[Principal]:
    """Admin principal (P3 operator or admin account) when the gate is on."""
    principal = await require_operator(request, authorization)
    if principal is None or principal.role == ACCOUNT_ADMIN:
        return principal
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")


__all__ = [
    "Principal",
    "account_token_is_active",
    "require_admin",
    "require_operator",
    "resolve_account_from_token",
    "resolve_principal",
]
