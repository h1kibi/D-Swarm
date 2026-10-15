"""Token usage analytics."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from apps.web.db.models import AccountModel, TokenUsageLogModel
from apps.web.db.session import get_db
from apps.web.accounts.db_service import DbAccountService

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


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


def _scope(query, account: AccountModel):
    """Admins see all usage; members see only their own."""
    if account.role.value != "admin":
        return query.filter(TokenUsageLogModel.account_id == account.id)
    return query


@router.get("/tokens")
async def token_analytics(
    account: AccountModel = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> dict:
    """Aggregate token usage per provider with a week-over-week trend."""
    base = db.query(
        TokenUsageLogModel.provider,
        func.sum(TokenUsageLogModel.tokens_used),
        func.count(func.distinct(TokenUsageLogModel.run_id)),
    )
    rows = _scope(base, account).group_by(TokenUsageLogModel.provider).all()

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    fortnight_ago = now - timedelta(days=14)

    stats: list[dict] = []
    total_tokens = 0

    for provider, tokens, run_count in rows:
        tokens = int(tokens or 0)
        run_count = int(run_count or 0)
        total_tokens += tokens

        def window_sum(start: datetime, end: Optional[datetime] = None) -> int:
            q = db.query(func.coalesce(func.sum(TokenUsageLogModel.tokens_used), 0)).filter(
                TokenUsageLogModel.provider == provider,
                TokenUsageLogModel.timestamp >= start,
            )
            if end is not None:
                q = q.filter(TokenUsageLogModel.timestamp < end)
            return int(_scope(q, account).scalar() or 0)

        recent = window_sum(week_ago)
        prior = window_sum(fortnight_ago, week_ago)
        trend = ((recent - prior) / prior * 100.0) if prior else 0.0

        stats.append({
            "provider": provider,
            "total_tokens": tokens,
            "total_cost": 0.0,  # no pricing table configured
            "runs_count": run_count,
            "avg_per_run": (tokens / run_count) if run_count else 0.0,
            "trend": trend,
        })

    return {"stats": stats, "total_tokens": total_tokens, "total_cost": 0.0}
