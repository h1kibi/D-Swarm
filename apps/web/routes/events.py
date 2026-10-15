"""Event management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional

from apps.web.db.session import get_db
from apps.web.db.models import AccountModel
from apps.web.services.event_service import EventService
from apps.web.accounts.db_service import DbAccountService
from pydantic import BaseModel


# Pydantic models
class EventCreate(BaseModel):
    """Create event request."""
    name: str
    description: Optional[str] = None


class EventUpdate(BaseModel):
    """Update event request."""
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class EventResponse(BaseModel):
    """Event response."""
    id: str
    name: str
    description: Optional[str]
    status: str
    created_by: str
    created_at: float
    updated_at: float
    archived_at: Optional[float]


# Router
router = APIRouter(prefix="/api/events", tags=["events"])


def get_current_account(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> AccountModel:
    """Get current authenticated account (Authorization: Bearer <token>)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    token = authorization[7:]
    service = DbAccountService(db)
    payload = service.verify_access_token(token, "access")
    
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    account = db.query(AccountModel).filter_by(id=payload["account_id"]).first()
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    
    return account


def require_admin(account: AccountModel = Depends(get_current_account)) -> AccountModel:
    """Require admin role."""
    if account.role.value != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return account


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(
    event: EventCreate,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> EventResponse:
    """Create new event (admin only)."""
    service = EventService(db)
    
    try:
        created = service.create_event(
            name=event.name,
            creator_id=admin.id,
            description=event.description
        )
        
        return EventResponse(
            id=created.id,
            name=created.name,
            description=created.description,
            status=created.status,
            created_by=created.created_by,
            created_at=created.created_at.timestamp(),
            updated_at=created.updated_at.timestamp(),
            archived_at=created.archived_at.timestamp() if created.archived_at else None
        )
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.get("")
async def list_events(
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[str] = None,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> dict:
    """List all events (paginated)."""
    service = EventService(db)
    events, total = service.list_events(skip=skip, limit=limit, status=status_filter)
    
    return {
        "events": [
            EventResponse(
                id=e.id,
                name=e.name,
                description=e.description,
                status=e.status,
                created_by=e.created_by,
                created_at=e.created_at.timestamp(),
                updated_at=e.updated_at.timestamp(),
                archived_at=e.archived_at.timestamp() if e.archived_at else None
            )
            for e in events
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/{event_id}")
async def get_event(
    event_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> EventResponse:
    """Get event by ID."""
    service = EventService(db)
    event = service.get_event(event_id)
    
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    return EventResponse(
        id=event.id,
        name=event.name,
        description=event.description,
        status=event.status,
        created_by=event.created_by,
        created_at=event.created_at.timestamp(),
        updated_at=event.updated_at.timestamp(),
        archived_at=event.archived_at.timestamp() if event.archived_at else None
    )


@router.patch("/{event_id}")
async def update_event(
    event_id: str,
    update: EventUpdate,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
) -> EventResponse:
    """Update event (admin only)."""
    service = EventService(db)
    
    try:
        updates = {k: v for k, v in update.dict().items() if v is not None}
        updated = service.update_event(event_id, admin.id, **updates)
        
        return EventResponse(
            id=updated.id,
            name=updated.name,
            description=updated.description,
            status=updated.status,
            created_by=updated.created_by,
            created_at=updated.created_at.timestamp(),
            updated_at=updated.updated_at.timestamp(),
            archived_at=updated.archived_at.timestamp() if updated.archived_at else None
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: str,
    admin: AccountModel = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete event (admin only)."""
    service = EventService(db)
    
    try:
        service.delete_event(event_id, admin.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
