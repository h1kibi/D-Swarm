"""Cross-resource search (events, targets, runs)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from apps.web.db.models import AccountModel, EventModel, RunModel, TargetModel
from apps.web.db.session import get_db
from apps.web.accounts.db_service import DbAccountService

router = APIRouter(prefix="/api/search", tags=["search"])


def get_current_account(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> AccountModel:
    """Get current authenticated account (Authorization: Bearer <token>)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    payload = DbAccountService(db).verify_access_token(authorization[7:], "access")
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    account = db.query(AccountModel).filter_by(id=payload["account_id"]).first()
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    return account


@router.get("")
async def search(
    q: str,
    type: Optional[str] = None,
    limit: int = 20,
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> dict:
    """Search events, targets, and runs by substring (case-insensitive)."""
    query = (q or "").strip()
    if len(query) < 2:
        return {"results": []}

    like = f"%{query}%"
    wanted = type if type in ("event", "target", "run") else None
    results: list[dict] = []

    if wanted in (None, "event"):
        rows = (
            db.query(EventModel)
            .filter(or_(EventModel.name.ilike(like), EventModel.description.ilike(like)))
            .limit(limit)
            .all()
        )
        for event in rows:
            results.append({
                "id": event.id,
                "type": "event",
                "title": event.name,
                "description": event.description or "",
                "metadata": {"status": event.status, "created_by": event.created_by},
            })

    if wanted in (None, "target"):
        rows = (
            db.query(TargetModel)
            .filter(or_(TargetModel.name.ilike(like), TargetModel.description.ilike(like)))
            .limit(limit)
            .all()
        )
        for target in rows:
            results.append({
                "id": target.id,
                "type": "target",
                "title": target.name,
                "description": target.description or "",
                "metadata": {
                    "category": target.category,
                    "event_id": target.event_id,
                    "is_solved": target.is_solved,
                },
            })

    if wanted in (None, "run"):
        rows = db.query(RunModel).filter(RunModel.id.ilike(like)).limit(limit).all()
        for run in rows:
            results.append({
                "id": run.id,
                "type": "run",
                "title": f"Run {run.id[:8]}",
                "description": f"status={run.status}",
                "metadata": {
                    "target_id": run.target_id,
                    "event_id": run.event_id,
                    "status": run.status,
                },
            })

    return {"results": results[:limit]}
