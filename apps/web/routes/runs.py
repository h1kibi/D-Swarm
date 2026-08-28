"""Run metadata and lifecycle routes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from apps.web.auth import AuthConfig, bearer_from_header, verify_token
from apps.web.http_utils import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES, _require_dict_body
from apps.web.run_manager import Run, RunManager
from dswarm.core.events import Event, EventType

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
async def list_runs(request: Request, archived: int = 0) -> Any:
    return {"runs": request.app.state.manager.list_runs(include_archived=bool(archived))}


@router.get("/{run_id}/budget")
async def budget_snapshot(run_id: str, request: Request) -> Any:
    """Return the run-scoped ledger and profile/account budget projections."""
    run = request.app.state.manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    ledger = run.ledger.snapshot() if run.ledger is not None else {
        "run_id": run_id, "ledger_state": "unavailable", "ledger_error": "ledger_unavailable",
    }
    return {
        "run_id": run_id,
        "ledger": ledger,
        "budget": run.budget_gate.snapshot(),
        "ledger_state": getattr(run.spawn_guard, "ledger_state", ledger.get("ledger_state")),
        "ledger_error": getattr(run.spawn_guard, "ledger_error", ledger.get("ledger_error")),
    }


@router.post("/{run_id}/budget/rebuild")
async def rebuild_budget(run_id: str, request: Request) -> Any:
    """Replay the run ledger and recover journal-only usage records."""
    manager = request.app.state.manager
    if manager.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        run = await manager.rebuild_ledger(run_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc) or "ledger_rebuild_failed") from exc
    ledger = run.ledger.snapshot() if run.ledger is not None else {}
    return {
        "run_id": run_id,
        "ledger": ledger,
        "budget": run.budget_gate.snapshot(),
        "ledger_state": getattr(run.spawn_guard, "ledger_state", ledger.get("ledger_state")),
        "ledger_error": getattr(run.spawn_guard, "ledger_error", ledger.get("ledger_error")),
    }


@router.patch("/{run_id}")
async def update_run(run_id: str, request: Request) -> Any:
    body = await _require_dict_body(request)
    mgr = request.app.state.manager
    ok = True
    if "pinned" in body:
        ok = mgr.set_pinned(run_id, bool(body["pinned"]), now=time.time()) and ok
    if "archived" in body:
        ok = mgr.set_archived(run_id, bool(body["archived"])) and ok
    if "name" in body:
        ok = mgr.rename(run_id, body.get("name")) and ok
    if "folder_id" in body:
        ok = mgr.set_folder(run_id, body.get("folder_id")) and ok
    if "order" in body:
        ok = mgr.set_order(run_id, body.get("order")) and ok
    if body.get("cancel"):
        ok = await mgr.cancel_run(run_id) and ok
    if body.get("pause"):
        ok = await mgr.pause_queued(run_id) and ok
    if body.get("resume"):
        ok = await mgr.resume_queued(run_id) and ok
    run = mgr.get(run_id)
    return {"ok": ok, "run": run.summary() if run else None}


@router.delete("/{run_id}")
async def delete_run(run_id: str, request: Request) -> Any:
    ok = await request.app.state.manager.delete(run_id)
    return {"ok": ok}


@router.post("/{run_id}/open")
async def open_run_workspace(run_id: str, request: Request) -> Any:
    ok = request.app.state.manager.open_workspace(run_id)
    return {"ok": ok}


@router.get("/{run_id}/credentials")
async def run_credentials(run_id: str, request: Request) -> Any:
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    mgr = request.app.state.manager
    run = mgr.get(run_id)
    graph_db = mgr.workspace_dir(run_id) / "graph" / "shared_graph.db"
    if not graph_db.exists():
        return {"credentials": []}
    challenge = Challenge(
        id=run_id,
        name=(run.name if run else run_id),
        category=(run.category if run else "web") or "web",
    )
    graph = None
    try:
        graph = SQLiteSharedGraph.open(db_path=graph_db, challenge=challenge)
        return {"credentials": graph.canonical_credentials()}
    finally:
        if graph is not None:
            graph.close()
@router.post("")
async def new_run(request: Request) -> Any:
    # Mint a fresh run id for a new conversation ("+ New solve"). The deck
    # then opens this run's SSE and POSTs /start with the dispatch prompt.
    run = request.app.state.manager.create_new()
    return {"run_id": run.run_id}

@router.post("/{run_id}/start")
async def start_run(run_id: str, request: Request) -> Any:
    body = await _require_dict_body(request)
    from apps.web.drivers import build_driver

    try:
        driver = build_driver(body, mgr=request.app.state.manager)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # seed rail metadata up front so the row appears the instant we dispatch
    # (before run.started lands) — conversational dispatch infers the rest.
    run = request.app.state.manager.get(run_id) or request.app.state.manager.create(run_id)
    ch = (body.get("challenge") or {})
    if ch.get("name"):
        run.name = ch["name"]
    if ch.get("category"):
        run.category = ch["category"]
    # Re-starting an existing run_id (e.g. a re-test redo of the same challenge):
    # the run object still carries the PRIOR run's terminal state (finished/solved/
    # flag). Reset it synchronously here so the rail doesn't show a freshly-
    # dispatched run as "已解出" until the new run.started bus event is sinked
    # (it would otherwise display the stale solved flag the whole time it runs).
    run.finished = False
    run.solved = False
    run.flag = None
    run.flags = []
    run.paused = False
    run.started = True
    # Keep the non-secret roster/backend/profile settings beside the run so a
    # later "继续解题" does not silently resolve against a different global
    # configuration.
    request.app.state.manager.remember_dispatch(run_id, body)
    request.app.state.manager.configure_budget(run_id, body)
    await request.app.state.manager.start(run_id, driver)

    # ChatGPT-style auto-title: if the operator gave no explicit name, kick off
    # a background summarizer that names the conversation from the prompt and
    # emits RUN_TITLED. Fire-and-forget so it never delays swarm launch.
    if not run.name:
        prompt = body.get("prompt") or ch.get("description") or ""
        if prompt.strip():
            from apps.web.titler import generate_title

            llm_profiles = request.app.state.manager.worker_config.get().get("llm_profiles", {})
            titler_profile = llm_profiles.get("titler") or {}
            title_model = titler_profile.get("model")
            title_base_url = titler_profile.get("base_url") or None
            title_usage_writer = request.app.state.manager.internal_usage_writer(
                run,
                solver_id="titler",
                profile_id="titler",
                configured_account_id=(
                    str(titler_profile.get("credential_account") or "").strip() or None
                ),
            )
            asyncio.create_task(
                generate_title(
                    prompt, bus=run.bus, run_id=run_id,
                    model=title_model, base_url=title_base_url,
                    usage_writer=title_usage_writer,
                    usage_context=title_usage_writer.context,
                )
            )

    # P4: the scheduler may have queued this run (concurrency cap) — surface
    # the position so the deck can render "queued (N)" instead of "running".
    resp: dict[str, Any] = {
        "run_id": run_id, "started": True, "kind": body.get("kind", "swarm"),
    }
    if run.queued:
        resp["queued"] = True
        resp["position"] = run.queue_position
    return resp

@router.post("/{run_id}/uploads")
async def upload_files(
    run_id: str, request: Request, files: list[UploadFile] = File(...)
) -> Any:
    # File-based tracks (crypto/rev/forensics/misc) ship the challenge AS
    # files. The deck POSTs them here; we save into the run's own folder
    # (sessions/{id}/uploads/) and hand back ABSOLUTE paths. The deck then
    # threads those paths into challenge.attachments at /start, and the
    # worker stages them into its cwd (CliSolver._stage_attachments). No
    # bytes flow through /start — only the saved paths.
    mgr: RunManager = request.app.state.manager
    # ensure a run handle exists so an upload BEFORE dispatch still works
    # (the deck promotes a draft to a real run id before uploading, but be
    # robust — mirror the get-or-create the events/start endpoints use).
    mgr.get(run_id) or mgr.create(run_id)
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=413, detail="too many files")

    dest_dir = mgr.uploads_dir(run_id)
    saved: list[dict[str, Any]] = []
    for uf in files:
        # SANITIZE: strip any path the client put in the name. Path(name).name
        # drops directories AND collapses "../x"/absolute paths to a basename,
        # so an upload can never escape dest_dir.
        name = Path(uf.filename or "file").name
        if not name or name in (".", ".."):
            name = "file"
        # dedupe collisions within this run's folder: foo.txt, foo-1.txt, ...
        target = dest_dir / name
        if target.exists():
            stem, suf = target.stem, target.suffix
            i = 1
            while (dest_dir / f"{stem}-{i}{suf}").exists():
                i += 1
            target = dest_dir / f"{stem}-{i}{suf}"
        # stream to disk in chunks with a running size guard (never buffer a
        # whole file in memory; abort + clean up if it blows the cap).
        size = 0
        try:
            with target.open("wb") as out:
                while True:
                    chunk = await uf.read(1 << 20)  # 1 MB
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        out.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413, detail=f"{name} too large"
                        )
                    out.write(chunk)
        finally:
            await uf.close()
        saved.append(
            {"name": target.name, "path": str(target.resolve()), "size": size}
        )
    return {"files": saved}

@router.get("/{run_id}/events")
async def events(run_id: str, request: Request) -> Any:
    # Auth: EventSource can't send an Authorization header, so the SSE stream
    # authenticates via a one-time ticket (?ticket=) minted by an
    # authenticated POST /api/auth/ticket. A bearer header is also accepted
    # (non-browser clients). This MUST run before manager.create() below, so
    # an unauthenticated open can't spawn empty run handles.
    cfg: AuthConfig = request.app.state.auth
    if cfg.enabled:
        tok = bearer_from_header(request.headers.get("Authorization"))
        authed = verify_token(cfg, tok) or request.app.state.tickets.redeem(
            request.query_params.get("ticket"))
        if not authed:
            raise HTTPException(status_code=401, detail="unauthorized")
    manager: RunManager = request.app.state.manager
    # A deck commonly opens its event stream BEFORE the run is launched (the
    # operator stares at an empty board, then fills the form). Create the run
    # handle on demand so the SSE stays open and starts streaming the instant
    # the run starts — instead of 404ing and forcing the browser to reconnect.
    run: Run = manager.get(run_id) or manager.create(run_id)

    last_id_hdr = request.headers.get("Last-Event-ID")
    last_id = int(last_id_hdr) if last_id_hdr and last_id_hdr.isdigit() else 0
    # The in-memory ring is bounded and a rehydrated/reopened run may have a
    # fresh EventBus. Always repair from the durable JSONL first, even on
    # reconnect. SessionStore.replay_monotonic() rewrites broken historical
    # seq resets (e.g. 1808 → 1 after a backend restart) into a single SSE
    # cursor, so the browser's Last-Event-ID never skips "new" low-id events.
    fresh = last_id == 0

    async def gen():
        replayed_seq = 0
        replayed_count = 0
        last_lifecycle = ""
        async for ev in run.store.replay_monotonic(run_id, after_seq=last_id):
            replayed_seq = ev.seq
            replayed_count += 1
            if ev.event_type in (EventType.RUN_STARTED,
                                 EventType.RUN_FINISHED,
                                 EventType.RUN_REOPENED):
                last_lifecycle = ev.event_type.value
            yield {
                "id": str(ev.seq),
                "event": ev.event_type.value,
                "data": ev.model_dump_json(),
            }
            if await request.is_disconnected():
                return
            # A large historical run can replay thousands of JSONL events.
            # Yield to uvicorn periodically so sidebar polls and live-run
            # control requests do not look "backend frozen" during replay.
            if replayed_count % 100 == 0:
                await asyncio.sleep(0)
        # Ghost-running guard: only needed for a fresh full replay. On reconnect
        # with no durable events after Last-Event-ID, we do not know the last
        # lifecycle from the skipped prefix and should simply wait on the bus.
        task = getattr(run, "task", None)
        live = task is not None and not task.done()
        if fresh and not live and last_lifecycle in ("run.started", "run.reopened"):
            replayed_seq = max(replayed_seq, run.store.last_stream_seq(run_id)) + 1
            synth = Event(
                event_type=EventType.RUN_FINISHED, run_id=run_id,
                seq=replayed_seq,
                payload={"flag": run.flag, "flags": list(run.flags),
                         "expected_flags": run.expected_flags,
                         "multi_flag": run.multi_flag,
                         "solved": run.solved})
            yield {
                "id": str(replayed_seq),
                "event": synth.event_type.value,
                "data": synth.model_dump_json(),
            }
        # live tail: everything after what we just replayed (or after the
        # client's Last-Event-ID on a reconnect). A finished run's bus is
        # closed, so subscribe() returns after backlog replay. Do NOT let the
        # HTTP response EOF: browser EventSource treats EOF as an error and
        # reconnects forever, replaying finished histories in a loop. Instead,
        # keep the SSE open (ping handles liveness) and hop to a fresh bus if
        # resolve/standby reopens the run.
        manager._sync_bus_seq(run.bus, store=run.store, run_id=run_id)
        tail_from = max(last_id, replayed_seq, run.store.last_stream_seq(run_id))
        while True:
            bus = run.bus
            async for ev in bus.subscribe(last_event_id=tail_from):
                tail_from = ev.seq
                yield {
                    "id": str(ev.seq),
                    "event": ev.event_type.value,
                    "data": ev.model_dump_json(),
                }
                if await request.is_disconnected():
                    return
            while run.bus is bus:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(1)

    return EventSourceResponse(
        gen(),
        ping=10,
        ping_message_factory=lambda: ServerSentEvent(comment="dswarm-ping"),
    )

@router.websocket("/{run_id}/terminal")
async def terminal(ws: WebSocket, run_id: str) -> None:
    # Auth check BEFORE accept(): a WebSocket can't carry an Authorization
    # header from the browser, so it presents a one-time ticket (?ticket=)
    # or a bearer token (?token=, non-browser). Reject the handshake outright
    # (close 4401) on failure so we never expose an authenticated socket.
    cfg: AuthConfig = ws.app.state.auth
    if cfg.enabled:
        authed = ws.app.state.tickets.redeem(ws.query_params.get("ticket")) or \
            verify_token(cfg, ws.query_params.get("token"))
        if not authed:
            await ws.close(code=4401)
            return
    await ws.accept()
    manager: RunManager = ws.app.state.manager
    run = manager.get(run_id)
    if run is None:
        await ws.close(code=4004)
        return
    try:
        # replay from 0 so a terminal opened mid/just-after a run still shows
        # the buffered output, then streams live
        async for ev in run.bus.subscribe(last_event_id=0):
            if ev.event_type is EventType.TERMINAL_OUTPUT:
                await ws.send_text(ev.payload.get("text", ""))
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return

@router.post("/{run_id}/resolve")
async def resolve_run(run_id: str, request: Request) -> Any:
    """"继续做题": continue a finished ReasonSwarm run through the current
    scheduler (reuses its workspace so verified facts carry over). Distinct from
    /hitl which, on a finished run, starts a single standby worker for follow-up."""
    body = await _require_dict_body(request, allow_empty=True)
    try:
        ok = await request.app.state.manager.resolve(run_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Configuration/preflight failures happen before the run is reopened;
        # never return 200/ok=true for a recovery that cannot be launched.
        raise HTTPException(status_code=503, detail=str(exc)[:500]) from exc
    if not ok:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "detail": "run is already active or queued"},
        )
    run = request.app.state.manager.get(run_id)
    return {"ok": True, "queued": bool(run and run.queued),
            "position": run.queue_position if run and run.queued else None}

@router.post("/{run_id}/workers")
async def spawn_worker(run_id: str, request: Request) -> Any:
    # Operator runtime control: add a worker to a live ReasonSwarm scheduler.
    # Body {"engine": "pi"} is optional; omitted lets the scheduler select from
    # the configured healthy Pi worker profiles.
    body = await _require_dict_body(request, allow_empty=True)
    ok = await request.app.state.manager.post_worker_cmd(
        run_id, "spawn", engine=body.get("engine"))
    return {"ok": ok}

@router.delete("/{run_id}/workers")
async def kill_worker(run_id: str, request: Request) -> Any:
    # operator runtime control: stop a specific worker by its solver_id.
    body = await _require_dict_body(request, allow_empty=True)
    ok = await request.app.state.manager.post_worker_cmd(
        run_id, "kill", solver_id=body.get("solver_id"))
    return {"ok": ok}

@router.post("/{run_id}/hitl")
async def hitl(run_id: str, request: Request) -> Any:
    body = await _require_dict_body(request)
    ok = await request.app.state.manager.post_hitl(
        run_id,
        body.get("target", "global"),
        body.get("action", "hint"),
        **{k: v for k, v in body.items() if k not in ("target", "action")},
    )
    return {"ok": ok}
