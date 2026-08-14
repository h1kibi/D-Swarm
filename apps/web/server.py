"""FastAPI backend for the web command deck (§14.1 / Sprint 1.1).

Endpoints:
  GET  /api/runs                      list known runs
  POST /api/runs/{run_id}/start       launch a run (mock driver, or swarm if a
                                       challenge spec is posted) — see drivers.py
  GET  /api/runs/{run_id}/events      SSE: the typed event stream (Last-Event-ID
                                       resume via the standard header)
  WS   /api/runs/{run_id}/terminal    sandbox terminal: TERMINAL_OUTPUT bytes
  POST /api/runs/{run_id}/hitl        human command into the run (hint/pause/etc.)
  GET  /                              the single-page UI (static)

The server holds NO solving logic — it only brokers the event bus + HITL. Event
schema is the only contract (§3).
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.web.auth import (
    PUBLIC_API_PATHS,
    AuthConfig,
    TicketStore,
    bearer_from_header,
    check_password,
    issue_token,
    verify_token,
)
from apps.web.run_manager import RunManager
from apps.web.startup_test import StartupTestController
from apps.web.http_utils import _env_float
from apps.web.routes.auth import router as auth_router
from apps.web.routes.blackboard import router as blackboard_router
from apps.web.routes.btw import router as btw_router
from apps.web.routes.credentials import router as credentials_router
from apps.web.routes.engines import router as engines_router
from apps.web.routes.folders import router as folders_router
from apps.web.routes.llm_settings import router as llm_settings_router
from apps.web.routes.profile_health import router as profile_health_router
from apps.web.routes.runtime_environment import router as runtime_environment_router
from apps.web.routes.runs import router as runs_router
from apps.web.routes.scheduler import router as scheduler_router
from apps.web.routes.settings_identity import router as settings_identity_router
from apps.web.routes.settings_workers import router as settings_workers_router
from apps.web.routes.startup_test import router as startup_test_router
from apps.web.routes.worker_image import router as worker_image_router
from apps.web.routes.worker_models import router as worker_models_router
from dswarm.core.dotenv_boot import load_env

load_env()  # local convenience: pick up repo-root .env (shell env still wins)

UI_DIR = Path(__file__).parent / "ui"







def create_app(manager: Optional[RunManager] = None) -> FastAPI:
    mgr = manager or RunManager()

    # Retention policy (BE-auto-archive): auto-archive idle runs, then delete the
    # ones that stay idle. Defaults: archive after 3 days, delete after 7 days,
    # sweep hourly. All env-tunable; set DSWARM_RETENTION_ENABLED=0 to disable
    # (pinned runs are NEVER auto-touched).
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start the reverse-connect control receiver: the in-container supervisors
        # DIAL this (host.docker.internal:<port>) — so the host must be listening
        # before any container starts. Lazy-starts on first use too, but starting it
        # here makes "control port already in use" surface at boot, not mid-run.
        try:
            from dswarm.solver.control_receiver import ControlReceiver
            ControlReceiver.instance()
        except OSError as exc:  # port already bound (another backend?) — log, continue
            print(f"[control-receiver] could not bind control port: {exc}", flush=True)
        task: Optional[asyncio.Task] = None
        enabled = os.environ.get("DSWARM_RETENTION_ENABLED", "1").lower() not in (
            "0", "false", "no", "off", "")
        if enabled:
            task = asyncio.create_task(mgr.retention_loop(
                interval_s=_env_float("DSWARM_RETENTION_INTERVAL", 3600.0),
                archive_after_s=_env_float("DSWARM_ARCHIVE_DAYS", 3.0) * 86400.0,
                delete_after_s=_env_float("DSWARM_DELETE_DAYS", 7.0) * 86400.0,
            ))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await app.state.startup_test.shutdown()
            # Tear down every live swarm/standby task (and its shelled CLI subprocess
            # group) so a server restart doesn't leave budget-eating zombies. This was
            # never wired up before — shutdown() existed but nothing called it.
            await mgr.shutdown()

    app = FastAPI(title="Project D-Swarm — Command Deck", lifespan=lifespan)
    app.state.manager = mgr
    app.state.startup_test = StartupTestController(mgr)
    app.state.engine_cache = {"ts": 0.0, "data": None}
    app.state.engine_cache_ttl_s = 300.0
    app.state.engine_refresh_lock = asyncio.Lock()
    app.include_router(auth_router)
    app.include_router(blackboard_router)
    app.include_router(btw_router)
    app.include_router(credentials_router)
    app.include_router(engines_router)
    app.include_router(folders_router)
    app.include_router(llm_settings_router)
    app.include_router(profile_health_router)
    app.include_router(runtime_environment_router)
    app.include_router(runs_router)
    app.include_router(scheduler_router)
    app.include_router(settings_identity_router)
    app.include_router(settings_workers_router)
    app.include_router(startup_test_router)
    app.include_router(worker_image_router)
    app.include_router(worker_models_router)

    # Auth (P3): a single-password gate in front of /api. fail_fast_check refuses
    # to start if bound to a non-loopback host with no password — see auth.py and
    # docs/_local/plan_p3_auth.md. When no password is set AND the bind is
    # loopback, auth is disabled and the deck behaves exactly as before.
    auth = AuthConfig.from_env()
    auth.fail_fast_check()
    app.state.auth = auth
    app.state.tickets = TicketStore()

    # Dev convenience: the Next dev server (:3001) can talk to this backend
    # directly. Connecting the browser's EventSource straight here (instead of
    # through Next's dev rewrite proxy) avoids the proxy BUFFERING the SSE stream
    # — the proxy holds events until the connection closes, which makes a live
    # run look frozen until it finishes. In prod the static UI is served same-
    # origin by this app, so CORS is a no-op there. Allowlist localhost only.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth gate. Added AFTER CORS, so CORS wraps it (outermost) — preflight
    # OPTIONS are answered by CORS and never reach here. We still bypass OPTIONS
    # defensively (a same-origin request via the Next proxy often omits Origin,
    # so CORS does not short-circuit it). Only /api is gated; the Next server
    # (:3001) owns the UI/login page and must be secured separately when exposed
    # (reverse proxy / loopback bind) — see docs/_local/plan_p3_auth.md.
    #
    # @app.middleware("http") does NOT see websocket scope; the /terminal WS and
    # the SSE /events stream do their own ticket/token check in-handler.
    #
    # IMPORTANT (CORS): a middleware that SHORT-CIRCUITS with its own Response
    # bypasses CORSMiddleware's response path, so a cross-origin 401 would arrive
    # at the browser WITHOUT Access-Control-Allow-Origin — the browser then
    # reports a network error instead of a 401, and the frontend can't tell "needs
    # login" from "backend down". The Next dev UI (:3001) talks to this backend
    # (:8000) cross-origin, so we must mirror the CORS allow-origin header onto the
    # 401 ourselves. (CORSMiddleware only auto-adds headers when the inner app
    # actually runs; our early return never reaches it.)
    _cors_origin_re = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?$")

    def _unauthorized(request: Request) -> JSONResponse:
        resp = JSONResponse({"error": "unauthorized"}, status_code=401)
        origin = request.headers.get("origin")
        if origin and _cors_origin_re.match(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
        return resp

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        cfg: AuthConfig = app.state.auth
        if not cfg.enabled:
            return await call_next(request)
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if not path.startswith("/api/"):
            return await call_next(request)  # static/UI (only present if built)
        if path in PUBLIC_API_PATHS:
            return await call_next(request)
        # SSE events stream authenticates via one-time ticket query param, not a
        # header (EventSource can't set headers); let the handler enforce it.
        if path.endswith("/events"):
            return await call_next(request)
        token = bearer_from_header(request.headers.get("Authorization"))
        if not verify_token(cfg, token):
            return _unauthorized(request)
        return await call_next(request)











    # ── P4 run scheduler (FIFO queue + global concurrency cap) ───────────────
    # The queue is in-memory and transient: a server restart drops pending
    # entries (their runs rehydrate as ghost-finished like any killed run).




    # BTW side-query observer (separate, no swarm slot)
    # Independent route (not /hitl), no run.hitl queue, no InsightBus GUIDANCE,
    # no bus.emit, no CostController, no CliSolver, no scheduler/max_worker slot.
    # Normal turns use a bounded read-only evidence package plus one fixed model
    # call. Explicit deep-audit turns retain the isolated CLI worker path below.
    # static UI: the deck is the Next.js app (run `./run.sh web` → :3001, which
    # talks to this backend's /api). If a Next.js static export ever drops an
    # index.html into ui/, serve it at / too; otherwise / is unused (the bare
    # backend is API-only).
    if (UI_DIR / "index.html").exists():
        @app.get("/")
        async def index() -> Any:
            return FileResponse(UI_DIR / "index.html")

    if UI_DIR.exists():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")

    return app


app = create_app()
