"""Read-only runtime pool diagnostics projection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dswarm.solver.runtime_diagnostics import (
    RuntimeDiagnosticsStore,
    sanitize_pool_id,
)

_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Per-pool lifecycle history surfaced to the deck: an allowlisted projection of
# the already-sanitized diagnostics rows (codes only, no free-text reasons).
_HISTORY_FIELDS = (
    "state", "reason_code", "recovery_episode", "updated_at", "kind", "generation",
)
_HISTORY_LIMIT = 8


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


def _project_history_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    out: dict[str, Any] = {field: row.get(field) for field in _HISTORY_FIELDS}
    failure = row.get("failure")
    out["failure"] = (
        {
            "category": str(failure.get("category") or "infrastructure"),
            "code": str(failure.get("code") or "runtime_operation_failed"),
        }
        if isinstance(failure, dict)
        else None
    )
    return out


@router.get("/{run_id}/runtime-pools")
async def get_runtime_pools(run_id: str, request: Request) -> dict[str, Any]:
    manager = request.app.state.manager
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    pool_manager = run.pool_manager
    views = pool_manager.snapshot_view() if pool_manager is not None else ()
    policy = getattr(run, "runtime_policy", None)
    policy_mode = str(getattr(policy, "mode", "") or "")
    store = RuntimeDiagnosticsStore(
        run_root=Path(manager.sessions_root) / run_id, run_id=run_id,
    )
    pools: list[dict[str, Any]] = []
    for view in views:
        pool = _project_view(view)
        try:
            rows = store.read_lifecycle(pool["pool_id"])[- _HISTORY_LIMIT:]
        except Exception:  # noqa: BLE001 - diagnostics are best-effort observability
            rows = []
        pool["history"] = [_project_history_row(row) for row in rows]
        pools.append(pool)
    return {
        "run_id": run_id,
        "policy_mode": policy_mode,
        "pools": pools,
    }


__all__ = ["router"]
