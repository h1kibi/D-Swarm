from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.run_observability import project_run_observability

router = APIRouter(prefix="/api/runs", tags=["run-observability"])


@router.get("/{run_id}/observability")
async def get_run_observability(run_id: str, request: Request) -> dict[str, Any]:
    run = request.app.state.manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    return project_run_observability(run)


__all__ = ["router"]
