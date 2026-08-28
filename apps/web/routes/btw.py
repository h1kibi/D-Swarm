"""BTW side-query observer route."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from apps.web.http_utils import _btw_timeout_exception, _env_float, _env_int, _require_dict_body
from apps.web.run_manager import RunManager

router = APIRouter(prefix="/api/runs", tags=["btw"])

@router.post("/{run_id}/btw")
async def btw(run_id: str, request: Request) -> Any:
    from apps.web.drivers import (
        _planner_llm_credentials,
        _standby_profile_for,
    )
    from apps.web.worker_config import backend_for_profile, resolve_worker_backend
    from dswarm.core.runtime_env import is_web_container
    from dswarm.solver.btw import (
        BtwLimiter,
        BtwWorkerPaths,
        BTW_MODEL,
        build_btw_evidence_pack_sync,
        build_btw_worker_prompt,
        btw_evidence_messages,
        parse_btw_structured_answer,
        run_meta_dict,
        sanitize_transcript,
        stream_btw_worker_deltas,
    )
    from dswarm.solver.cli_driver import driver_for
    from dswarm.solver.credential_accounts import (
        account_store_root,
        runtime_env_for_engine,
    )
    from dswarm.solver.worker_profiles import base_engine_for_profile

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
    safe = run_id.replace("/", "_").replace("..", "_")
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
    runtime_profiles = wc.get("runtime_profiles") or []

    # Normal BTW is a bounded evidence-pack summary. A shell worker is an
    # explicit deep-audit mode only; this prevents a routine side question
    # from starting a tool-using CLI for minutes.
    deep_audit = bool(body.get("deep_audit")) or str(body.get("mode") or "").strip().lower() in {
        "audit", "deep_audit", "deep-audit",
    } or body.get("worker_backend") is not None
    if not deep_audit:
        async def evidence_stream():
            this_task = asyncio.current_task()
            cancelled = False
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
                from dswarm.core.llm import LLMClient
                from dswarm.solver.btw import _BTW_AUTH_FAILURE

                configured_base = (
                    os.environ.get("DSWARM_BTW_BASE_URL")
                    or os.environ.get("DSWARM_DEEPSEEK_BASE_URL")
                    or ""
                ).strip()
                account_key, account_base = _planner_llm_credentials(
                    sessions_root=mgr.sessions_root,
                    worker_profiles=worker_profiles,
                    planner_base=configured_base,
                )
                api_key = (
                    os.environ.get("DSWARM_BTW_API_KEY")
                    or os.environ.get("DSWARM_DEEPSEEK_API_KEY")
                    or os.environ.get("DEEPSEEK_API_KEY")
                    or account_key
                    or ""
                )
                base_url = configured_base or account_base or "https://api.deepseek.com/v1"
                if not api_key:
                    payload = {
                        "final": "观察员暂时无法回答：旁路总结服务未配置 API 凭据。请检查 BTW/DeepSeek provider 配置。",
                        "answer_type": "insufficient", "evidence_refs": [],
                    }
                else:
                    messages = btw_evidence_messages(
                        question=question,
                        pack=pack,
                        transcript=transcript,
                        context_hint=context_hint,
                    )
                    # The previous 8s per-read timeout was shorter than the
                    # 25s BTW wall-clock budget. Reasoning models can spend
                    # several seconds before emitting the first byte, so the
                    # transport raised httpx.ReadTimeout prematurely. Keep a
                    # bounded request, but leave the read timeout enough room
                    # for the model to start and finish. Both values remain
                    # operator-configurable for slower/faster gateways.
                    read_timeout = max(8.0, _env_float("DSWARM_BTW_LLM_READ_TIMEOUT", 20.0))
                    overall_timeout = max(
                        read_timeout,
                        _env_float("DSWARM_BTW_LLM_OVERALL_TIMEOUT", 35.0),
                    )
                    btw_usage_writer = mgr.internal_usage_writer(
                        run, solver_id="btw", profile_id="btw",
                    )
                    async with LLMClient(
                        api_key=api_key,
                        base_url=base_url,
                        timeout=read_timeout,
                        overall_timeout=overall_timeout,
                        usage_writer=btw_usage_writer,
                        usage_context=btw_usage_writer.context,
                    ) as client:
                        result = await client.chat(
                            model=BTW_MODEL,
                            messages=messages,
                            temperature=0.2,
                            # Reasoning models spend the output budget on
                            # reasoning_content before emitting the structured
                            # answer. A small fixed cap can therefore return
                            # finish_reason=length with empty content. Omit the
                            # cap and let the model endpoint use its own limit;
                            # the prompt still requires a concise JSON answer.
                            max_tokens=None,
                            stream=False,
                        )
                    if result.finish_reason == "timeout":
                        answer = f"观察员暂时无法在 {overall_timeout:.0f} 秒内完成只读总结；没有启动深度 worker。可稍后重试，或主动选择深度审计。"
                        payload = {"final": answer, "answer_type": "insufficient", "evidence_refs": []}
                    elif not result.content.strip():
                        answer = "观察员没有返回可读结论；证据包已生成，但模型响应为空。没有启动深度 worker。"
                        payload = {"final": answer, "answer_type": "insufficient", "evidence_refs": []}
                    else:
                        parsed = parse_btw_structured_answer(result.content, pack)
                        payload = {
                            "final": parsed["answer_markdown"],
                            "answer_type": parsed["answer_type"],
                            "evidence_refs": parsed["evidence_refs"],
                            "uncertainties": parsed["uncertainties"],
                        }
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
                    msg = "观察员只读总结请求超时；证据包可能已生成，但模型没有及时返回。没有启动深度 worker。"
                else:
                    detail = raw_error or type(exc).__name__
                    prefix = "观察员暂时无法完成只读总结："
                    msg = detail if detail.startswith(prefix) else prefix + detail[:180]
                # `final` is the user-facing failure answer. Do not also emit
                # the same text as `error`, otherwise the UI renders it twice
                # (once in the assistant bubble and once in the error banner).
                yield {"data": json.dumps({
                    "final": msg,
                    "answer_type": "insufficient", "evidence_refs": [],
                }, ensure_ascii=False)}
            finally:
                if this_task is not None:
                    limiter.release(run_id, this_task)
            if not cancelled:
                yield {"data": json.dumps({"done": True}, ensure_ascii=False)}

        return EventSourceResponse(evidence_stream(), ping=10)

    winner: dict[str, Any] = {}
    if winner_path.exists():
        try:
            raw = json.loads(winner_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                winner = raw
        except Exception:
            winner = {}
    def _pick_profile() -> tuple[dict[str, Any] | None, str]:
        requested = str(
            body.get("profile") or body.get("engine") or ""
        ).strip()
        review = wc.get("review_policy") or {}
        candidates = [
            requested,
            str(review.get("engine") or "").strip(),
            str(winner.get("engine") or "").strip(),
        ]
        for p in worker_profiles:
            if isinstance(p, dict) and p.get("enabled", True):
                roles = p.get("roles") or []
                if "respond" in roles or "review" in roles:
                    candidates.append(str(p.get("name") or p.get("id") or ""))
        candidates.extend(str(e) for e in (wc.get("engines") or []))
        for cand in candidates:
            if not cand:
                continue
            profile = _standby_profile_for(cand, worker_profiles)
            if profile is not None:
                return profile, cand
            base = base_engine_for_profile(cand)
            if base in ("pi",):
                return None, base
        return None, "pi"

    async def stream():
        # Register this generation as the run's active btw; cancel any prior.
        this_task = asyncio.current_task()
        if this_task is not None:
            limiter.acquire(run_id, this_task)
        # A CLI observer can spend its first minutes reading SQLite/artifacts
        # before it emits a complete assistant message.  Send an explicit SSE
        # status first so the UI does not look like a dead/empty reply.
        yield {"data": json.dumps({"status": "正在读取 run 证据…"}, ensure_ascii=False)}
        profile, selected = _pick_profile()
        transport = base_engine_for_profile(profile or selected)
        worker_backend = resolve_worker_backend(
            request_backend=body.get("worker_backend"),
            config_backend=wc.get("worker_backend"),
            env_backend=os.environ.get("DSWARM_WORKER_BACKEND"),
            in_web_container=is_web_container(),
        )
        backend = (
            backend_for_profile(
                profile,
                runtime_profiles=runtime_profiles,
                worker_backend=worker_backend,
                in_web_container=is_web_container(),
            )
            if profile else worker_backend
        )
        container = None
        runtime_lease = None
        account_root = account_store_root(mgr.sessions_root)
        worker_root = root / "workers" / "_btw"
        worker_root.mkdir(parents=True, exist_ok=True)
        workdir = worker_root / f"{transport}-{int(time.time() * 1000)}"
        workdir.mkdir(parents=True, exist_ok=True)
        worker_instance_id = uuid.uuid4().hex
        runtime_policy = getattr(run, "runtime_policy", None)
        strict_docker = runtime_policy is not None and runtime_policy.mode == "docker"
        try:
            if strict_docker:
                from dswarm.swarm.runtime import (
                    RuntimeSpawnRequest,
                    runtime_lease_factory_for_request,
                )

                runtime_profile_id = str(
                    (profile or {}).get("name")
                    or (profile or {}).get("id")
                    or selected
                )
                lease_factory = runtime_lease_factory_for_request(
                    snapshot=getattr(run, "runtime_snapshot", None),
                    pool_manager=getattr(run, "pool_manager", None),
                    request=RuntimeSpawnRequest(
                        profile_id=runtime_profile_id,
                        worker_instance_id=worker_instance_id,
                        operation_kind="btw",
                        mode="btw",
                    ),
                )
                runtime_lease = await lease_factory(worker_instance_id, "btw")
                container = runtime_lease.executor
                worker_env = dict(runtime_lease.worker_env)
            elif backend == "container":
                # Container execution is manager-owned. Old/no-policy runs cannot
                # create a second run-global container from this side route.
                from dswarm.solver.container_pool import RuntimeFailure

                raise RuntimeFailure(
                    category="configuration", code="runtime_pool_unavailable"
                )
            else:
                # Explicit local-dev runs retain the host path; Docker policy can
                # never reach it because the strict branch above is authoritative.
                worker_env = runtime_env_for_engine(
                    transport,
                    account_root=account_root,
                    account_id=(
                        profile.get("credential_account") if profile else None
                    ),
                    container=False,
                ).env

            def _worker_path(p: Path) -> str:
                if container is not None:
                    mapper = getattr(container, "to_container_path", None)
                    if callable(mapper):
                        return str(mapper(str(p)))
                return str(p)

            prompt = build_btw_worker_prompt(
                question=question,
                paths=BtwWorkerPaths(
                    workspace=_worker_path(root),
                    jsonl=_worker_path(jsonl_path),
                    graph_db=_worker_path(graph_db),
                    board=_worker_path(board_path),
                    winner=_worker_path(winner_path),
                    arts=_worker_path(arts_path),
                    uploads=_worker_path(uploads_path),
                ),
                challenge_id=run_id,
                challenge_name=challenge_name,
                challenge_category=challenge_category,
                run_state=str(meta.get("state") or ""),
                context_hint=context_hint,
                transcript=transcript,
            )
            worker_env["DSWARM_BTW_WORKER"] = "1"
            # BTW remains a read-only observer even though its credential and
            # gateway token are projected by the same pool manager as workers.
            worker_env["DSWARM_BLACKBOARD_DB"] = ""
            if profile:
                worker_env.setdefault(
                    "DSWARM_WORKER_PROFILE_ID", str(profile.get("id") or "")
                )
                worker_env.setdefault(
                    "DSWARM_CREDENTIAL_ACCOUNT_ID",
                    str(profile.get("credential_account") or ""),
                )
                if profile.get("model"):
                    worker_env.setdefault("DSWARM_WORKER_MODEL", str(profile["model"]))

            async for chunk in stream_btw_worker_deltas(
                driver=driver_for(profile or transport),
                prompt=prompt,
                cwd=str(workdir),
                timeout=_env_int("DSWARM_BTW_WORKER_TIMEOUT", 240),
                env=worker_env,
                container=container,
                web_access=False,
                kb_access=False,
            ):
                if await request.is_disconnected():
                    break
                yield {"data": json.dumps({"delta": chunk}, ensure_ascii=False)}
        except asyncio.CancelledError:
            # limiter cancel or client disconnect — stop cleanly.
            pass
        except Exception as e:  # noqa: BLE001
            raw_error = str(e).strip()
            if _btw_timeout_exception(e) or "timeout" in raw_error.lower():
                detail = "观察员深度审计请求超时；旁路 worker 没有及时返回。"
            else:
                detail = raw_error or type(e).__name__
            yield {"data": json.dumps({"error": detail[:300]}, ensure_ascii=False)}
        finally:
            if runtime_lease is not None:
                await runtime_lease.release()
            this_task = asyncio.current_task()
            if this_task is not None:
                limiter.release(run_id, this_task)
            try:
                import shutil
                workdir_resolved = workdir.resolve()
                worker_root_resolved = worker_root.resolve()
                workdir_resolved.relative_to(worker_root_resolved)
                shutil.rmtree(workdir_resolved)
            except Exception:
                # Cleanup must never mask the observer answer or turn a
                # completed BTW request into a 500.
                pass
        yield {"data": json.dumps({"done": True}, ensure_ascii=False)}

    return EventSourceResponse(stream(), ping=10)
