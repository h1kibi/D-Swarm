"""BTW side-query observer route."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from apps.web.http_utils import _btw_timeout_exception, _env_float, _require_dict_body
from apps.web.run_manager import RunManager
from dswarm.core.storage import safe_run_storage_key

router = APIRouter(prefix="/api/runs", tags=["btw"])

@router.post("/{run_id}/btw")
async def btw(run_id: str, request: Request) -> Any:
    from dswarm.solver.btw import (
        BtwLimiter,
        btw_model_for,
        build_btw_evidence_pack_sync,
        btw_evidence_messages,
        parse_btw_structured_answer,
        run_meta_dict,
        sanitize_transcript,
    )
    from dswarm.core.llm import LLMClient
    from dswarm.solver.btw import _BTW_AUTH_FAILURE
    body = await _require_dict_body(request)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)
    transcript = sanitize_transcript(body.get("transcript"))
    context_hint = str(body.get("context_hint") or "")

    mgr: RunManager = request.app.state.manager
    run = mgr.get(run_id)
    if run is None:
        # Unknown run → 404. Do NOT create a workspace for it.
        return JSONResponse({"error": "unknown run"}, status_code=404)

    # The worker needs a cwd, so /btw creates only a per-turn scratch dir under
    # the run workspace. It never opens the graph read-write or joins the swarm.
    safe = safe_run_storage_key(run_id)
    root = mgr.workspace_dir(run_id).resolve()
    graph_db = root / "graph" / "shared_graph.db"
    jsonl_path = (mgr.sessions_root / f"{safe}.jsonl").resolve()
    board_path = root / ".dswarm_board.md"
    winner_path = root / "winner.json"
    arts_path = root / "arts"
    uploads_path = (mgr.sessions_root / safe / "uploads").resolve()
    challenge_name = run.name or run_id
    challenge_category = (run.category or "web") or "web"
    meta = run_meta_dict(run)
    try:
        deck_workers = getattr(run, "deck_workers", None)
        if deck_workers:
            meta["workers"] = list(deck_workers)
    except Exception:
        pass

    # Lazy-init the per-app limiter (one BtwLimiter for all runs, keyed by run_id).
    limiter: BtwLimiter = getattr(request.app.state, "btw_limiters", None)
    if limiter is None:
        limiter = BtwLimiter()
        request.app.state.btw_limiters = limiter  # type: ignore[attr-defined]

    wc = mgr.worker_config.resolve(challenge_category)
    worker_profiles = wc.get("worker_profiles") or []
    # Observer is strictly read-only and settings-only. Deep audit used to
    # launch an isolated CLI worker here; that violated the role boundary even
    # though the worker had no graph writer. Reviewer/Target audit APIs own any
    # future write-capable audit workflow.
    if (
        body.get("deep_audit")
        or str(body.get("mode") or "").strip().lower() in {"audit", "deep_audit", "deep-audit"}
        or body.get("worker_backend") is not None
        or body.get("profile") is not None
        or body.get("engine") is not None
    ):
        return JSONResponse(
            {"error": "observer_read_only", "detail": "Observer only supports the bounded read-only evidence snapshot."},
            status_code=400,
        )

    async def evidence_stream():
        this_task = asyncio.current_task()
        cancelled = False
        pack: dict[str, Any] | None = None
        if this_task is not None:
            limiter.acquire(run_id, this_task)
        try:
            yield {"data": json.dumps({"status": "正在整理只读证据…"}, ensure_ascii=False)}
            pack = await asyncio.wait_for(
                asyncio.to_thread(
                    build_btw_evidence_pack_sync,
                    graph_db_path=str(graph_db),
                    jsonl_path=str(jsonl_path),
                    challenge_id=run_id,
                    challenge_name=challenge_name,
                    challenge_category=challenge_category,
                    run_meta=meta,
                    board_path=str(board_path),
                    winner_path=str(winner_path),
                    arts_path=str(arts_path),
                    uploads_path=str(uploads_path),
                ),
                timeout=5.0,
            )
            yield {"data": json.dumps({"status": "正在生成观察结论…"}, ensure_ascii=False)}
            configured_base = (
                os.environ.get("DSWARM_BTW_BASE_URL")
                or os.environ.get("DSWARM_DEEPSEEK_BASE_URL")
                or ""
            ).strip()
            from apps.web.reason_llm import resolve_reason_llm_endpoint
            cfg = mgr.worker_config.get()
            profiles_cfg = cfg.get("llm_profiles") if isinstance(cfg.get("llm_profiles"), dict) else {}
            binding_snapshot = getattr(run, "binding_snapshot", None)
            snapshot_roles = binding_snapshot.get("roles") if isinstance(binding_snapshot, dict) else None
            snapshot_fallbacks = binding_snapshot.get("fallback_candidates") if isinstance(binding_snapshot, dict) else None
            if isinstance(snapshot_roles, dict) and isinstance(snapshot_roles.get("observer"), dict):
                observer_profile = dict(snapshot_roles["observer"])
                observer_fallback_profile = dict(snapshot_fallbacks.get("observer") or {}) if isinstance(snapshot_fallbacks, dict) else {}
            else:
                observer_profile = profiles_cfg.get("observer")
                observer_fallback_profile = profiles_cfg.get("observer_fallback")
            messages = btw_evidence_messages(
                question=question, pack=pack, transcript=transcript,
                context_hint=context_hint,
            )
            read_timeout = max(8.0, _env_float("DSWARM_BTW_LLM_READ_TIMEOUT", 20.0))
            overall_timeout = max(read_timeout, _env_float("DSWARM_BTW_LLM_OVERALL_TIMEOUT", 35.0))

            async def _call_observer(
                profile: Any, profile_id: str,
            ) -> tuple[dict[str, Any] | None, str]:
                """Call exactly one configured read-only Observer binding."""
                if not isinstance(profile, dict) or not profile:
                    return None, "unconfigured"
                try:
                    resolved = resolve_reason_llm_endpoint(
                        sessions_root=mgr.sessions_root,
                        worker_profiles=worker_profiles,
                        profile=dict(profile),
                        llm_providers=cfg.get("llm_providers") or [],
                    )
                    api_key = str(resolved.get("api_key") or "").strip()
                    if not api_key:
                        return None, "missing_credentials"
                    base_url = configured_base or str(resolved.get("base_url") or "")
                    usage_writer = mgr.internal_usage_writer(
                        run, solver_id="observer", profile_id=profile_id)
                    async with LLMClient(
                        api_key=api_key, base_url=base_url, timeout=read_timeout,
                        overall_timeout=overall_timeout, usage_writer=usage_writer,
                        usage_context=usage_writer.context,
                    ) as client:
                        result = await client.chat(
                            model=str(
                                resolved.get("model")
                                or profile.get("model")
                                or btw_model_for(base_url)
                            ),
                            messages=messages, temperature=0.2,
                            max_tokens=None, stream=False,
                        )
                    if getattr(result, "finish_reason", "") == "timeout":
                        return None, "timeout"
                    content = str(getattr(result, "content", "") or "").strip()
                    if not content:
                        return None, "empty"
                    parsed = parse_btw_structured_answer(content, pack)
                    return {
                        "final": parsed["answer_markdown"],
                        "answer_type": parsed["answer_type"],
                        "evidence_refs": parsed["evidence_refs"],
                        "uncertainties": parsed["uncertainties"],
                    }, ""
                except Exception as exc:  # noqa: BLE001 - try the explicit backup only
                    if _btw_timeout_exception(exc) or "timeout" in str(exc).lower():
                        return None, "timeout"
                    return None, type(exc).__name__
            payload, primary_failure = await _call_observer(
                observer_profile, "observer")
            observer_source = "default"
            fallback_used = False
            fallback_failure = ""
            if payload is None:
                payload, fallback_failure = await _call_observer(
                    observer_fallback_profile, "observer_fallback")
                if payload is not None:
                    observer_source = "fallback"
                    fallback_used = True
                else:
                    # Keep diagnostics internal. The public response states the
                    # isolated Observer chain is unavailable and never names or
                    # invokes Titler/Reason as an implicit fallback.
                    payload = {
                        "final": (
                            "观察员暂时不可用：默认 Observer 与配置的轻量备用均不可用；"
                            "不会回退到 Titler 或 Reason。"
                        ),
                        "answer_type": "insufficient",
                        "evidence_refs": [],
                        "uncertainties": [],
                    }
                    if primary_failure == "timeout" or fallback_failure == "timeout":
                        payload["final"] = "观察员只读总结请求超时；默认与轻量备用均未及时返回。"
                    observer_source = "unavailable"

            # Every normal Observer response carries machine-resolvable snapshot
            # references, even when both configured bindings are unavailable.
            payload.setdefault("observer", "observer")
            payload.setdefault(
                "snapshot_event_seq",
                max(
                    (
                        int(item.get("seq") or 0)
                        for item in pack.get("events", [])
                        if isinstance(item, dict)
                    ),
                    default=0,
                ),
            )
            if not isinstance(payload.get("evidence_refs"), list):
                payload["evidence_refs"] = []
            if not isinstance(payload.get("uncertainties"), list):
                payload["uncertainties"] = []
            payload["observer_source"] = observer_source
            payload["fallback_used"] = fallback_used
            yield {"data": json.dumps(payload, ensure_ascii=False)}
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:  # noqa: BLE001
            raw_error = str(exc).strip()
            lowered = raw_error.lower()
            if "401" in raw_error or "api key" in lowered or "authentication" in lowered:
                msg = _BTW_AUTH_FAILURE
            elif _btw_timeout_exception(exc) or "timeout" in lowered:
                msg = "观察员只读总结请求超时；证据包可能已生成，但模型没有及时返回。"
            else:
                msg = "观察员暂时无法完成只读总结：" + (raw_error or type(exc).__name__)[:180]
            yield {"data": json.dumps({"final": msg, "answer_type": "insufficient", "evidence_refs": [], "observer": "observer", "observer_source": "unavailable", "fallback_used": False}, ensure_ascii=False)}
        finally:
            if this_task is not None:
                limiter.release(run_id, this_task)
        if not cancelled:
            yield {"data": json.dumps({"done": True}, ensure_ascii=False)}

    return EventSourceResponse(evidence_stream(), ping=10)
