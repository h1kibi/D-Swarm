"""Database-backed account service layer."""

from __future__ import annotations

import os
import secrets
import time
from typing import Optional, List, Tuple
from datetime import datetime, timedelta

from passlib.context import CryptContext
import jwt
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet

from apps.web.db.models import AccountModel, AccountApiKeyModel, AccountRole
from apps.web.accounts.models import Account, AccountApiKey


# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class DbAccountService:
    """Database-backed account service for production use."""
    
    def __init__(self, db: Session):
        """Initialize with database session."""
        self.db = db
        
        # JWT configuration
        self.jwt_secret = os.getenv("DSWARM_JWT_SECRET", "dev-secret-change-in-production")
        self.jwt_algorithm = os.getenv("DSWARM_JWT_ALGORITHM", "HS256")
        self.session_ttl = int(os.getenv("DSWARM_WEB_SESSION_TTL", "43200"))  # 12 hours
        self.api_token_ttl = int(os.getenv("DSWARM_API_TOKEN_TTL", "31536000"))  # 365 days
        
        # Encryption for API keys
        key = os.getenv("DSWARM_ENCRYPTION_KEY")
        if key:
            self.cipher = Fernet(key.encode())
        else:
            # Dev fallback - generate ephemeral key (don't use in production!)
            self.cipher = Fernet(Fernet.generate_key())
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        return pwd_context.hash(password)
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify a password against a hash."""
        return pwd_context.verify(plain_password, hashed_password)
    
    def create_account(
        self,
        username: str,
        display_name: str,
        password: str,
        role: str = "member",
        created_by: Optional[str] = None
    ) -> Account:
        """Create a new account."""
        # Check username not in use
        existing = self.db.query(AccountModel).filter_by(username=username).first()
        if existing:
            raise ValueError(f"Username '{username}' already in use")
        
        account_model = AccountModel(
            username=username,
            display_name=display_name,
            password_hash=self.hash_password(password),
            role=AccountRole.ADMIN if role == "admin" else AccountRole.MEMBER,
            is_active=True,
            created_by=created_by
        )
        
        self.db.add(account_model)
        self.db.commit()
        self.db.refresh(account_model)
        
        return self._model_to_account(account_model)
    
    def authenticate(self, username: str, password: str) -> Optional[Account]:
        """Authenticate user with username and password."""
        account_model = self.db.query(AccountModel).filter_by(username=username).first()
        
        if not account_model:
            return None
        
        if not account_model.is_active:
            return None
        
        if not self.verify_password(password, account_model.password_hash):
            return None
        
        # Update last login
        account_model.last_login = datetime.utcnow()
        self.db.commit()
        
        return self._model_to_account(account_model)
    
    def get_account_by_id(self, account_id: str) -> Optional[Account]:
        """Get account by ID."""
        model = self.db.query(AccountModel).filter_by(id=account_id).first()
        return self._model_to_account(model) if model else None
    
    def get_account_by_username(self, username: str) -> Optional[Account]:
        """Get account by username."""
        model = self.db.query(AccountModel).filter_by(username=username).first()
        return self._model_to_account(model) if model else None
    
    def list_accounts(self, skip: int = 0, limit: int = 100) -> Tuple[List[Account], int]:
        """List all accounts with pagination."""
        query = self.db.query(AccountModel)
        total = query.count()
        models = query.offset(skip).limit(limit).all()
        accounts = [self._model_to_account(m) for m in models]
        return accounts, total
    
    def update_account(
        self,
        account_id: str,
        is_active: Optional[bool] = None,
        display_name: Optional[str] = None
    ) -> Optional[Account]:
        """Update account information."""
        model = self.db.query(AccountModel).filter_by(id=account_id).first()
        if not model:
            return None
        
        if is_active is not None:
            model.is_active = is_active
        
        if display_name is not None:
            model.display_name = display_name
        
        model.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(model)
        
        return self._model_to_account(model)
    
    def change_password(self, account_id: str, old_password: str, new_password: str) -> bool:
        """Change account password."""
        model = self.db.query(AccountModel).filter_by(id=account_id).first()
        if not model:
            return False
        
        if not self.verify_password(old_password, model.password_hash):
            return False
        
        model.password_hash = self.hash_password(new_password)
        self.db.commit()
        return True
    
    def create_access_token(self, account_id: str, token_type: str = "access") -> Tuple[str, int]:
        """Create JWT access token."""
        account = self.db.query(AccountModel).filter_by(id=account_id).first()
        if not account:
            raise ValueError(f"Account {account_id} not found")
        
        if token_type == "access":
            ttl = self.session_ttl
        elif token_type == "api":
            ttl = self.api_token_ttl
        else:
            raise ValueError(f"Invalid token type: {token_type}")
        
        now = time.time()
        payload = {
            "account_id": account_id,
            "username": account.username,
            "role": account.role.value,
            "type": token_type,
            "iat": now,
            "exp": now + ttl
        }
        
        token = jwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)
        return token, ttl
    
    def verify_access_token(self, token: str, expected_type: str = "access") -> Optional[dict]:
        """Verify JWT token and return payload."""
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm])
            
            if payload.get("type") != expected_type:
                return None
            
            return payload
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None
    
    def create_api_key(self, account_id: str) -> Tuple[str, AccountApiKey]:
        """Create a new API key for external integrations."""
        account_model = self.db.query(AccountModel).filter_by(id=account_id).first()
        if not account_model:
            raise ValueError(f"Account {account_id} not found")
        
        # Generate plain token
        plain_token = secrets.token_urlsafe(32)
        
        # Encrypt it
        encrypted = self.cipher.encrypt(plain_token.encode())
        
        api_key_model = AccountApiKeyModel(
            account_id=account_id,
            provider="local_agent",
            encrypted_key=encrypted.decode()
        )
        
        self.db.add(api_key_model)
        self.db.commit()
        self.db.refresh(api_key_model)
        
        api_key = AccountApiKey(
            id=api_key_model.id,
            account_id=account_id,
            provider=api_key_model.provider,
            created_at=api_key_model.created_at.timestamp(),
            revoked_at=None
        )
        
        return plain_token, api_key
    
    def verify_api_token(self, token: str) -> Optional[dict]:
        """Verify an API token and return account info."""
        try:
            # Try to find matching key (by encrypted comparison)
            for key_model in self.db.query(AccountApiKeyModel).all():
                try:
                    decrypted = self.cipher.decrypt(key_model.encrypted_key.encode())
                    if decrypted.decode() == token:
                        if key_model.revoked_at:
                            return None
                        
                        account = self.db.query(AccountModel).filter_by(id=key_model.account_id).first()
                        if not account or not account.is_active:
                            return None
                        
                        return {
                            "account_id": account.id,
                            "username": account.username,
                            "role": account.role.value
                        }
                except Exception:
                    continue
            
            return None
        except Exception:
            return None
    
    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key."""
        key_model = self.db.query(AccountApiKeyModel).filter_by(id=key_id).first()
        if not key_model:
            return False
        
        key_model.revoked_at = datetime.utcnow()
        self.db.commit()
        return True
    
    # Private helpers
    
    def _model_to_account(self, model: AccountModel) -> Account:
        """Convert database model to Account."""
        return Account(
            id=model.id,
            username=model.username,
            display_name=model.display_name,
            role=model.role.value,
            is_active=model.is_active,
            created_at=model.created_at.timestamp(),
            updated_at=model.updated_at.timestamp(),
            last_login=model.last_login.timestamp() if model.last_login else None
        )
