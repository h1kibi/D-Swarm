"""Account management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from typing import Optional

from apps.web.accounts.dependencies import (
    get_account_service, get_current_account, require_admin
)
from apps.web.accounts.models import Account, AccountRole
from apps.web.accounts.schemas import (
    LoginRequest, LoginResponse, AccountResponse, CreateAccountRequest,
    CreateAccountResponse, UpdateAccountRequest, ChangePasswordRequest,
    CurrentUserResponse, AccountListResponse, ApiKeyResponse
)
from apps.web.accounts.service import AccountService

router = APIRouter(prefix="/api/auth", tags=["auth"])
accounts_router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ═══════════════════════════════════════════════════════════════════
# Authentication endpoints
# ═══════════════════════════════════════════════════════════════════

@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    service: AccountService = Depends(get_account_service)
):
    """Login with username and password.
    
    Returns access token and account information.
    """
    account = service.authenticate(req.username, req.password)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )
    
    access_token, ttl = service.create_access_token(account.id, token_type="access")
    refresh_token, _ = service.create_access_token(account.id, token_type="refresh")
    
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=ttl,
        account_id=account.id,
        username=account.username,
        display_name=account.display_name,
        role=account.role.value
    )


@router.post("/logout")
async def logout(
    current_account: Account = Depends(get_current_account)
):
    """Logout (client should discard token)."""
    return {"status": "ok"}


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user(
    current_account: Account = Depends(get_current_account)
):
    """Get current authenticated user information."""
    return CurrentUserResponse(
        id=current_account.id,
        username=current_account.username,
        display_name=current_account.display_name,
        role=current_account.role.value,
        is_active=current_account.is_active
    )


# ═══════════════════════════════════════════════════════════════════
# Account management endpoints (Admin only)
# ═══════════════════════════════════════════════════════════════════

@accounts_router.get("", response_model=AccountListResponse)
async def list_accounts(
    skip: int = 0,
    limit: int = 100,
    admin: Account = Depends(require_admin),
    service: AccountService = Depends(get_account_service)
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
    service: AccountService = Depends(get_account_service)
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
    service: AccountService = Depends(get_account_service)
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
    service: AccountService = Depends(get_account_service)
):
    """Update account (Admin only)."""
    account = service.update_account(
        account_id,
        is_active=req.is_active,
        display_name=req.display_name
    )
    
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    return AccountResponse(**account.to_dict())


@accounts_router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: str,
    admin: Account = Depends(require_admin),
    service: AccountService = Depends(get_account_service)
):
    """Delete account (Admin only - soft delete by deactivating)."""
    account = service.get_account_by_id(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    
    if account_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    
    service.update_account(account_id, is_active=False)


# ═══════════════════════════════════════════════════════════════════
# Password management
# ═══════════════════════════════════════════════════════════════════

@accounts_router.put("/me/password")
async def change_password(
    req: ChangePasswordRequest,
    current_account: Account = Depends(get_current_account),
    service: AccountService = Depends(get_account_service)
):
    """Change password for current user."""
    if not service.change_password(
        current_account.id,
        req.old_password,
        req.new_password
    ):
        raise HTTPException(status_code=401, detail="Incorrect old password")
    
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
# API key management
# ═══════════════════════════════════════════════════════════════════

@accounts_router.post("/me/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def generate_api_key(
    current_account: Account = Depends(get_current_account),
    service: AccountService = Depends(get_account_service)
):
    """Generate new API key for external integrations.
    
    Returns: {
        "key_id": "xxx",
        "provider": "local_agent",
        "created_at": 1234567890,
        "is_active": true,
        "api_token": "secret_token_only_shown_once"  # Not included in response
    }
    
    Note: The api_token should be displayed once and stored by the client.
    """
    plain_token, api_key = service.create_api_key(current_account.id)
    
    # Return response (token NOT included in normal response)
    return ApiKeyResponse(
        key_id=api_key.id,
        provider=api_key.provider,
        created_at=api_key.created_at,
        is_active=api_key.is_active
    )
