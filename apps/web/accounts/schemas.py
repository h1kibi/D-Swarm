"""Pydantic schemas for account-related API requests/responses."""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional, List


class LoginRequest(BaseModel):
    """Login request with username and password."""
    username: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    """Login response with JWT tokens."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    account_id: str
    username: str
    display_name: str
    role: str  # "admin" or "member"


class AccountResponse(BaseModel):
    """Account information response (no sensitive data)."""
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: float
    updated_at: float
    last_login: Optional[float] = None


class AccountListResponse(BaseModel):
    """List of accounts with pagination."""
    accounts: List[AccountResponse]
    total: int
    skip: int
    limit: int


class CreateAccountRequest(BaseModel):
    """Admin request to create new account."""
    username: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8)  # Minimum 8 chars
    role: str = Field(default="member", regex="^(admin|member)$")


class CreateAccountResponse(BaseModel):
    """Response when account is created."""
    id: str
    username: str
    display_name: str
    role: str
    created_at: float
    temporary_password: str  # Only shown once


class UpdateAccountRequest(BaseModel):
    """Admin request to update account."""
    is_active: Optional[bool] = None
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)


class ChangePasswordRequest(BaseModel):
    """User request to change their password."""
    old_password: str
    new_password: str = Field(..., min_length=8)


class ApiKeyResponse(BaseModel):
    """API key response."""
    key_id: str
    provider: str
    created_at: float
    is_active: bool


class RefreshTokenRequest(BaseModel):
    """Refresh token request."""
    refresh_token: str


class CurrentUserResponse(BaseModel):
    """Current authenticated user info."""
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
