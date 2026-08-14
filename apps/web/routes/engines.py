"""Engine availability and health-check routes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request

from dswarm.solver.credential_accounts import account_store_root

router = APIRouter(prefix="/api/engines", tags=["engines"])

@router.get("")
async def engines(request: Request) -> Any:
    from dswarm.solver.cli_driver import engine_status

    cache = request.app.state.engine_cache
    ttl_s = request.app.state.engine_cache_ttl_s
    refresh_lock = request.app.state.engine_refresh_lock
    now = time.time()
    if cache["data"] is not None and now - cache["ts"] <= ttl_s:
        return {"engines": cache["data"]}
    if refresh_lock.locked() and cache["data"] is not None:
        return {"engines": cache["data"]}
    async with refresh_lock:
        now = time.time()
        if cache["data"] is not None and now - cache["ts"] <= ttl_s:
            return {"engines": cache["data"]}
        acct_root = str(account_store_root(request.app.state.manager.sessions_root))
        try:
            cfg = request.app.state.manager.worker_config.get()
            backend = str(cfg.get("worker_backend") or "local")
            enabled = set(cfg.get("engines") or [])
            profiles = [
                p for p in (cfg.get("worker_profiles") or [])
                if (p.get("name") or p.get("id")) in enabled
            ]
        except Exception:
            backend = "local"
            profiles = []
        data = await asyncio.to_thread(engine_status, acct_root, backend, profiles)
        cache["data"] = data
        cache["ts"] = time.time()
    return {"engines": cache["data"]}


@router.get("/health")
async def engines_health(request: Request) -> Any:
    from dswarm.solver.cli_driver import engine_health

    backend = str(request.query_params.get("backend") or "local")
    if backend not in ("local", "container"):
        backend = "local"
    acct_root = str(account_store_root(request.app.state.manager.sessions_root))
    profiles = []
    if backend == "local":
        try:
            cfg = request.app.state.manager.worker_config.get()
            enabled = set(cfg.get("engines") or [])
            profiles = [
                p for p in (cfg.get("worker_profiles") or [])
                if (p.get("name") or p.get("id")) in enabled
            ]
        except Exception:
            profiles = []
    data = await asyncio.to_thread(engine_health, backend, acct_root, profiles)
    return {"engines": data}
