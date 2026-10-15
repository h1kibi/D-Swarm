"""Account management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from typing import Optional

from sqlalchemy.orm import Session

from apps.web.accounts import login_guard
from apps.web.accounts.dependencies import (
    get_account_service, get_current_account, require_admin
)
from apps.web.db.session import get_db
from apps.web.services.audit_service import AuditService
from apps.web.accounts.models import Account, AccountRole
from apps.web.accounts.schemas import (
    LoginRequest, AccountResponse, CreateAccountRequest,
    CreateAccountResponse, UpdateAccountRequest, ChangePasswordRequest,
    AccountListResponse, ApiKeyResponse
)
from apps.web.accounts.db_service import DbAccountService
from apps.web.auth import AuthConfig, check_password, issue_token, verify_token
from dswarm.core.runtime_env import is_web_container

router = APIRouter(prefix="/api/auth", tags=["auth"])
accounts_router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ═══════════════════════════════════════════════════════════════════
# Authentication endpoints
# ═══════════════════════════════════════════════════════════════════

@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Login: account credentials, or the operator password (P3 gate).

    - no password at all: open-mode probe → 200 ``auth_required: false``
    - account credentials (username optional; the password identifies the
      account) → account JWT, returned as both ``access_token`` and ``token``
    - operator password (DSWARM_WEB_PASSWORD, gate enabled) → P3 session token

    Throttled per client host: repeated failures answer 429 until the window
    frees; every attempt lands in the audit trail.
    """
    cfg = AuthConfig.from_env()
    host = (request.client.host if request.client else "") or "unknown"

    wait = login_guard.blocked_for(request.app, host)
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed attempts; retry in {int(wait) + 1}s",
        )

    if not req.password:
        if not cfg.enabled:
            return {"ok": True, "token": "", "auth_required": False}
        login_guard.record_failure(request.app, host)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid password"
        )

    username = req.username
    if not username:
        # Password-only mode: match the password against active accounts and
        # log in as the account it belongs to (solo-operator convenience).
        candidates, _ = service.list_accounts(skip=0, limit=1000)
        for candidate in candidates:
            if candidate.is_active and service.verify_password(
                req.password, candidate.password_hash
            ):
                username = candidate.username
                break

    if username:
        account = service.authenticate(username, req.password)
        if account:
            login_guard.record_success(request.app, host)
            access_token, ttl = service.create_access_token(account.id, token_type="access")
            AuditService(db).log_action(
                action="login",
                actor_id=account.id,
                resource_type="account",
                resource_id=account.id,
                ip_address=host,
                details={"username": account.username, "role": account.role.value},
            )
            return {
                "access_token": access_token,
                "token": access_token,  # legacy deck field
                "token_type": "bearer",
                "expires_in": ttl,
                "account_id": account.id,
                "username": account.username,
                "display_name": account.display_name,
                "role": account.role.value,
                "auth_required": cfg.enabled,
                "ok": True,
            }

    # Operator-password fallback (P3 single-password gate).
    if cfg.enabled and check_password(cfg, req.password):
        login_guard.record_success(request.app, host)
        token = issue_token(cfg)
        AuditService(db).log_action(
            action="login",
            actor_id="operator",
            resource_type="auth",
            resource_id="operator",
            ip_address=host,
            details={"mode": "operator-password"},
        )
        return {
            "token": token,
            "access_token": token,
            "token_type": "bearer",
            "username": "operator",
            "display_name": "Operator",
            "role": "admin",
            "auth_required": True,
            "ok": True,
        }

    login_guard.record_failure(request.app, host)
    AuditService(db).log_action(
        action="login",
        actor_id="unknown",
        resource_type="auth",
        resource_id=str(req.username or "?"),
        ip_address=host,
        status="failure",
        details={"username": req.username or ""},
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
    )


