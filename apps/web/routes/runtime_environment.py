"""Runtime environment settings route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/settings", tags=["runtime-environment"])


@router.put("/runtime-environment")
async def put_runtime_environment(request: Request) -> Any:
    body = await _require_dict_body(request)
    try:
        cfg = request.app.state.manager.worker_config.set_runtime_environment(
            backend=str(body.get("backend") or ""),
            runtime_id=str(body.get("runtime_id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "config": cfg}
