"""Target management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional, List

from apps.web.db.session import get_db
from apps.web.db.models import AccountModel
from apps.web.services.target_service import TargetService
from apps.web.accounts.db_service import DbAccountService
from pydantic import BaseModel


# Pydantic models
class TargetCreate(BaseModel):
    """Create target request."""
    event_id: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    target_address: Optional[str] = None
    attachments: Optional[List[str]] = None
    expected_flags: int = 1
    flag_format_hint: Optional[str] = None


class TargetUpdate(BaseModel):
    """Update target request."""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    target_address: Optional[str] = None
    attachments: Optional[List[str]] = None
    expected_flags: Optional[int] = None
    flag_format_hint: Optional[str] = None


class TargetResponse(BaseModel):
    """Target response."""
    id: str
    event_id: str
    name: str
    description: Optional[str]
    category: Optional[str]
    target_address: Optional[str]
    attachments: List[str]
    expected_flags: int
    flag_format_hint: Optional[str]
    is_solved: bool
    solved_by: Optional[str]
    solved_at: Optional[float]
    flags: List[str]
    facts_count: int
    created_by: str
    created_at: float
    updated_at: float


class FlagSubmission(BaseModel):
    """Flag submission request."""
    flag: str


class FactSubmission(BaseModel):
    """Fact submission request."""
    fact: dict


class SnapshotResponse(BaseModel):
    """Target snapshot response."""
    id: str
    target_id: str
    edited_by: str
    edited_at: float
    operation: str
    changes: dict
    snapshot: dict


# Router
router = APIRouter(prefix="/api/targets", tags=["targets"])


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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_target(
    target: TargetCreate,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    """Create new target within an event."""
    service = TargetService(db)
    
    try:
        created = service.create_target(
            event_id=target.event_id,
            name=target.name,
            creator_id=account.id,
            description=target.description,
            category=target.category,
            target_address=target.target_address,
            attachments=target.attachments,
            expected_flags=target.expected_flags,
            flag_format_hint=target.flag_format_hint
        )
        
        return TargetResponse(
            id=created.id,
            event_id=created.event_id,
            name=created.name,
            description=created.description,
            category=created.category,
            target_address=created.target_address,
            attachments=created.attachments,
            expected_flags=created.expected_flags,
            flag_format_hint=created.flag_format_hint,
            is_solved=created.is_solved,
            solved_by=created.solved_by,
            solved_at=created.solved_at.timestamp() if created.solved_at else None,
            flags=created.flags,
            facts_count=len(created.facts),
            created_by=created.created_by,
            created_at=created.created_at.timestamp(),
            updated_at=created.updated_at.timestamp()
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("")
async def list_targets(
    event_id: Optional[str] = None,
    creator_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> dict:
    """List targets with optional filtering."""
    service = TargetService(db)
    targets, total = service.list_targets(
        event_id=event_id,
        creator_id=creator_id,
        skip=skip,
        limit=limit
    )
    
    return {
        "targets": [
            TargetResponse(
                id=t.id,
                event_id=t.event_id,
                name=t.name,
                description=t.description,
                category=t.category,
                target_address=t.target_address,
                attachments=t.attachments,
                expected_flags=t.expected_flags,
                flag_format_hint=t.flag_format_hint,
                is_solved=t.is_solved,
                solved_by=t.solved_by,
                solved_at=t.solved_at.timestamp() if t.solved_at else None,
                flags=t.flags,
                facts_count=len(t.facts),
                created_by=t.created_by,
                created_at=t.created_at.timestamp(),
                updated_at=t.updated_at.timestamp()
            )
            for t in targets
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/{target_id}")
async def get_target(
    target_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    """Get target by ID."""
    service = TargetService(db)
    target = service.get_target(target_id)
    
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    return TargetResponse(
        id=target.id,
        event_id=target.event_id,
        name=target.name,
        description=target.description,
        category=target.category,
        target_address=target.target_address,
        attachments=target.attachments,
        expected_flags=target.expected_flags,
        flag_format_hint=target.flag_format_hint,
        is_solved=target.is_solved,
        solved_by=target.solved_by,
        solved_at=target.solved_at.timestamp() if target.solved_at else None,
        flags=target.flags,
        facts_count=len(target.facts),
        created_by=target.created_by,
        created_at=target.created_at.timestamp(),
        updated_at=target.updated_at.timestamp()
    )


@router.patch("/{target_id}")
async def update_target(
    target_id: str,
    update: TargetUpdate,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    """Update target (creator or admin only)."""
    service = TargetService(db)
    
    try:
        updates = {k: v for k, v in update.dict().items() if v is not None}
        updated = service.update_target(target_id, account.id, **updates)
        
        return TargetResponse(
            id=updated.id,
            event_id=updated.event_id,
            name=updated.name,
            description=updated.description,
            category=updated.category,
            target_address=updated.target_address,
            attachments=updated.attachments,
            expected_flags=updated.expected_flags,
            flag_format_hint=updated.flag_format_hint,
            is_solved=updated.is_solved,
            solved_by=updated.solved_by,
            solved_at=updated.solved_at.timestamp() if updated.solved_at else None,
            flags=updated.flags,
            facts_count=len(updated.facts),
            created_by=updated.created_by,
            created_at=updated.created_at.timestamp(),
            updated_at=updated.updated_at.timestamp()
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@router.post("/{target_id}/flags", status_code=status.HTTP_201_CREATED)
async def add_flag(
    target_id: str,
    submission: FlagSubmission,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    """Add a flag to target."""
    service = TargetService(db)
    
    try:
        target = service.add_flag(target_id, submission.flag, account.id)
        
        return TargetResponse(
            id=target.id,
            event_id=target.event_id,
            name=target.name,
            description=target.description,
            category=target.category,
            target_address=target.target_address,
            attachments=target.attachments,
            expected_flags=target.expected_flags,
            flag_format_hint=target.flag_format_hint,
            is_solved=target.is_solved,
            solved_by=target.solved_by,
            solved_at=target.solved_at.timestamp() if target.solved_at else None,
            flags=target.flags,
            facts_count=len(target.facts),
            created_by=target.created_by,
            created_at=target.created_at.timestamp(),
            updated_at=target.updated_at.timestamp()
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/{target_id}/facts", status_code=status.HTTP_201_CREATED)
async def add_fact(
    target_id: str,
    submission: FactSubmission,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> TargetResponse:
    """Add a discovered fact to target."""
    service = TargetService(db)
    
    try:
        target = service.add_fact(target_id, submission.fact, account.id)
        
        return TargetResponse(
            id=target.id,
            event_id=target.event_id,
            name=target.name,
            description=target.description,
            category=target.category,
            target_address=target.target_address,
            attachments=target.attachments,
            expected_flags=target.expected_flags,
            flag_format_hint=target.flag_format_hint,
            is_solved=target.is_solved,
            solved_by=target.solved_by,
            solved_at=target.solved_at.timestamp() if target.solved_at else None,
            flags=target.flags,
            facts_count=len(target.facts),
            created_by=target.created_by,
            created_at=target.created_at.timestamp(),
            updated_at=target.updated_at.timestamp()
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("/{target_id}/snapshots")
async def get_snapshots(
    target_id: str,
    skip: int = 0,
    limit: int = 100,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> dict:
    """Get target edit history snapshots."""
    service = TargetService(db)
    snapshots, total = service.get_target_snapshots(target_id, skip, limit)
    
    return {
        "snapshots": [
            SnapshotResponse(
                id=s.id,
                target_id=s.target_id,
                edited_by=s.edited_by,
                edited_at=s.edited_at.timestamp(),
                operation=s.operation,
                changes=s.changes,
                snapshot=s.snapshot
            )
            for s in snapshots
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
):
    """Delete target (creator or admin only)."""
    service = TargetService(db)
    
    try:
        service.delete_target(target_id, account.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
