"""Event management service layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy.orm import Session

from apps.web.db.models import EventModel, TargetModel, AccountModel
from apps.web.accounts.models import Account


class EventService:
    """Service for Event CRUD and management."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def create_event(
        self,
        name: str,
        creator_id: str,
        description: Optional[str] = None
    ) -> EventModel:
        """Create a new event (admin only)."""
        # Verify creator is admin
        creator = self.db.query(AccountModel).filter_by(id=creator_id).first()
        if not creator or creator.role.value != "admin":
            raise PermissionError("Only admins can create events")
        
        event = EventModel(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            created_by=creator_id,
            status="active"
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return event
    
    def get_event(self, event_id: str) -> Optional[EventModel]:
        """Get event by ID."""
        return self.db.query(EventModel).filter_by(id=event_id).first()
    
    def list_events(
        self,
        skip: int = 0,
        limit: int = 100,
        status: Optional[str] = None
    ) -> tuple[List[EventModel], int]:
        """List all events with pagination."""
        query = self.db.query(EventModel)
        
        if status:
            query = query.filter_by(status=status)
        
        total = query.count()
        events = query.offset(skip).limit(limit).all()
        
        return events, total
    
    def update_event(
        self,
        event_id: str,
        updater_id: str,
        **kwargs
    ) -> EventModel:
        """Update event (admin only)."""
        # Verify updater is admin
        updater = self.db.query(AccountModel).filter_by(id=updater_id).first()
        if not updater or updater.role.value != "admin":
            raise PermissionError("Only admins can update events")
        
        event = self.get_event(event_id)
        if not event:
            raise ValueError(f"Event {event_id} not found")
        
        for key, value in kwargs.items():
            if key in ["name", "description", "status"]:
                setattr(event, key, value)
        
        event.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(event)
        return event
    
    def archive_event(self, event_id: str, archiver_id: str) -> EventModel:
        """Archive an event (admin only)."""
        event = self.update_event(event_id, archiver_id, status="archived")
        event.archived_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(event)
        return event
    
    def delete_event(self, event_id: str, deleter_id: str) -> bool:
        """Delete event (admin only)."""
        # Verify deleter is admin
        deleter = self.db.query(AccountModel).filter_by(id=deleter_id).first()
        if not deleter or deleter.role.value != "admin":
            raise PermissionError("Only admins can delete events")
        
        event = self.get_event(event_id)
        if not event:
            raise ValueError(f"Event {event_id} not found")
        
        self.db.delete(event)
        self.db.commit()
        return True
