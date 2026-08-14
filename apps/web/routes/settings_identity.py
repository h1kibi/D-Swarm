"""Identity model settings route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.put("/identity")
async def put_identity_model(request: Request) -> Any:
    body = await _require_dict_body(request)
    try:
        cfg = request.app.state.manager.worker_config.set_identity_model(
            seats=body.get("seats"),
            credentials=body.get("credentials"),
            environments=body.get("environments"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "config": cfg}
