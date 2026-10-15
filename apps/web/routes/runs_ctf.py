"""Run management API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional, List

from apps.web.db.session import get_db
from apps.web.db.models import AccountModel
from apps.web.services.run_service import RunService
from apps.web.accounts.db_service import DbAccountService
from pydantic import BaseModel


# Pydantic models
class RunCreate(BaseModel):
    """Create run request."""
    target_id: str
    event_id: str


class RunUpdate(BaseModel):
    """Update run request."""
    status: Optional[str] = None
    solved: Optional[bool] = None
    flags_found: Optional[List[str]] = None
    facts: Optional[List[dict]] = None


class TokenUsage(BaseModel):
    """Token usage data."""
    provider: str
    tokens: int


class RunResponse(BaseModel):
    """Run response."""
    id: str
    target_id: str
    event_id: str
    created_by: str
    created_at: float
    status: str
    solved: bool
    flags_found: List[str]
    facts_count: int
    token_usage: dict
    is_deleted: bool


# Router
router = APIRouter(prefix="/api/runs", tags=["runs"])


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
async def create_run(
    run_input: RunCreate,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    """Create new solve attempt run."""
    service = RunService(db)
    
    try:
        # Check concurrent run limit
        if service.check_concurrent_limit(account.id, limit=3):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Concurrent run limit (3) exceeded"
            )
        
        created = service.create_run(
            target_id=run_input.target_id,
            event_id=run_input.event_id,
            creator_id=account.id
        )
        
        return RunResponse(
            id=created.id,
            target_id=created.target_id,
            event_id=created.event_id,
            created_by=created.created_by,
            created_at=created.created_at.timestamp(),
            status=created.status,
            solved=created.solved,
            flags_found=created.flags_found,
            facts_count=len(created.facts),
            token_usage=created.token_usage,
            is_deleted=created.is_deleted
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("")
async def list_runs(
    target_id: Optional[str] = None,
    event_id: Optional[str] = None,
    creator_id: Optional[str] = None,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> dict:
    """List runs with optional filtering."""
    service = RunService(db)
    
    # Only allow filtering own runs unless admin
    if account.role.value != "admin" and creator_id and creator_id != account.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    
    # Default to listing own runs if not admin
    if account.role.value != "admin" and not creator_id:
        creator_id = account.id
    
    runs, total = service.list_runs(
        target_id=target_id,
        event_id=event_id,
        creator_id=creator_id,
        status=status_filter,
        skip=skip,
        limit=limit
    )
    
    return {
        "runs": [
            RunResponse(
                id=r.id,
                target_id=r.target_id,
                event_id=r.event_id,
                created_by=r.created_by,
                created_at=r.created_at.timestamp(),
                status=r.status,
                solved=r.solved,
                flags_found=r.flags_found,
                facts_count=len(r.facts),
                token_usage=r.token_usage,
                is_deleted=r.is_deleted
            )
            for r in runs
        ],
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    """Get run by ID (with permission check)."""
    service = RunService(db)
    run = service.get_run_with_permission_check(run_id, account.id)
    
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    return RunResponse(
        id=run.id,
        target_id=run.target_id,
        event_id=run.event_id,
        created_by=run.created_by,
        created_at=run.created_at.timestamp(),
        status=run.status,
        solved=run.solved,
        flags_found=run.flags_found,
        facts_count=len(run.facts),
        token_usage=run.token_usage,
        is_deleted=run.is_deleted
    )


@router.patch("/{run_id}")
async def update_run(
    run_id: str,
    update: RunUpdate,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    """Update run status and results."""
    service = RunService(db)
    run = service.get_run_with_permission_check(run_id, account.id)
    
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    try:
        updates = {}
        if update.status:
            updates["status"] = update.status
        if update.solved is not None:
            updates["solved"] = update.solved
        if update.flags_found:
            updates["flags_found"] = update.flags_found
        if update.facts:
            updates["facts"] = update.facts
        
        updated = service.update_run_status(run_id, **updates)
        
        return RunResponse(
            id=updated.id,
            target_id=updated.target_id,
            event_id=updated.event_id,
            created_by=updated.created_by,
            created_at=updated.created_at.timestamp(),
            status=updated.status,
            solved=updated.solved,
            flags_found=updated.flags_found,
            facts_count=len(updated.facts),
            token_usage=updated.token_usage,
            is_deleted=updated.is_deleted
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.post("/{run_id}/tokens", status_code=status.HTTP_200_OK)
async def record_token_usage(
    run_id: str,
    usage: TokenUsage,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    """Record token usage for a run."""
    service = RunService(db)
    run = service.get_run_with_permission_check(run_id, account.id)
    
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    try:
        updated = service.add_token_usage(run_id, usage.provider, usage.tokens)
        
        return RunResponse(
            id=updated.id,
            target_id=updated.target_id,
            event_id=updated.event_id,
            created_by=updated.created_by,
            created_at=updated.created_at.timestamp(),
            status=updated.status,
            solved=updated.solved,
            flags_found=updated.flags_found,
            facts_count=len(updated.facts),
            token_usage=updated.token_usage,
            is_deleted=updated.is_deleted
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("/{run_id}/tokens")
async def get_token_usage(
    run_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> dict:
    """Get token usage for a run."""
    service = RunService(db)
    run = service.get_run_with_permission_check(run_id, account.id)
    
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    return {
        "run_id": run_id,
        "token_usage": run.token_usage,
        "total_tokens": sum(run.token_usage.values())
    }


@router.post("/{run_id}/stop", status_code=status.HTTP_200_OK)
async def stop_run(
    run_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
) -> RunResponse:
    """Stop a running solve attempt."""
    service = RunService(db)
    run = service.get_run_with_permission_check(run_id, account.id)
    
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    
    try:
        stopped = service.stop_run(run_id)
        
        return RunResponse(
            id=stopped.id,
            target_id=stopped.target_id,
            event_id=stopped.event_id,
            created_by=stopped.created_by,
            created_at=stopped.created_at.timestamp(),
            status=stopped.status,
            solved=stopped.solved,
            flags_found=stopped.flags_found,
            facts_count=len(stopped.facts),
            token_usage=stopped.token_usage,
            is_deleted=stopped.is_deleted
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db)
):
    """Delete run (soft delete)."""
    service = RunService(db)
    
    try:
        service.delete_run(run_id, account.id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
