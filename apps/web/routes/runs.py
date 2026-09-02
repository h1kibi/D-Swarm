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
from apps.web.http_utils import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    MAX_UPLOAD_TOTAL_BYTES,
    _require_dict_body,
)
from apps.web.run_manager import Run, RunManager
from dswarm.core.events import Event, EventType
from dswarm.solver.runtime_policy import RuntimePolicyError
from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuildError

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
async def list_runs(request: Request, archived: int = 0) -> Any:
    summaries = request.app.state.manager.list_runs(include_archived=bool(archived))
    authenticated = _request_authenticated(request)
    if authenticated:
        return {"runs": summaries}
    # Containment (run-6427): worker containers can reach the host control
    # plane (Docker Desktop loops host.docker.internal back to host services),
    # and this deployment may run passwordless. A flag VALUE listable without
    # authentication let one run scrape another challenge's accepted flags out
    # of the rail API and "solve" without ever seeing the attachment. The rail
    # only needs counts for its progress chips — values stay behind auth.
    return {"runs": [_redact_summary(s) for s in summaries]}


def _request_authenticated(request: Request) -> bool:
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        return False  # passwordless: nothing proves an operator is asking
    return verify_token(cfg, bearer_from_header(request.headers.get("Authorization")))


def _redact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return summary
    out = dict(summary)
    flags = out.pop("flags", None) or []
    if out.get("flag"):
        flags = list(flags) or [out["flag"]]
    out["flag"] = None
    out["flags"] = []
    out["flag_count"] = len(flags)
    out["flags_redacted"] = True
    return out


# Ledger error classifier for the deck: machine-readable kind so the UI can
# branch on KNOWN, operator-actionable failure classes instead of string
# matching raw messages. usage_conflict = the same provider call recorded with
# two different outcomes in run history — replay can never reconcile it (the
# gateway double-record defect fixed 2026-08-30; pre-fix runs keep the pair).
_LEDGER_ERROR_KINDS = {
    "conflicting usage_id:": "usage_conflict",
    "invalid usage event:": "invalid_event",
}


def _ledger_error_kind(error: Any) -> str | None:
    text = str(error or "")
    for marker, kind in _LEDGER_ERROR_KINDS.items():
        if marker in text:
            return kind
    return None