@router.post("/logout")
async def logout(
    current_account: Account = Depends(get_current_account),
    service: DbAccountService = Depends(get_account_service),
    authorization: Optional[str] = Header(None),
):
    """Logout. Denylists this token's jti so it stops working immediately."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    payload = service.verify_access_token(token, "access") if token else None
    if payload and payload.get("jti"):
        service.revoke_token(
            str(payload["jti"]),
            account_id=current_account.id,
            expires_at=payload.get("exp"),
        )
    return {"status": "ok"}


@router.get("/me")
async def get_current_user(
    authorization: Optional[str] = Header(None),
    service: DbAccountService = Depends(get_account_service),
):
    """Current identity: the account user object, or the P3 gate status.

    The dashboard pages need the account (id/role); the deck's LoginGate needs
    ``auth_required``/``in_container``. One endpoint serves both.
    """
    cfg = AuthConfig.from_env()
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]

    if token:
        payload = service.verify_access_token(token, "access")
        if payload:
            account = service.get_account_by_id(payload["account_id"])
            if account and account.is_active:
                return {
                    "id": account.id,
                    "username": account.username,
                    "display_name": account.display_name,
                    "role": account.role.value,
                    "is_active": account.is_active,
                }
        if verify_token(cfg, token):
            return {
                "authenticated": True,
                "auth_required": cfg.enabled,
                "in_container": is_web_container(),
            }

    if not cfg.enabled:
        return {
            "authenticated": True,
            "auth_required": False,
            "in_container": is_web_container(),
        }

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ═══════════════════════════════════════════════════════════════════
# Account management endpoints (Admin only)
# ═══════════════════════════════════════════════════════════════════

@accounts_router.get("", response_model=AccountListResponse)
async def list_accounts(
    skip: int = 0,
    limit: int = 100,
    admin: Account = Depends(require_admin),
    service: DbAccountService = Depends(get_account_service)
):
    """List all accounts (Admin only)."""
    accounts, total = service.list_accounts(skip=skip, limit=limit)
    return AccountListResponse(
        accounts=[AccountResponse(**acc.to_dict()) for acc in accounts],
        total=total,
        skip=skip,
        limit=limit
    )


@accounts_router.post("", response_model=CreateAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(
    req: CreateAccountRequest,
    admin: Account = Depends(require_admin),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Create new account (Admin only)."""
    try:
        account = service.create_account(
            username=req.username,
            display_name=req.display_name,
            password=req.password,
            role=AccountRole(req.role),
            created_by=admin.id
        )
        AuditService(db).log_action(
            action="create",
            actor_id=admin.id,
            resource_type="account",
            resource_id=account.id,
            details={"username": account.username, "role": account.role.value},
        )
        
        return CreateAccountResponse(
            id=account.id,
            username=account.username,
            display_name=account.display_name,
            role=account.role.value,
            created_at=account.created_at,
            temporary_password=req.password  # Only shown once
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@accounts_router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str,
    admin: Account = Depends(require_admin),
    service: DbAccountService = Depends(get_account_service)
):
    """Get account information (Admin only)."""
    account = service.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return AccountResponse(**account.to_dict())


@accounts_router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str,
    req: UpdateAccountRequest,
    admin: Account = Depends(require_admin),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Update account (Admin only)."""
    account = service.update_account(
        account_id,
        is_active=req.is_active,
        display_name=req.display_name
    )
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    AuditService(db).log_action(
        action="update",
        actor_id=admin.id,
        resource_type="account",
        resource_id=account_id,
        details={k: v for k, v in {"is_active": req.is_active, "display_name": req.display_name}.items() if v is not None},
    )
    return AccountResponse(**account.to_dict())


@accounts_router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: str,
    admin: Account = Depends(require_admin),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Delete account (Admin only - soft delete by deactivating)."""
    account = service.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    
    service.update_account(account_id, is_active=False)
    AuditService(db).log_action(
        action="delete",
        actor_id=admin.id,
        resource_type="account",
        resource_id=account_id,
        details={"soft": True},
    )


# ═══════════════════════════════════════════════════════════════════
# Password management
# ═══════════════════════════════════════════════════════════════════

@accounts_router.put("/me/password")
async def change_password(
    req: ChangePasswordRequest,
    current_account: Account = Depends(get_current_account),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Change password for current user."""
    if not service.change_password(
        current_account.id,
        req.old_password,
        req.new_password
    ):
        raise HTTPException(status_code=401, detail="Incorrect old password")
    
    AuditService(db).log_action(
        action="update",
        actor_id=current_account.id,
        resource_type="account",
        resource_id=current_account.id,
        details={"field": "password"},
    )
    # Password change rotates credentials: every token issued earlier stops
    # working, and the caller gets a fresh one so this session continues.
    service.bump_token_valid_after(current_account.id)
    access_token, ttl = service.create_access_token(current_account.id, token_type="access")
    return {"status": "ok", "access_token": access_token, "expires_in": ttl}


# ═══════════════════════════════════════════════════════════════════
# API key management
# ═══════════════════════════════════════════════════════════════════

@accounts_router.post("/me/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def generate_api_key(
    current_account: Account = Depends(get_current_account),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Generate a new API key for external integrations.

    The plaintext ``api_token`` is included in THIS response only — store it
    now; it can never be retrieved again (revoke + recreate to rotate).
    """
    plain_token, api_key = service.create_api_key(current_account.id)
    AuditService(db).log_action(
        action="create",
        actor_id=current_account.id,
        resource_type="api_key",
        resource_id=api_key.id,
    )
    return ApiKeyResponse(
        key_id=api_key.id,
        provider=api_key.provider,
        created_at=api_key.created_at,
        is_active=api_key.is_active,
        api_token=plain_token,
    )


@accounts_router.get("/me/api-keys")
async def list_my_api_keys(
    current_account: Account = Depends(get_current_account),
    service: DbAccountService = Depends(get_account_service),
):
    """List the current user's API keys (metadata only, never plaintext)."""
    return {"keys": service.list_api_keys(current_account.id)}


@accounts_router.delete("/me/api-keys/{key_id}")
async def revoke_my_api_key(
    key_id: str,
    current_account: Account = Depends(get_current_account),
    service: DbAccountService = Depends(get_account_service),
    db: Session = Depends(get_db),
):
    """Revoke one of the current user's API keys."""
    from apps.web.db.models import AccountApiKeyModel

    key = service.db.query(AccountApiKeyModel).filter_by(id=key_id).first()
    if key is None or key.account_id != current_account.id:
        raise HTTPException(status_code=404, detail="API key not found")
    ok = service.revoke_api_key(key_id)
    AuditService(db).log_action(
        action="delete",
        actor_id=current_account.id,
        resource_type="api_key",
        resource_id=key_id,
        status="success" if ok else "failure",
    )
    return {"ok": ok}
