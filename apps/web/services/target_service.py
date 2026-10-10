"""Target management service layer."""

from __future__ import annotations

import uuid
import json
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

from apps.web.db.models import (
    TargetModel, EventModel, TargetSnapshotModel, AccountModel
)


class TargetService:
    """Service for Target CRUD with creator-based permissions."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_target(
        self,
        event_id: str,
        name: str,
        creator_id: str,
        description: Optional[str] = None,
        category: Optional[str] = None,
        target_address: Optional[str] = None,
        attachments: Optional[List[str]] = None,
        expected_flags: int = 1,
        flag_format_hint: Optional[str] = None
    ) -> TargetModel:
        """Create a new target within an event."""
        # Verify event exists
        event = self.db.query(EventModel).filter_by(id=event_id).first()
        if not event:
            raise ValueError(f"Event {event_id} not found")
        
        # Verify creator exists and is member or admin
        creator = self.db.query(AccountModel).filter_by(id=creator_id).first()
        if not creator or not creator.is_active:
            raise ValueError(f"Creator {creator_id} not found or inactive")
        
        target = TargetModel(
            id=str(uuid.uuid4()),
            event_id=event_id,
            name=name,
            description=description,
            category=category,
            target_address=target_address,
            attachments=attachments or [],
            expected_flags=expected_flags,
            flag_format_hint=flag_format_hint,
            created_by=creator_id
        )
        
        self.db.add(target)
        self.db.flush()
        
        # Create initial snapshot
        self._create_snapshot(
            target_id=target.id,
            edited_by=creator_id,
            operation="create",
            changes={},
            snapshot=self._target_to_dict(target)
        )
        
        self.db.commit()
        self.db.refresh(target)
        return target
    
    def get_target(self, target_id: str) -> Optional[TargetModel]:
        """Get target by ID."""
        return self.db.query(TargetModel).filter_by(id=target_id).first()
    
    def list_targets(
        self,
        event_id: Optional[str] = None,
        creator_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[TargetModel], int]:
        """List targets with optional filtering."""
        query = self.db.query(TargetModel)
        
        if event_id:
            query = query.filter_by(event_id=event_id)
        
        if creator_id:
            query = query.filter_by(created_by=creator_id)
        
        total = query.count()
        targets = query.offset(skip).limit(limit).all()
        
        return targets, total
    
    def update_target(
        self,
        target_id: str,
        updater_id: str,
        **kwargs
    ) -> TargetModel:
        """Update target (creator or admin)."""
        target = self.get_target(target_id)
        if not target:
            raise ValueError(f"Target {target_id} not found")
        
        # Check permissions: creator or admin
        updater = self.db.query(AccountModel).filter_by(id=updater_id).first()
        if not updater:
            raise ValueError(f"Updater {updater_id} not found")
        
        is_admin = updater.role.value == "admin"
        is_creator = target.created_by == updater_id
        
        if not (is_admin or is_creator):
            raise PermissionError("Only creator or admin can update target")
        
        # Track changes
        old_state = self._target_to_dict(target)
        changes = {}
        
        for key, value in kwargs.items():
            if key in ["name", "description", "category", "target_address", 
                      "attachments", "expected_flags", "flag_format_hint"]:
                old_value = getattr(target, key)
                if old_value != value:
                    changes[key] = {"old": old_value, "new": value}
                    setattr(target, key, value)
        
        target.updated_at = datetime.utcnow()
        self.db.flush()
        
        # Create snapshot if changes
        if changes:
            self._create_snapshot(
                target_id=target_id,
                edited_by=updater_id,
                operation="update",
                changes=changes,
                snapshot=self._target_to_dict(target)
            )
        
        self.db.commit()
        self.db.refresh(target)
        return target
    
    def add_flag(
        self,
        target_id: str,
        flag: str,
        found_by: str
    ) -> TargetModel:
        """Add a flag to target."""
        target = self.get_target(target_id)
        if not target:
            raise ValueError(f"Target {target_id} not found")
        
        if flag not in target.flags:
            target.flags.append(flag)
        
        if not target.is_solved and len(target.flags) >= target.expected_flags:
            target.is_solved = True
            target.solved_by = found_by
            target.solved_at = datetime.utcnow()
        
        target.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(target)
        return target
    
    def add_fact(
        self,
        target_id: str,
        fact: dict,
        discovered_by: str
    ) -> TargetModel:
        """Add a discovered fact to target."""
        target = self.get_target(target_id)
        if not target:
            raise ValueError(f"Target {target_id} not found")
        
        fact_entry = {
            "id": str(uuid.uuid4()),
            "content": fact,
            "discovered_by": discovered_by,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        target.facts.append(fact_entry)
        target.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(target)
        return target
    
    def get_target_snapshots(
        self,
        target_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[List[TargetSnapshotModel], int]:
        """Get target edit history."""
        query = self.db.query(TargetSnapshotModel).filter_by(target_id=target_id)
        total = query.count()
        snapshots = query.order_by(TargetSnapshotModel.edited_at.desc()).offset(skip).limit(limit).all()
        
        return snapshots, total
    
    def delete_target(self, target_id: str, deleter_id: str) -> bool:
        """Delete target (creator or admin)."""
        target = self.get_target(target_id)
        if not target:
            raise ValueError(f"Target {target_id} not found")
        
        # Check permissions
        deleter = self.db.query(AccountModel).filter_by(id=deleter_id).first()
        if not deleter:
            raise ValueError(f"Deleter {deleter_id} not found")
        
        is_admin = deleter.role.value == "admin"
        is_creator = target.created_by == deleter_id
        
        if not (is_admin or is_creator):
            raise PermissionError("Only creator or admin can delete target")
        
        self.db.delete(target)
        self.db.commit()
        return True
    
    # Private helpers
    
    def _target_to_dict(self, target: TargetModel) -> dict:
        """Convert target to dictionary for snapshots."""
        return {
            "id": target.id,
            "event_id": target.event_id,
            "name": target.name,
            "description": target.description,
            "category": target.category,
            "target_address": target.target_address,
            "attachments": target.attachments,
            "expected_flags": target.expected_flags,
            "flag_format_hint": target.flag_format_hint,
            "is_solved": target.is_solved,
            "flags": target.flags,
            "facts_count": len(target.facts)
        }
    
    def _create_snapshot(
        self,
        target_id: str,
        edited_by: str,
        operation: str,
        changes: dict,
        snapshot: dict
    ):
        """Create a target snapshot."""
        snap = TargetSnapshotModel(
            id=str(uuid.uuid4()),
            target_id=target_id,
            edited_by=edited_by,
            operation=operation,
            changes=changes,
            snapshot=snapshot
        )
        self.db.add(snap)
