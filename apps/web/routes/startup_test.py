"""Worker startup-test routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from apps.web.auth import bearer_from_header, verify_token
from apps.web.startup_test import sse_json

router = APIRouter(prefix="/api/startup-test", tags=["startup-test"])


@router.post("")
async def start_startup_test(request: Request) -> Any:
    body: dict[str, Any] = {}
    try:
        if (request.headers.get("content-type") or "").lower().startswith("application/json"):
            data = await request.json()
            if isinstance(data, dict):
                body = data
    except Exception:
        body = {}
    session = await request.app.state.startup_test.start(
        mode=str(body.get("mode") or "startup"),
        benchmark=str(body.get("benchmark") or "local-smoke"),
    )
    return {"test_id": session.id}


@router.get("/{test_id}/events")
async def startup_test_events(test_id: str, request: Request) -> Any:
    cfg = request.app.state.auth
    if cfg.enabled:
        tok = bearer_from_header(request.headers.get("Authorization"))
        authed = verify_token(cfg, tok) or request.app.state.tickets.redeem(
            request.query_params.get("ticket"))
        if not authed:
            raise HTTPException(status_code=401, detail="unauthorized")

    session = request.app.state.startup_test.get(test_id)
    if session is None:
        return EventSourceResponse(_empty_stream(), ping=10)

    last_seq_raw = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID") or "0"
    try:
        last_seq = max(0, int(str(last_seq_raw).strip() or "0"))
    except ValueError:
        last_seq = 0

    async def stream():
        async for item in session.iter_events(last_seq=last_seq):
            yield {"id": str(item.get("seq", "")), "data": sse_json(item)}

    return EventSourceResponse(stream(), ping=10)


async def _empty_stream():
    if False:
        yield
