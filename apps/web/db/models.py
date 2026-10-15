"""Database models for CTF team platform using SQLAlchemy."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import Column, String, Integer, Float, Boolean, ForeignKey, Text, JSON, DateTime, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import enum

Base = declarative_base()


class AccountRole(str, enum.Enum):
    """Account roles."""
    ADMIN = "admin"
    MEMBER = "member"


class AccountModel(Base):
    """Account database model."""
    __tablename__ = "accounts"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(
        SQLEnum(AccountRole, values_callable=lambda enum_cls: [m.value for m in enum_cls]),
        nullable=False,
        default=AccountRole.MEMBER,
    )
    is_active = Column(Boolean, default=True, nullable=False)
    
    created_by = Column(String(36), ForeignKey("accounts.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    # Tokens issued before this instant are rejected (set on password change).
    token_valid_after = Column(DateTime, nullable=True)
    
    # Relationships
    api_keys = relationship("AccountApiKeyModel", back_populates="account", cascade="all, delete-orphan")
    events = relationship("EventModel", back_populates="created_by_account", cascade="all, delete-orphan")
    targets = relationship(
        "TargetModel",
        foreign_keys="TargetModel.created_by",
        back_populates="created_by_account",
        cascade="all, delete-orphan",
    )
    runs = relationship("RunModel", back_populates="created_by_account", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Account {self.username}>"


class AccountApiKeyModel(Base):
    """API key for external integrations."""
    __tablename__ = "account_api_keys"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    provider = Column(String(50), nullable=False)  # "local_agent"
    encrypted_key = Column(Text, nullable=False)
    
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)
    
    # Relationships
    account = relationship("AccountModel", back_populates="api_keys")
    
    def __repr__(self):
        return f"<ApiKey {self.id[:8]}... for {self.account_id[:8]}>"


class RevokedTokenModel(Base):
    """Denylisted account-JWT ids (logout / rotation).

    Rows are appended on logout and opportunistically pruned once their token's
    expiry has passed; the table stays small at team scale.
    """
    __tablename__ = "revoked_tokens"

    jti = Column(String(64), primary_key=True)
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=True, index=True)
    revoked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<RevokedToken {self.jti[:8]}...>"


class EventModel(Base):
    """CTF competition event."""
    __tablename__ = "ctf_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")  # draft, active, archived
    
    created_by = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)
    
    # Relationships
    created_by_account = relationship("AccountModel", back_populates="events")
    targets = relationship("TargetModel", back_populates="event", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Event {self.name}>"


class TargetModel(Base):
    """Challenge target within an event."""
    __tablename__ = "ctf_targets"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = Column(String(36), ForeignKey("ctf_events.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(50), nullable=True)  # crypto, web, pwn, misc, etc.
    
    target_address = Column(String(255), nullable=True)  # hostname:port or URL
    attachments = Column(JSON, default=list, nullable=False)  # list of file paths/URLs
    expected_flags = Column(Integer, default=1, nullable=False)
    flag_format_hint = Column(String(255), nullable=True)
    
    is_solved = Column(Boolean, default=False, nullable=False)
    solved_by = Column(String(36), ForeignKey("accounts.id"), nullable=True)
    solved_at = Column(DateTime, nullable=True)
    
    flags = Column(JSON, default=list, nullable=False)  # list of accepted flags
    facts = Column(JSON, default=list, nullable=False)  # list of discovered facts
    
    created_by = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    event = relationship("EventModel", back_populates="targets")
    created_by_account = relationship(
        "AccountModel", back_populates="targets", foreign_keys=[created_by]
    )
    runs = relationship("RunModel", back_populates="target", cascade="all, delete-orphan")
    snapshots = relationship("TargetSnapshotModel", back_populates="target", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Target {self.name}>"


class TargetSnapshotModel(Base):
    """Snapshot of target edit history."""
    __tablename__ = "target_snapshots"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_id = Column(String(36), ForeignKey("ctf_targets.id"), nullable=False, index=True)
    
    edited_by = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    edited_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    operation = Column(String(50), nullable=False)  # "create", "update", "delete_run", etc.
    changes = Column(JSON, nullable=False)  # {"field": {"old": ..., "new": ...}}
    snapshot = Column(JSON, nullable=False)  # complete target state
    
    # Relationships
    target = relationship("TargetModel", back_populates="snapshots")
    
    def __repr__(self):
        return f"<Snapshot {self.target_id[:8]}...>"


class RunModel(Base):
    """Single solve attempt."""
    __tablename__ = "ctf_runs"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_id = Column(String(36), ForeignKey("ctf_targets.id"), nullable=False, index=True)
    event_id = Column(String(36), ForeignKey("ctf_events.id"), nullable=False, index=True)
    
    created_by = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    
    status = Column(String(20), nullable=False, default="running")  # running, completed, failed, stopped
    solved = Column(Boolean, default=False, nullable=False)
    
    flags_found = Column(JSON, default=list, nullable=False)
    facts = Column(JSON, default=list, nullable=False)
    
    workspace_id = Column(String(255), nullable=True)
    container_id = Column(String(255), nullable=True)
    
    token_usage = Column(JSON, default=dict, nullable=False)  # {provider: count}
    
    logs_location = Column(String(255), nullable=True)
    artifacts = Column(JSON, default=list, nullable=False)
    
    is_deleted = Column(Boolean, default=False, nullable=False)
    
    # Relationships
    target = relationship("TargetModel", back_populates="runs")
    created_by_account = relationship("AccountModel", back_populates="runs")
    
    def __repr__(self):
        return f"<Run {self.id[:8]}...>"


class AuditLogModel(Base):
    """Audit trail for all operations."""
    __tablename__ = "audit_logs"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    
    action = Column(String(50), nullable=False, index=True)  # "account_create", "target_edit", etc.
    actor = Column(String(36), ForeignKey("accounts.id"), nullable=False)
    
    resource_type = Column(String(50), nullable=True)  # "target", "run", "event"
    resource_id = Column(String(36), nullable=True, index=True)
    
    details = Column(JSON, nullable=False)  # Operation-specific data
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    ip_address = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="success")  # success, failure
    
    def __repr__(self):
        return f"<AuditLog {self.action}>"


class TokenUsageLogModel(Base):
    """Per-account token consumption tracking."""
    __tablename__ = "token_usage_logs"
    
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id = Column(String(36), ForeignKey("accounts.id"), nullable=False, index=True)
    
    provider = Column(String(50), nullable=False)  # "openai", "anthropic", etc.
    model = Column(String(100), nullable=True)
    tokens_used = Column(Integer, nullable=False)
    
    run_id = Column(String(36), ForeignKey("ctf_runs.id"), nullable=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    
    def __repr__(self):
        return f"<TokenUsage {self.account_id[:8]}... {self.tokens_used}>"
