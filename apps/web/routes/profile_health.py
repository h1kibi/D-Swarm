"""Worker profile health routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import project_probe_result

router = APIRouter(prefix="/api/settings/profiles", tags=["profile-health"])

_PROFILE_HEALTH_FIELDS = (
    "profile_id",
    "engine",
    "backend",
    "status",
    "layer",
    "blocker",
    "detail",
    "model",
    "account_id",
    "binding_kind",
    "effective_credential_id",
)


@router.get("/health")
async def get_profiles_health(request: Request) -> Any:
    from apps.web.worker_config import backend_for_profile
    from dswarm.core.runtime_env import is_web_container
    from dswarm.solver.profile_health import evaluate_profile_health

    cfg = request.app.state.manager.worker_config.get()
    profiles = [p for p in (cfg.get("worker_profiles") or []) if isinstance(p, dict)]
    runtime_profiles = cfg.get("runtime_profiles") or []
    worker_backend = str(cfg.get("worker_backend") or "")
    in_web = is_web_container()
    sessions_root = request.app.state.manager.sessions_root

    def _eval_all() -> list[dict]:
        out: list[dict] = []
        for p in profiles:
            backend = backend_for_profile(
                p, runtime_profiles=runtime_profiles,
                worker_backend=worker_backend, in_web_container=in_web,
            )
            h = evaluate_profile_health(
                p, backend=backend, sessions_root=sessions_root, depth="binding",
                llm_providers=cfg.get("llm_providers") or [],
            )
            out.append(project_probe_result(h, fields=_PROFILE_HEALTH_FIELDS, include_ok=True))
        return out

    return {"profiles": await asyncio.to_thread(_eval_all)}


@router.post("/{profile_id}/health")
async def test_profile_health(profile_id: str, request: Request) -> Any:
    from apps.web.worker_config import backend_for_profile
    from dswarm.core.runtime_env import is_web_container
    from dswarm.solver.profile_health import evaluate_profile_health

    cfg = request.app.state.manager.worker_config.get()
    profiles = [p for p in (cfg.get("worker_profiles") or []) if isinstance(p, dict)]
    match = next(
        (p for p in profiles
         if str(p.get("name") or p.get("id")) == profile_id
         or str(p.get("id")) == profile_id),
        None,
    )
    if match is None:
        from dswarm.solver.worker_profiles import resolve_seat_ref
        sid = resolve_seat_ref(
            profile_id, seats=cfg.get("seats") or [],
            alias_table=cfg.get("seat_alias") or {},
        )
        if sid is not None:
            match = next((p for p in profiles if str(p.get("id")) == sid), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"unknown profile: {profile_id}")
    backend = backend_for_profile(
        match, runtime_profiles=cfg.get("runtime_profiles") or [],
        worker_backend=str(cfg.get("worker_backend") or ""),
        in_web_container=is_web_container(),
    )
    h = await asyncio.to_thread(
        evaluate_profile_health,
        match, backend=backend,
        sessions_root=request.app.state.manager.sessions_root, depth="auth",
        llm_providers=cfg.get("llm_providers") or [],
    )
    return project_probe_result(h, fields=_PROFILE_HEALTH_FIELDS, include_ok=True)