@router.get("/{run_id}/budget")
async def budget_snapshot(run_id: str, request: Request) -> Any:
    """Return the run-scoped ledger and profile/account budget projections."""
    run = request.app.state.manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    ledger = run.ledger.snapshot() if run.ledger is not None else {
        "run_id": run_id, "ledger_state": "unavailable", "ledger_error": "ledger_unavailable",
    }
    ledger_error = getattr(run.spawn_guard, "ledger_error", ledger.get("ledger_error"))
    return {
        "run_id": run_id,
        "ledger": ledger,
        "budget": run.budget_gate.snapshot(),
        "ledger_state": getattr(run.spawn_guard, "ledger_state", ledger.get("ledger_state")),
        "ledger_error": ledger_error,
        "ledger_error_kind": _ledger_error_kind(ledger_error),
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
    if _request_authenticated(request):
        return {"ok": ok, "run": run.summary() if run else None}
    return {"ok": ok, "run": _redact_summary(run.summary()) if run else None}


@router.post("/{run_id}/retitle")
async def retitle_run(run_id: str, request: Request) -> Any:
    """Re-apply the rail naming rule (`方向-标识`: the identifier is the 题目名
    → URL host[:port] → attachment filename, whatever independently pins down
    the challenge) to an EXISTING run, using its remembered dispatch body.

    Sessions dispatched before the rule landed keep their old slug/LLM name
    forever otherwise. Deterministic rule hits rename synchronously and return
    the name; anything else (pwn with no stated 题目名) falls back to the
    background titler LLM and reports `pending: true` —the rail's poll picks
    the rename up when it lands. Derived titles go through the fleet-wide
    uniqueness guard, so a re-solve of one target becomes `…-2` instead of a
    second identical row. The result becomes the run's sticky custom name, so
    a later RUN_TITLED / re-solve cannot clobber an explicit operator request.
    """
    mgr: RunManager = request.app.state.manager
    run = mgr.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    body = mgr.dispatch_for(run_id)
    ch = body.get("challenge") or {}
    prompt = str(body.get("prompt") or ch.get("description") or "")
    if not prompt.strip():
        raise HTTPException(status_code=409, detail="no remembered dispatch prompt to derive a name from")
    from apps.web.drivers import _infer_challenge
    from apps.web.titler import compose_title, generate_title, rule_name_part

    # same derivation as /start's auto-titler: INFERRED category + attachment
    # filenames (challenge.attachments are absolute saved paths). challenge.name
    # stays the RAW operator-supplied 题目名 — the inferred slug from prompt
    # words is not an identifier and must not win the ladder.
    inferred_ch = (_infer_challenge(dict(body)).get("challenge") or {})
    category = str(inferred_ch.get("category") or "")
    challenge_name = str(ch.get("name") or "")
    attachment_names = [
        Path(a).name for a in (inferred_ch.get("attachments") or [])
        if isinstance(a, str)
    ]
    name_part, deterministic = rule_name_part(
        prompt, category, attachment_names, challenge_name,
    )
    if deterministic:
        title = mgr.unique_title(run_id, compose_title(category, name_part))
        if title:
            mgr.rename(run_id, title)
        return {"ok": bool(title), "name": title, "pending": False}

    llm_profiles = mgr.worker_config.get().get("llm_profiles", {})
    titler_profile = llm_profiles.get("titler") or {}
    title_usage_writer = mgr.internal_usage_writer(
        run,
        solver_id="titler",
        profile_id="titler",
        configured_account_id=(
            str(titler_profile.get("credential_account") or "").strip() or None
        ),
    )

    async def _apply_llm_title() -> None:
        # no bus event: RUN_TITLED only adopts over a slug, and this run already
        # has a name — we rename directly when the (always-non-empty) title lands.
        try:
            title = await generate_title(
                prompt,
                model=titler_profile.get("model"),
                base_url=titler_profile.get("base_url") or None,
                usage_writer=title_usage_writer,
                usage_context=title_usage_writer.context,
                category=category,
                attachment_names=attachment_names,
                challenge_name=challenge_name or None,
            )
        except Exception:
            return
        if title:
            mgr.rename(run_id, mgr.unique_title(run_id, title))

    asyncio.create_task(_apply_llm_title())
    return {"ok": True, "name": None, "pending": True}


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
    # operator-supplied names are sticky; derived slugs (from _infer_challenge
    # inside build_driver) are preliminary and may be replaced by the rule/LLM
    # title (RUN_TITLED) once it lands.
    had_operator_name = bool(ch.get("name"))
    run.name_is_slug = not had_operator_name
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
    try:
        await request.app.state.manager.start(run_id, driver)
    except RuntimePolicyError as exc:
        # M9a fail-closed: a misconfigured runtime (container profiles without a
        # freezable docker context, local workers without the dual gate) must
        # surface as an operator-visible launch error, not a silently dead run.
        raise HTTPException(status_code=400, detail=f"runtime_policy: {exc}") from exc
    except RuntimeSnapshotBuildError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"runtime_snapshot: {exc.code}: {exc.safe_detail}",
        ) from exc
    except Exception as exc:
        # ``RunManager.start`` settles preflight failures before re-raising. Do
        # not reflect raw host/SDK exception text: it can contain credentials or
        # request URLs. The terminal event carries the same safe classification.
        raise HTTPException(
            status_code=400, detail=f"dispatch: {type(exc).__name__}: startup preflight failed"
        ) from exc

    # ChatGPT-style auto-title: if the operator gave no explicit name, kick off
    # a background summarizer that names the conversation from the prompt and
    # emits RUN_TITLED. Fire-and-forget so it never delays swarm launch.
    if not run.name:
        prompt = body.get("prompt") or ch.get("description") or ""
        if prompt.strip():
            from apps.web.titler import generate_title
            from apps.web.drivers import _infer_challenge

            # rule naming needs the INFERRED category and the attachment
            # filenames (challenge.attachments are absolute saved paths).
            inferred_ch = (_infer_challenge(dict(body)).get("challenge") or {})
            category = str(inferred_ch.get("category") or "")
            attachment_names = [
                Path(a).name for a in (inferred_ch.get("attachments") or [])
                if isinstance(a, str)
            ]
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
                    category=category,
                    attachment_names=attachment_names,
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
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        # Multipart framing adds a small amount of overhead. The streaming
        # counter below remains authoritative for the actual file bytes.
        framing_allowance = max(1 << 20, len(files) * 4096)
        if int(content_length) > MAX_UPLOAD_TOTAL_BYTES + framing_allowance:
            raise HTTPException(status_code=413, detail="upload request too large")

    dest_dir = mgr.uploads_dir(run_id)
    saved: list[dict[str, Any]] = []
    total_size = 0
    try:
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
                        total_size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise HTTPException(
                                status_code=413, detail=f"{name} too large"
                            )
                        if total_size > MAX_UPLOAD_TOTAL_BYTES:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise HTTPException(
                                status_code=413, detail="upload request too large"
                            )
                        out.write(chunk)
            finally:
                await uf.close()
            saved.append(
                {"name": target.name, "path": str(target.resolve()), "size": size}
            )
    except HTTPException:
        for entry in saved:
            Path(entry["path"]).unlink(missing_ok=True)
        raise
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
