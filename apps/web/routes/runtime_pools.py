"""Read-only runtime pool diagnostics projection."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dswarm.solver.runtime_diagnostics import sanitize_pool_id

_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_int(value: Any) -> int:
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _safe_failure(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    category = getattr(value, "category", "")
    code = getattr(value, "code", "")
    if not isinstance(category, str) or category not in {
        "infrastructure", "identity", "auth", "configuration", "capacity", "worker"
    }:
        category = "infrastructure"
    if not isinstance(code, str) or _SAFE_CODE_RE.fullmatch(code) is None:
        code = "runtime_operation_failed"
    return {"category": category, "code": code}

router = APIRouter(prefix="/api/runs", tags=["runtime-pools"])


def _project_view(view: Any) -> dict[str, Any]:
    failure = _safe_failure(view.failure)
    return {
        "pool_id": sanitize_pool_id(view.pool_id),
        "state": str(view.state) if isinstance(view.state, str) else "unknown",
        "generation": _safe_int(view.generation),
        "pool_instance_id": (
            str(view.pool_instance_id)
            if isinstance(view.pool_instance_id, str)
            and all(0x21 <= ord(char) <= 0x7E for char in view.pool_instance_id)
            and len(view.pool_instance_id) <= 256
            else ""
        ),
        "active_workers": _safe_int(view.active_workers),
        "waiting_workers": _safe_int(view.waiting_workers),
        "capacity": _safe_int(view.capacity),
        "failure": failure,
        "recovery_episode": _safe_int(view.recovery_episode),
    }


@router.get("/{run_id}/runtime-pools")
async def get_runtime_pools(run_id: str, request: Request) -> dict[str, Any]:
    run = request.app.state.manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    pool_manager = run.pool_manager
    views = pool_manager.snapshot_view() if pool_manager is not None else ()
    return {"run_id": run_id, "pools": [_project_view(view) for view in views]}


__all__ = ["router"]
