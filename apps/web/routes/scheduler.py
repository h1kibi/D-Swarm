"""Run scheduler routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("")
async def get_scheduler(request: Request) -> Any:
    return request.app.state.manager.scheduler_snapshot()


@router.put("")
async def put_scheduler(request: Request) -> Any:
    body = await _require_dict_body(request)
    mgr = request.app.state.manager
    try:
        n = int(body.get("max_concurrent_runs") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "max_concurrent_runs must be an integer"},
                            status_code=400)
    limit = mgr.set_scheduler_limit(n)
    await mgr.dispatch_pending()
    return mgr.scheduler_snapshot()
