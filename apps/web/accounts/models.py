"""Account domain model and database definitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AccountRole(str, Enum):
    """User account roles with permissions."""
    ADMIN = "admin"
    MEMBER = "member"


@dataclass
class Account:
    """User account model.
    
    Attributes:
        id: Unique account identifier (UUID)
        username: Unique username for login
        display_name: Display name for UI
        role: Account role (admin or member)
        password_hash: bcrypt password hash (never store plaintext)
        is_active: Whether account is enabled
        created_by: Admin who created this account (UUID, None for first admin)
        created_at: Timestamp of account creation
        updated_at: Timestamp of last update
        last_login: Timestamp of last login
    """
    id: str
    username: str
    display_name: str
    role: AccountRole
    password_hash: str
    is_active: bool = True
    created_by: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_login: Optional[float] = None
    
    @staticmethod
    def new(username: str, display_name: str, password_hash: str, 
            role: AccountRole = AccountRole.MEMBER, 
            created_by: Optional[str] = None) -> Account:
        """Create a new account instance."""
        import time
        now = time.time()
        return Account(
            id=str(uuid.uuid4()),
            username=username,
            display_name=display_name,
            role=role,
            password_hash=password_hash,
            is_active=True,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            last_login=None
        )
    
    def can_create_target(self) -> bool:
        """Check if account can create targets."""
        return self.is_active
    
    def can_delete_target(self, target_creator_id: str) -> bool:
        """Check if account can delete a target.
        
        Admin can delete any target; members can only delete their own.
        """
        if self.role == AccountRole.ADMIN:
            return True
        return self.id == target_creator_id
    
    def can_view_audit(self) -> bool:
        """Check if account can view audit logs."""
        return self.role == AccountRole.ADMIN
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses (no password hash)."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role.value,
            "is_active": self.is_active,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login": self.last_login,
        }


@dataclass
class AccountApiKey:
    """API key for external integrations (本地 Agent).
    
    Attributes:
        id: Key identifier
        account_id: Owner account ID
        provider: Provider name (e.g., "local_agent")
        encrypted_key: Encrypted API token (Fernet)
        created_at: Creation timestamp
        revoked_at: Revocation timestamp (None if active)
    """
    id: str
    account_id: str
    provider: str
    encrypted_key: str
    created_at: float
    revoked_at: Optional[float] = None
    
    @staticmethod
    def new(account_id: str, provider: str, encrypted_key: str) -> AccountApiKey:
        """Create new API key."""
        import time
        return AccountApiKey(
            id=str(uuid.uuid4()),
            account_id=account_id,
            provider=provider,
            encrypted_key=encrypted_key,
            created_at=time.time(),
            revoked_at=None
        )
    
    @property
    def is_active(self) -> bool:
        """Check if key is currently active."""
        return self.revoked_at is None
