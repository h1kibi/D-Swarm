"""Worker model option and probe routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/settings", tags=["worker-models"])


@router.get("/worker-models")
async def get_worker_models() -> Any:
    from apps.web.worker_models import worker_model_options_payload

    return worker_model_options_payload()


@router.post("/worker-model/test")
async def test_worker_model(request: Request) -> Any:
    body = await _require_dict_body(request)
    from apps.web.worker_models import probe_worker_model

    profile = body.get("profile")
    if not isinstance(profile, dict):
        raise HTTPException(status_code=400, detail="profile must be an object")
    return await asyncio.to_thread(
        probe_worker_model,
        profile=profile,
        model=str(body.get("model") or ""),
        sessions_root=request.app.state.manager.sessions_root,
        backend=str(body.get("backend") or "local"),
    )
