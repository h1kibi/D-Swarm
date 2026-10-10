"""Run management service layer for privacy and token tracking."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

from apps.web.db.models import RunModel, TargetModel, EventModel, AccountModel


class RunService:
    """Service for Run management with privacy and token tracking."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_run(
        self,
        target_id: str,
        event_id: str,
        creator_id: str
    ) -> RunModel:
        """Create a new solve attempt run."""
        # Verify target and event exist
        target = self.db.query(TargetModel).filter_by(id=target_id).first()
        if not target:
            raise ValueError(f"Target {target_id} not found")
        
        event = self.db.query(EventModel).filter_by(id=event_id).first()
        if not event:
            raise ValueError(f"Event {event_id} not found")
        
        # Verify creator exists and is active
        creator = self.db.query(AccountModel).filter_by(id=creator_id).first()
        if not creator or not creator.is_active:
            raise ValueError(f"Creator {creator_id} not found or inactive")
        
        run = RunModel(
            id=str(uuid.uuid4()),
            target_id=target_id,
            event_id=event_id,
            created_by=creator_id,
            status="running"
        )
        
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run
    
    def get_run(self, run_id: str) -> Optional[RunModel]:
        """Get run by ID."""
        return self.db.query(RunModel).filter_by(id=run_id).first()
    
    def get_run_with_permission_check(
        self,
        run_id: str,
        account_id: str
    ) -> Optional[RunModel]:
        """Get run only if account is creator or admin."""
        run = self.get_run(run_id)
        if not run:
            return None
        
        account = self.db.query(AccountModel).filter_by(id=account_id).first()
        if not account:
            return None
        
        is_admin = account.role.value == "admin"
        is_creator = run.created_by == account_id
        
        if not (is_admin or is_creator):
            return None
        
        return run
    
    def list_runs(
        self,
        target_id: Optional[str] = None,
        event_id: Optional[str] = None,
        creator_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[RunModel], int]:
        """List runs with optional filtering."""
        query = self.db.query(RunModel).filter_by(is_deleted=False)
        
        if target_id:
            query = query.filter_by(target_id=target_id)
        
        if event_id:
            query = query.filter_by(event_id=event_id)
        
        if creator_id:
            query = query.filter_by(created_by=creator_id)
        
        if status:
            query = query.filter_by(status=status)
        
        total = query.count()
        runs = query.order_by(RunModel.created_at.desc()).offset(skip).limit(limit).all()
        
        return runs, total
    
    def update_run_status(
        self,
        run_id: str,
        status: str,
        solved: bool = False,
        flags_found: Optional[List[str]] = None,
        facts: Optional[List[dict]] = None
    ) -> RunModel:
        """Update run status and results."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        
        run.status = status
        if solved is not None:
            run.solved = solved
        
        if flags_found is not None:
            run.flags_found = flags_found
        
        if facts is not None:
            run.facts = facts
        
        self.db.commit()
        self.db.refresh(run)
        return run
    
    def add_token_usage(
        self,
        run_id: str,
        provider: str,
        tokens: int
    ) -> RunModel:
        """Record token usage for a run."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        
        if provider not in run.token_usage:
            run.token_usage[provider] = 0
        
        run.token_usage[provider] += tokens
        
        self.db.commit()
        self.db.refresh(run)
        return run
    
    def get_run_token_usage(self, run_id: str) -> dict:
        """Get total token usage for a run."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        
        return run.token_usage
    
    def stop_run(self, run_id: str) -> RunModel:
        """Stop a running solve attempt."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        
        run.status = "stopped"
        self.db.commit()
        self.db.refresh(run)
        return run
    
    def delete_run(self, run_id: str, deleter_id: str) -> bool:
        """Mark run as deleted (soft delete)."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")
        
        # Check permissions: creator or admin
        deleter = self.db.query(AccountModel).filter_by(id=deleter_id).first()
        if not deleter:
            raise ValueError(f"Deleter {deleter_id} not found")
        
        is_admin = deleter.role.value == "admin"
        is_creator = run.created_by == deleter_id
        
        if not (is_admin or is_creator):
            raise PermissionError("Only creator or admin can delete run")
        
        run.is_deleted = True
        self.db.commit()
        return True
    
    def get_account_run_count(self, account_id: str) -> int:
        """Get count of active runs for account."""
        return self.db.query(RunModel).filter_by(
            created_by=account_id,
            status="running",
            is_deleted=False
        ).count()
    
    def check_concurrent_limit(self, account_id: str, limit: int = 3) -> bool:
        """Check if account has exceeded concurrent run limit."""
        count = self.get_account_run_count(account_id)
        return count >= limit
