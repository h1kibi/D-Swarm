"""Account service layer for authentication and account management."""

from __future__ import annotations

import os
import secrets
import time
from typing import Optional, List, Tuple

from passlib.context import CryptContext
import jwt

from apps.web.accounts.models import Account, AccountRole, AccountApiKey
from apps.web.accounts.runtime import jwt_secret
from cryptography.fernet import Fernet


# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AccountService:
    """Service for account management and authentication."""
    
    # In-memory storage for demo; replace with database
    _accounts: dict[str, Account] = {}
    _api_keys: dict[str, AccountApiKey] = {}
    
    def __init__(self):
        """Initialize account service."""
        self.jwt_secret = jwt_secret()
        self.jwt_algorithm = os.getenv("DSWARM_JWT_ALGORITHM", "HS256")
        self.jwt_ttl = int(os.getenv("DSWARM_WEB_SESSION_TTL", "43200"))  # 12 hours
        self.api_token_ttl = int(os.getenv("DSWARM_API_TOKEN_TTL", "31536000"))  # 365 days
        
        # Initialize encryption key for API tokens
        key_str = os.getenv("DSWARM_ENCRYPTION_KEY")
        if not key_str:
            # Generate a temporary key for demo (should use env var in production)
            self.encryption_key = Fernet.generate_key()
        else:
            self.encryption_key = key_str.encode() if isinstance(key_str, str) else key_str
        
        self.cipher = Fernet(self.encryption_key)
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against a hash."""
        return pwd_context.verify(plain_password, hashed_password)
    
    def create_account(self, username: str, display_name: str, password: str,
                      role: AccountRole = AccountRole.MEMBER,
                      created_by: Optional[str] = None) -> Account:
        """Create a new account.
        
        Args:
            username: Username for login
            display_name: Display name for UI
            password: Plain text password (will be hashed)
            role: Account role (admin or member)
            created_by: Admin account ID who created this
            
        Returns:
            New Account instance
            
        Raises:
            ValueError: If username already exists
        """
        # Check if username exists
        if any(acc.username == username for acc in self._accounts.values()):
            raise ValueError(f"Username '{username}' already exists")
        
        password_hash = self.hash_password(password)
        account = Account.new(
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            role=role,
            created_by=created_by
        )
        
        self._accounts[account.id] = account
        return account
    
    def authenticate(self, username: str, password: str) -> Optional[Account]:
        """Authenticate user with username and password.
        
        Args:
            username: Login username
            password: Login password
            
        Returns:
            Account if authentication succeeds, None otherwise
        """
        account = None
        for acc in self._accounts.values():
            if acc.username == username:
                account = acc
                break
        
        if not account or not account.is_active:
            return None
        
        if not self.verify_password(password, account.password_hash):
            return None
        
        # Update last login
        account.last_login = time.time()
        
        return account
    
    def get_account_by_id(self, account_id: str) -> Optional[Account]:
        """Get account by ID."""
        return self._accounts.get(account_id)
    
    def get_account_by_username(self, username: str) -> Optional[Account]:
        """Get account by username."""
        for account in self._accounts.values():
            if account.username == username:
                return account
        return None
    
    def list_accounts(self, skip: int = 0, limit: int = 100) -> Tuple[List[Account], int]:
        """List all accounts with pagination."""
        accounts = list(self._accounts.values())
        total = len(accounts)
        return accounts[skip:skip+limit], total
    
    def update_account(self, account_id: str, 
                      is_active: Optional[bool] = None,
                      display_name: Optional[str] = None) -> Optional[Account]:
        """Update account information."""
        account = self._accounts.get(account_id)
        if not account:
            return None
        
        if is_active is not None:
            account.is_active = is_active
        if display_name is not None:
            account.display_name = display_name
        
        account.updated_at = time.time()
        return account
    
    def change_password(self, account_id: str, old_password: str, 
                       new_password: str) -> bool:
        """Change account password.
        
        Returns:
            True if successful, False if old password is wrong
        """
        account = self._accounts.get(account_id)
        if not account:
            return False
        
        if not self.verify_password(old_password, account.password_hash):
            return False
        
        account.password_hash = self.hash_password(new_password)
        account.updated_at = time.time()
        return True
    
    def create_access_token(self, account_id: str, 
                           token_type: str = "access") -> Tuple[str, int]:
        """Create JWT access token.
        
        Args:
            account_id: Account ID
            token_type: Token type ("access" or "refresh")
            
        Returns:
            Tuple of (token, expires_in_seconds)
        """
        account = self._accounts.get(account_id)
        if not account:
            raise ValueError(f"Account {account_id} not found")
        
        ttl = self.jwt_ttl if token_type == "access" else 7 * 24 * 3600  # 7 days for refresh
        expires_at = int(time.time()) + ttl
        
        payload = {
            "account_id": account_id,
            "username": account.username,
            "role": account.role.value,
            "type": token_type,
            "exp": expires_at,
            "iat": int(time.time())
        }
        
        token = jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)
        return token, ttl
    
    def verify_access_token(self, token: str, 
                           expected_type: str = "access") -> Optional[dict]:
        """Verify JWT token and return payload.
        
        Args:
            token: JWT token string
            expected_type: Expected token type ("access" or "refresh")
            
        Returns:
            Token payload if valid, None otherwise
        """
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])
            
            # Check token type
            if payload.get("type") != expected_type:
                return None
            
            return payload
        except jwt.InvalidTokenError:
            return None
    
    def create_api_key(self, account_id: str) -> Tuple[str, AccountApiKey]:
        """Create a new API key for external integrations.
        
        Returns:
            Tuple of (plain_token, AccountApiKey_obj)
            The plain token is only returned once; store it securely.
        """
        # Generate a new token
        plain_token = secrets.token_urlsafe(32)
        
        # Encrypt it for storage
        encrypted = self.cipher.encrypt(plain_token.encode()).decode()
        
        api_key = AccountApiKey.new(account_id, "local_agent", encrypted)
        self._api_keys[api_key.id] = api_key
        
        return plain_token, api_key
    
    def verify_api_token(self, token: str) -> Optional[dict]:
        """Verify an API token and return account info.
        
        Returns:
            Dict with account_id if valid, None otherwise
        """
        # Find the key by comparing encrypted values
        for key in self._api_keys.values():
            if not key.is_active:
                continue
            
            try:
                decrypted = self.cipher.decrypt(key.encrypted_key.encode()).decode()
                if decrypted == token:
                    account = self._accounts.get(key.account_id)
                    if account and account.is_active:
                        return {
                            "account_id": account.id,
                            "username": account.username,
                            "role": account.role.value
                        }
            except Exception:
                continue
        
        return None
    
    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key."""
        key = self._api_keys.get(key_id)
        if not key:
            return False
        
        key.revoked_at = time.time()
        return True
