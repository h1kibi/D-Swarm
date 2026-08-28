"""Blackboard worker bridge route."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body
from dswarm.solver.blackboard_skill import _repo_blackboard_script

router = APIRouter(prefix="/api/blackboard", tags=["blackboard"])

@router.post("/{run_id}")
async def blackboard_command(run_id: str, request: Request) -> Any:
    token = request.headers.get("X-Blackboard-Token", "")
    if not request.app.state.manager.verify_board_token(run_id, token):
        raise HTTPException(status_code=401, detail="invalid blackboard token")
    body = await _require_dict_body(request, allow_empty=True)
    import subprocess
    import sys

    from dswarm.solver.blackboard_skill import _repo_blackboard_script

    cmd = str(body.get("cmd") or "").strip()
    allowed = {
        "read-facts", "read-review", "read-routes", "read-branches",
        "read-deadends", "read-flags", "list-intents", "write-fact",
        "mark-deadend", "claim", "claim-activity", "list-activities",
        "claim-resource", "release-resource", "read-resource-locks",
        "read-directives", "directive-status", "register-cleanup", "read-cleanups",
    }
    if cmd not in allowed:
        raise HTTPException(status_code=400, detail=f"unsupported blackboard command: {cmd}")
    script = _repo_blackboard_script()
    if not script:
        raise HTTPException(status_code=500, detail="blackboard script unavailable")
    args = [str(a) for a in (body.get("args") or []) if isinstance(a, (str, int, float))]
    root = request.app.state.manager.workspace_dir(run_id)
    graph_dir = root / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DSWARM_BLACKBOARD_DB"] = str(graph_dir / "shared_graph.db")
    env["DSWARM_WORKER_ID"] = str(body.get("worker") or "worker")
    env["DSWARM_INTENT_ID"] = str(body.get("intent_id") or "")
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, script, cmd, *args],
        cwd=str(graph_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
