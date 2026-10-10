"""Audit logging service and middleware."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

from apps.web.db.models import AuditLogModel, AccountModel


class AuditService:
    """Service for audit logging and queries."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def log_action(
        self,
        action: str,
        actor_id: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        details: Optional[dict] = None,
        ip_address: Optional[str] = None,
        status: str = "success"
    ) -> AuditLogModel:
        """Log an action to the audit trail."""
        log_entry = AuditLogModel(
            id=str(uuid.uuid4()),
            action=action,
            actor=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip_address=ip_address,
            status=status
        )
        
        self.db.add(log_entry)
        self.db.commit()
        self.db.refresh(log_entry)
        return log_entry
    
    def get_logs(
        self,
        action: Optional[str] = None,
        actor_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[AuditLogModel], int]:
        """Query audit logs with optional filtering."""
        query = self.db.query(AuditLogModel)
        
        if action:
            query = query.filter_by(action=action)
        
        if actor_id:
            query = query.filter_by(actor=actor_id)
        
        if resource_type:
            query = query.filter_by(resource_type=resource_type)
        
        if resource_id:
            query = query.filter_by(resource_id=resource_id)
        
        if status:
            query = query.filter_by(status=status)
        
        total = query.count()
        logs = query.order_by(AuditLogModel.timestamp.desc()).offset(skip).limit(limit).all()
        
        return logs, total
    
    def get_account_actions(
        self,
        account_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[AuditLogModel], int]:
        """Get all actions by a specific account."""
        return self.get_logs(actor_id=account_id, skip=skip, limit=limit)
    
    def get_resource_history(
        self,
        resource_type: str,
        resource_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[AuditLogModel], int]:
        """Get audit history for a specific resource."""
        return self.get_logs(
            resource_type=resource_type,
            resource_id=resource_id,
            skip=skip,
            limit=limit
        )
    
    def get_failed_actions(
        self,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[AuditLogModel], int]:
        """Get all failed actions (for security review)."""
        return self.get_logs(status="failure", skip=skip, limit=limit)
    
    def get_account_statistics(self, account_id: str) -> dict:
        """Get statistics for account audit trail."""
        query = self.db.query(AuditLogModel).filter_by(actor=account_id)
        
        total_actions = query.count()
        successful_actions = query.filter_by(status="success").count()
        failed_actions = query.filter_by(status="failure").count()
        
        # Count by action type
        action_counts = {}
        for log in query.all():
            action_counts[log.action] = action_counts.get(log.action, 0) + 1
        
        # Get last action
        last_action = query.order_by(AuditLogModel.timestamp.desc()).first()
        
        return {
            "account_id": account_id,
            "total_actions": total_actions,
            "successful_actions": successful_actions,
            "failed_actions": failed_actions,
            "failure_rate": failed_actions / total_actions if total_actions > 0 else 0,
            "action_breakdown": action_counts,
            "last_action_timestamp": last_action.timestamp.isoformat() if last_action else None
        }
