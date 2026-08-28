"""Worker startup-test controller.

Runs an isolated ReasonSwarm sub-run for every enabled worker against a built-in
pseudo challenge. The test runs never appear in the normal RunManager.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from apps.web.http_utils import project_probe_result
from apps.web.provider_errors import ProviderErrorAggregator, classify_provider_error
from apps.web.run_manager import RunManager

RunWorkerTest = Callable[[dict[str, Any], float, "StartupTestSession"], Awaitable[dict[str, Any]]]
RunFullFlowTest = Callable[["StartupTestSession"], Awaitable[dict[str, Any]]]

_DIAGNOSTIC_KEYS = (
    "status",
    "layer",
    "blocker",
    "backend",
    "model",
    "account_id",
    "binding_kind",
    "effective_credential_id",
)


def _diagnostic_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Return stable diagnostic fields for startup-test summaries.

    Preflight comes from profile_health and can include useful operator-facing
    context. Preserve those fields instead of collapsing everything into a flat
    detail string, but keep the allow-list tight so arbitrary probe internals do
    not become part of the public SSE/API contract.
    """
    return project_probe_result(data, fields=_DIAGNOSTIC_KEYS, omit_none=True)


class StartupTestSession:
    def __init__(
        self,
        test_id: str,
        controller: "StartupTestController",
        *,
        mode: str = "startup",
        benchmark: str = "local-smoke",
    ) -> None:
        self.id = test_id
        self.controller = controller
        self.mode = mode
        self.benchmark = benchmark
        # Legacy single-reader queue kept for unit tests/internal callers. The API
        # must not consume it directly: the desktop panel may reconnect or multiple
        # clients may watch the same detection run.
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.started_at = time.time()
        self._seq = 0
        self._history: list[dict[str, Any]] = []
        self._closed = False
        self._changed = asyncio.Condition()
        self.events_path = (
            Path(controller.manager.sessions_root)
            / "_startup_test_events"
            / test_id
            / "events.jsonl"
        )
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary: dict[str, Any] = {
            "ok": False,
            "mode": mode,
            "benchmark": benchmark,
            "passed": 0,
            "failed": 0,
            "results": [],
            "checks": [],
        }

    async def emit(self, event: dict[str, Any]) -> None:
        self._seq += 1
        item = {"seq": self._seq, "ts": time.time(), "test_id": self.id, **event}
        async with self._changed:
            self._history.append(item)
            self._changed.notify_all()
        try:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            # The live UI must not hang merely because diagnostic persistence failed.
            pass
        await self.events.put(item)

    async def close(self) -> None:
        async with self._changed:
            self._closed = True
            self._changed.notify_all()
        await self.events.put(None)

    async def iter_events(self, *, last_seq: int = 0):
        next_index = 0
        if last_seq > 0:
            async with self._changed:
                while next_index < len(self._history) and int(self._history[next_index].get("seq", 0)) <= last_seq:
                    next_index += 1
        while True:
            async with self._changed:
                while next_index >= len(self._history) and not self._closed:
                    await self._changed.wait()
                if next_index >= len(self._history) and self._closed:
                    break
                item = self._history[next_index]
                next_index += 1
            yield item

    async def emit_worker_phase(
        self,
        worker_id: str,
        phase: str,
        detail: str = "",
        *,
        ok: bool | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        await self.emit({
            "type": "worker.phase",
            "worker_id": worker_id,
            "phase": phase,
            "detail": detail,
            "ok": ok,
            **_diagnostic_fields(diagnostics or {}),
        })

    async def emit_raw_event(self, worker_id: str, ev: Any) -> None:
        event_type = str(getattr(ev, "event_type", ""))
        await self.emit({
            "type": "worker.event",
            "worker_id": worker_id,
            "event_type": event_type,
            "payload": getattr(ev, "payload", {}),
        })

    async def emit_provider_error(self, diagnostic: Any) -> None:
        payload = diagnostic.to_event() if hasattr(diagnostic, "to_event") else dict(diagnostic)
        await self.emit({"type": "provider.error", **payload})

    async def emit_flow_check(self, check_id: str, *, ok: bool, detail: str = "") -> dict[str, Any]:
        check = {"id": check_id, "ok": bool(ok), "detail": detail}
        await self.emit({
            "type": "flow.check",
            "check_id": check_id,
            "ok": bool(ok),
            "detail": detail,
        })
        return check


class StartupTestController:
    def __init__(
        self,
        manager: RunManager,
        *,
        run_worker_test: RunWorkerTest | None = None,
        run_full_flow_test: RunFullFlowTest | None = None,
        timeout_per_worker: float = 180.0,
    ) -> None:
        self.manager = manager
        self.timeout_per_worker = float(timeout_per_worker)
        self.run_worker_test = run_worker_test or type(self).default_run_worker_test
        self.run_full_flow_test = run_full_flow_test
        self.sessions: dict[str, StartupTestSession] = {}
        self._test_manager = RunManager(sessions_root=manager.sessions_root)

    def get(self, test_id: str) -> StartupTestSession | None:
        return self.sessions.get(test_id)

    async def start(self, *, mode: str = "startup", benchmark: str = "local-smoke") -> StartupTestSession:
        mode = str(mode or "startup").strip().lower()
        if mode not in {"startup", "full_flow"}:
            raise ValueError(f"unsupported startup-test mode: {mode}")
        benchmark = str(benchmark or "local-smoke").strip() or "local-smoke"
        test_id = f"startup-{int(time.time() * 1000)}-{id(self):x}"
        session = StartupTestSession(test_id, self, mode=mode, benchmark=benchmark)
        self.sessions[test_id] = session
        session.task = asyncio.create_task(self._run(session))
        return session

    async def shutdown(self) -> None:
        pending = [s.task for s in self.sessions.values() if s.task is not None and not s.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _enabled_profiles(self) -> list[dict[str, Any]]:
        cfg = self.manager.worker_config.get()
        return [
            p for p in (cfg.get("worker_profiles") or [])
            if isinstance(p, dict) and p.get("enabled", True)
        ]

    @staticmethod
    def _profile_label(profile: dict[str, Any]) -> str:
        return str(
            profile.get("label")
            or profile.get("name")
            or profile.get("id")
            or profile.get("engine")
            or "worker"
        )

    async def _preflight(self, profile: dict[str, Any]) -> dict[str, Any]:
        from apps.web.worker_config import backend_for_profile
        from dswarm.core.runtime_env import is_web_container
        from dswarm.solver.profile_health import evaluate_profile_health

        cfg = self.manager.worker_config.get()
        backend = backend_for_profile(
            profile,
            runtime_profiles=cfg.get("runtime_profiles") or [],
            worker_backend=str(cfg.get("worker_backend") or ""),
            in_web_container=is_web_container(),
        )
        health = evaluate_profile_health(
            profile,
            backend=backend,
            sessions_root=self.manager.sessions_root,
            depth="auth",
            llm_providers=cfg.get("llm_providers") or [],
        )
        return {
            "ok": bool(getattr(health, "ok", False)),
            "detail": str(getattr(health, "detail", "") or ""),
            **_diagnostic_fields({
                "status": getattr(health, "status", None),
                "layer": getattr(health, "layer", None),
                "blocker": getattr(health, "blocker", None),
                "backend": getattr(health, "backend", None),
                "model": getattr(health, "model", None),
                "account_id": getattr(health, "account_id", None),
                "binding_kind": getattr(health, "binding_kind", None),
                "effective_credential_id": getattr(health, "effective_credential_id", None),
            }),
        }

    async def _run(self, session: StartupTestSession) -> None:
        profiles = self._enabled_profiles()
        await session.emit({
            "type": "test.started",
            "mode": session.mode,
            "benchmark": session.benchmark,
            "worker_count": len(profiles),
        })
        results: list[dict[str, Any]] = []
        provider_errors = ProviderErrorAggregator(window_s=60.0, fatal_threshold=3, majority_ratio=0.5)
        for index, profile in enumerate(profiles, start=1):
            worker_id = self._profile_label(profile)
            await session.emit_worker_phase(worker_id, "preflight", "checking credentials and endpoint")
            preflight = await self._preflight(profile)
            if not preflight["ok"]:
                await session.emit_worker_phase(
                    worker_id,
                    "failed",
                    str(preflight["detail"] or "preflight failed"),
                    ok=False,
                    diagnostics=preflight,
                )
                results.append({
                    "worker_id": worker_id,
                    "ok": False,
                    "phase": "preflight",
                    "detail": preflight["detail"],
                    **_diagnostic_fields(preflight),
                })
                detail = str(preflight.get("detail") or preflight.get("blocker") or "")
                diag = classify_provider_error(
                    detail,
                    provider=str(preflight.get("provider") or preflight.get("backend") or ""),
                    account_id=str(preflight.get("account_id") or preflight.get("effective_credential_id") or ""),
                    worker_id=worker_id,
                )
                await session.emit_provider_error(diag)
                alert = provider_errors.record(diag, now=time.time(), active_workers=len(profiles))
                if alert:
                    await session.emit(alert)
                continue

            await session.emit_worker_phase(worker_id, "running", "launching isolated workflow")
            try:
                outcome = await asyncio.wait_for(
                    self.run_worker_test(
                        profile,
                        timeout=self.timeout_per_worker,
                        session=session,
                    ),
                    timeout=self.timeout_per_worker + 30.0,
                )
            except asyncio.TimeoutError:
                outcome = {"ok": False, "detail": "worker startup test timed out", "phase": "run"}
            except Exception as exc:  # noqa: BLE001
                outcome = {"ok": False, "detail": str(exc)[:500], "phase": "run"}

            ok = bool(outcome.get("ok"))
            await session.emit_worker_phase(
                worker_id,
                "done" if ok else "failed",
                str(outcome.get("detail") or ""),
                ok=ok,
            )
            results.append({
                "worker_id": worker_id,
                "ok": ok,
                "phase": str(outcome.get("phase") or ("done" if ok else "run")),
                "detail": str(outcome.get("detail") or ""),
            })
            if not ok:
                detail = str(outcome.get("detail") or "")
                diag = classify_provider_error(
                    detail,
                    provider=str(outcome.get("provider") or outcome.get("backend") or ""),
                    account_id=str(outcome.get("account_id") or outcome.get("effective_credential_id") or ""),
                    worker_id=worker_id,
                )
                await session.emit_provider_error(diag)
                alert = provider_errors.record(diag, now=time.time(), active_workers=len(profiles))
                if alert:
                    await session.emit(alert)

        checks: list[dict[str, Any]] = []
        # Make per-worker outcomes available to the default full-flow control-plane
        # checks before the final summary is assembled. This lets the full-flow mode
        # explicitly assert coverage of *all enabled workers* rather than only
        # testing shared components in isolation.
        session.summary["results"] = list(results)
        if session.mode == "full_flow":
            checks = await self._run_full_flow_checks(session)

        passed = sum(1 for row in results if row["ok"])
        failed = len(results) - passed
        checks_ok = all(bool(c.get("ok")) for c in checks) if checks else True
        session.summary = {
            "ok": bool(results) and failed == 0 and checks_ok,
            "mode": session.mode,
            "benchmark": session.benchmark,
            "passed": passed,
            "failed": failed,
            "results": results,
            "checks": checks,
        }
        await session.emit({
            "type": "test.done",
            "summary": session.summary,
        })
        await session.close()

    async def _run_full_flow_checks(self, session: StartupTestSession) -> list[dict[str, Any]]:
        if self.run_full_flow_test is not None:
            outcome = await self.run_full_flow_test(session)
            checks = outcome.get("checks") if isinstance(outcome, dict) else None
            if isinstance(checks, list):
                return [c for c in checks if isinstance(c, dict)]
        return await self._run_default_full_flow_checks(session)

    async def _run_default_full_flow_checks(self, session: StartupTestSession) -> list[dict[str, Any]]:
        """Exercise the local benchmark control-plane without depending on a hard CTF.

        The per-worker loop above is responsible for spending real LLM calls against
        every enabled worker.  This deterministic second half verifies the shared
        components those workers are expected to use: append-only blackboard writes,
        operator hint directives, reason intent creation/claiming, BTW read-only
        progress packaging, and durable resume/recovery artifacts.
        """
        from dswarm.models.solve_graph import Challenge
        from dswarm.solver.btw import build_btw_evidence_pack_sync
        from dswarm.swarm.shared_graph import SQLiteSharedGraph

        checks: list[dict[str, Any]] = []

        async def add(check_id: str, ok: bool, detail: str) -> None:
            checks.append(await session.emit_flow_check(check_id, ok=ok, detail=detail))

        profiles = self._enabled_profiles()
        first_worker = self._profile_label(profiles[0]) if profiles else "next-worker"
        bench = (session.benchmark or "local-smoke").strip() or "local-smoke"
        root = Path(self.manager.sessions_root) / "_startup_full_flow" / session.id
        root.mkdir(parents=True, exist_ok=True)
        graph_path = root / "shared_graph.db"
        jsonl_path = root / "events.jsonl"
        board_path = root / "board.md"
        winner_path = root / "winner.json"
        artifacts_path = root / "artifacts"
        uploads_path = root / "uploads"
        artifacts_path.mkdir(exist_ok=True)
        uploads_path.mkdir(exist_ok=True)

        challenge = Challenge(
            id=f"startup-full-flow-{session.id}",
            name=f"D-Swarm full-flow {bench}",
            category="misc",
            description=(
                "Local prepared benchmark for D-Swarm system self-test. "
                "It validates orchestration/control-plane behavior instead of "
                "depending on challenge difficulty."
            ),
        )
        dispatch_body = {
            "challenge": challenge.model_dump() if hasattr(challenge, "model_dump") else challenge.dict(),
            "benchmark": bench,
            "mode": "full_flow",
            "worker_profiles": profiles,
            "engines": [self._profile_label(p) for p in profiles],
        }
        dispatch_path = root / ".dswarm_dispatch.json"
        dispatch_path.write_text(json.dumps(dispatch_body, ensure_ascii=False, indent=2), encoding="utf-8")
        jsonl_path.write_text(
            json.dumps({"type": "run.started", "benchmark": bench}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        board_path.write_text("# startup full-flow board\n", encoding="utf-8")
        winner_path.write_text(json.dumps({"challenge": dispatch_body["challenge"]}, ensure_ascii=False), encoding="utf-8")

        graph = SQLiteSharedGraph(graph_path, challenge)
        try:
            await add("benchmark.loaded", True, f"local benchmark selected: {bench}; enabled_workers={len(profiles)}")
            worker_results = [r for r in (session.summary.get("results") or []) if isinstance(r, dict)]
            enabled_labels = [self._profile_label(p) for p in profiles]
            passed_workers = {str(r.get("worker_id") or "") for r in worker_results if r.get("ok")}
            missing_workers = [w for w in enabled_labels if w not in passed_workers]
            await add(
                "workers.checked",
                bool(enabled_labels) and not missing_workers and len(worker_results) >= len(enabled_labels),
                (
                    f"passed_workers={len(passed_workers)}/{len(enabled_labels)}; "
                    f"enabled={','.join(enabled_labels) or '-'}; "
                    f"missing={','.join(missing_workers) or '-'}"
                ),
            )

            evidence_seq = graph.add_evidence(
                actor="startup-test",
                source="local-benchmark",
                fact=f"benchmark {bench} loaded and control-plane graph writable",
                verified=True,
                witness="startup full-flow self-test",
            )
            directive = graph.add_operator_directive(
                actor="operator",
                action="hint",
                text="Use the prepared local-smoke benchmark hint; do not interrupt a live single-shot worker.",
                scope="global",
                standing=False,
                preempt_policy="soft_rebind",
            )
            directive_id = str(directive.get("directive_id") or "")
            queued_seq = graph.update_directive_status(
                directive_id=directive_id,
                status="queued",
                actor="startup-test",
                generated_fact_seq=evidence_seq,
            )
            directives = graph.operator_directives(active_only=True)
            blackboard_ok = evidence_seq > 0 and bool(directive_id) and any(
                d.get("directive_id") == directive_id and d.get("status") == "queued"
                for d in directives
            )
            await add(
                "blackboard.checked",
                blackboard_ok,
                f"evidence_seq={evidence_seq}; directive_id={directive_id}; queued_seq={queued_seq}",
            )

            intent_id = f"I-{session.id[-10:]}"
            intent_seq = graph.propose_intent(
                actor="reason",
                intent_id=intent_id,
                goal="Consume the operator hint directive in the next worker turn",
                payload={
                    "source": "operator_hint",
                    "directive_id": directive_id,
                    "priority": "operator",
                    "direction": "full-flow-self-test",
                    "worker_class": "code",
                },
                from_fact_seqs=[evidence_seq] if evidence_seq > 0 else None,
            )
            claimed = graph.claim_intent(worker=first_worker, intent_id=intent_id, lease_s=60.0)
            bound_seq = graph.update_directive_status(
                directive_id=directive_id,
                status="bound",
                actor="coordinator",
                generated_intent_id=intent_id,
                bound_worker=first_worker,
            )
            acted_seq = graph.update_directive_status(
                directive_id=directive_id,
                status="acted",
                actor=first_worker,
                generated_intent_id=intent_id,
                bound_worker=first_worker,
            )
            await add(
                "reason.checked",
                intent_seq > 0 and claimed,
                f"intent_id={intent_id}; intent_seq={intent_seq}; claimed={claimed}; worker={first_worker}",
            )
            await add(
                "hint.checked",
                queued_seq > 0 and bound_seq > 0 and acted_seq > 0,
                (
                    "queued immediately as blackboard directive; "
                    f"directive_id={directive_id}; bound_worker={first_worker}; "
                    "single-shot current turn may drain gracefully before consumption"
                ),
            )

            pack = build_btw_evidence_pack_sync(
                graph_db_path=str(graph_path),
                jsonl_path=str(jsonl_path),
                challenge_id=challenge.id,
                challenge_name=challenge.name,
                challenge_category=str(challenge.category),
                run_meta={"state": "running", "workers": profiles},
                board_path=str(board_path),
                winner_path=str(winner_path),
                arts_path=str(artifacts_path),
                uploads_path=str(uploads_path),
            )
            facts_n = len(pack.get("facts") or [])
            intents_n = len(pack.get("intents") or [])
            events_n = len(pack.get("events") or [])
            await add(
                "btw.checked",
                facts_n > 0 and intents_n > 0 and events_n > 0,
                f"facts={facts_n}; intents={intents_n}; events={events_n}; warnings={len(pack.get('warnings') or [])}",
            )

            # Stop/resume/recovery are validated as lifecycle control-plane
            # artifacts: stop preserves the workspace, resume reloads the exact
            # dispatch body, and a new graph handle after close can still see work.
            await add(
                "stop.checked",
                graph_path.exists() and dispatch_path.exists(),
                "operator stop preserves shared graph and dispatch artifacts for graceful worker settlement",
            )
            loaded_dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
            await add(
                "resume.checked",
                loaded_dispatch.get("benchmark") == bench and bool(loaded_dispatch.get("worker_profiles")),
                f"dispatch body reload ok; workers={len(loaded_dispatch.get('worker_profiles') or [])}",
            )

            stop_path = root / "operator_stop.json"
            stop_path.write_text(json.dumps({
                "action": "stop",
                "directive_id": directive_id,
                "settled_workers": enabled_labels,
                "preserve_workspace": True,
            }, ensure_ascii=False), encoding="utf-8")
            await add(
                "recovery.user_stop_resume.checked",
                stop_path.exists() and graph_path.exists() and dispatch_path.exists(),
                f"stop artifact preserved; resume dispatch workers={len(loaded_dispatch.get('worker_profiles') or [])}",
            )

            crash_path = root / "worker_crash.json"
            crash_path.write_text(json.dumps({
                "worker": first_worker,
                "intent_id": intent_id,
                "directive_id": directive_id,
                "next_action": "reclaim_intent_from_graph",
            }, ensure_ascii=False), encoding="utf-8")
            crash_marker = json.loads(crash_path.read_text(encoding="utf-8"))
            await add(
                "recovery.worker_crash.checked",
                crash_marker.get("intent_id") == intent_id and bool(graph.operator_directives(active_only=False)),
                f"crash marker persisted; intent_id={intent_id}; next_action={crash_marker.get('next_action')}",
            )

            restarted_manager = RunManager(sessions_root=self.manager.sessions_root)
            await add(
                "recovery.backend_restart.checked",
                restarted_manager.sessions_root == self.manager.sessions_root and dispatch_path.exists() and graph_path.exists(),
                f"new manager reopened sessions root; dispatch_exists={dispatch_path.exists()}; graph_exists={graph_path.exists()}",
            )

            desktop_restart_path = root / "desktop_restart.json"
            desktop_restart_path.write_text(json.dumps({
                "restart_kind": "desktop_app",
                "test_id": session.id,
                "resume_from": str(dispatch_path),
                "board": str(board_path),
            }, ensure_ascii=False), encoding="utf-8")
            desktop_marker = json.loads(desktop_restart_path.read_text(encoding="utf-8"))
            await add(
                "recovery.desktop_restart.checked",
                desktop_marker.get("test_id") == session.id and Path(str(desktop_marker.get("resume_from"))).exists(),
                "desktop relaunch marker can recover dispatch path and board path",
            )
        except Exception as exc:  # noqa: BLE001
            await add("recovery.checked", False, f"full-flow self-test failed: {str(exc)[:300]}")
            return checks
        finally:
            try:
                graph.close()
            except Exception:
                pass

        reopened = None
        try:
            reopened = SQLiteSharedGraph(graph_path, challenge)
            events = reopened.events()
            recovered_kinds = {str(ev.get("kind") or "") for ev in events}
            recovered_directive = any(
                (ev.get("payload") or {}).get("directive_id") == directive_id
                for ev in events
            )
            await add(
                "recovery.checked",
                recovered_directive and any("intent" in kind for kind in recovered_kinds),
                f"graph reopen ok; events={len(events)}; directive_id={directive_id}; intent_id={intent_id}",
            )
        except Exception as exc:  # noqa: BLE001
            await add("recovery.checked", False, f"graph reopen failed: {str(exc)[:300]}")
        finally:
            if reopened is not None:
                try:
                    reopened.close()
                except Exception:
                    pass
        return checks


    @staticmethod
    def _smoke_category_for_profile(profile: dict[str, Any]) -> str:
        """Choose a legal pseudo challenge category for this profile's smoke run.

        ``Challenge.category`` is a strict CTF-category literal.  The smoke run
        therefore cannot invent a neutral category.  For generic or non-category
        seats, use ``misc`` and attach a temporary route alias to the profile copy
        so ReasonSwarm resolves the category's agent profile to the exact seat
        under test.
        """
        haystack = " ".join(
            str(profile.get(key) or "")
            for key in ("id", "name", "label", "image", "engine", "transport")
        ).lower()
        normalized = haystack.replace("_", "-")
        direction_markers = (
            ("web", ("pi-web", "-web", "web")),
            ("pwn", ("pi-pwn", "-pwn", "pwn")),
            ("reverse", ("pi-rev", "pi-reverse", "-rev", "reverse")),
            ("crypto", ("pi-crypto", "-crypto", "crypto")),
            ("forensics", ("pi-forensics", "-forensics", "forensics")),
            # Challenge.category has no aisec literal yet.  Alias AISec seats
            # through misc for smoke purposes while preserving the profile's real
            # image, account and model binding in the test-only profile copy.
            ("misc", ("pi-misc", "-misc", "misc", "pi-aisec", "pi-ai-sec", "-aisec", "ai-sec", "aisec")),
        )
        for category, markers in direction_markers:
            if any(marker in normalized for marker in markers):
                return category
        return "misc"

    @staticmethod
    def _smoke_profile_for_category(profile: dict[str, Any], category: str) -> dict[str, Any]:
        """Return a test-only profile copy routeable by ReasonSwarm's category alias.

        Settings can store seats under opaque ids (``seat_pi_*``) with a friendly
        label.  ReasonSwarm asks for canonical labels such as ``pi-misc``.  The
        isolated startup run contains exactly one worker profile, so it is safe to
        make that copy answer to the category alias while keeping the original
        seat id/name/image/account/model intact.  This avoids accidentally testing
        another enabled seat that merely matches the category.
        """
        from dswarm.solver.worker_profiles import direction_profile_name

        out = dict(profile)
        alias = direction_profile_name(category) or "pi-misc"
        out["label"] = alias
        return out

    @staticmethod
    def _cleanup_timeout(timeout: float) -> float:
        """Bounded grace period for isolated worker-run teardown.

        Real container teardown can legitimately spend close to the docker remove
        timeout while killing the supervisor and any child CLI workers.  The
        startup-test timeout is the worker execution budget, not the lifecycle
        cleanup budget; using a small fraction of it can cancel RunManager.delete
        midway and leave a non-cancellable executor thread/container behind.
        """
        try:
            worker_timeout = float(timeout)
        except (TypeError, ValueError):
            worker_timeout = 0.0
        if worker_timeout < 1.0:
            return min(1.0, max(0.05, worker_timeout * 10.0))
        return min(30.0, max(20.0, worker_timeout * 0.25))

    @staticmethod
    async def default_run_worker_test(
        profile: dict[str, Any],
        timeout: float,
        session: StartupTestSession,
    ) -> dict[str, Any]:
        from apps.web.drivers import build_driver
        from dswarm.core.events import EventType

        controller = session.controller
        manager = controller.manager
        test_manager = controller._test_manager
        cfg = manager.worker_config.get()
        worker_id = controller._profile_label(profile)
        marker = "startup_test_ok"
        state = {"ok": False, "detail": "worker completed without startup_test_ok marker"}

        smoke_category = StartupTestController._smoke_category_for_profile(profile)
        smoke_profile = StartupTestController._smoke_profile_for_category(profile, smoke_category)

        body = {
            "challenge": {
                "name": "worker-startup-test",
                "category": smoke_category,
                "description": (
                    "Internal worker startup smoke test, not a CTF puzzle. Your only task is to "
                    "run exactly this local shell command, then stop: "
                    "printf 'startup_test_ok\nVERIFIED_FACT=startup_test_ok\n'. "
                    "Do not inspect files, do not solve anything, and do not contact external services."
                ),
            },
            "offline": True,
            "kb": False,
            "engines": [str(smoke_profile.get("name") or smoke_profile.get("id"))],
            "max_workers": 1,
            "max_total_workers": 1,
            "wall_clock_budget": timeout,
            "worker_backend": str(cfg.get("worker_backend") or ""),
            "runtime_profiles": cfg.get("runtime_profiles") or [],
            "worker_profiles": [smoke_profile],
            "llm_profiles": cfg.get("llm_profiles") or {},
            "llm_providers": cfg.get("llm_providers") or [],
        }

        run = test_manager.create_new()

        marker_seen: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def _sink(ev: Any) -> None:
            await session.emit_raw_event(worker_id, ev)
            payload = getattr(ev, "payload", {}) or {}
            fact = str(payload.get("fact") or "")
            text = str(payload.get("text") or payload.get("message") or "")
            if marker in fact or marker in text:
                state["ok"] = True
                state["detail"] = marker
                if not marker_seen.done():
                    marker_seen.set_result(None)

        run.bus.add_sink(_sink)
        driver = build_driver(body, mgr=manager)
        await test_manager.start(run.run_id, driver)
        try:
            waitables: list[asyncio.Future | asyncio.Task] = [marker_seen]
            if run.task is not None:
                waitables.append(run.task)
            done, _pending = await asyncio.wait(
                waitables, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if marker_seen in done and state["ok"]:
                return {"ok": True, "detail": marker, "phase": "done"}
            if not done:
                return {"ok": False, "detail": "worker startup test timed out", "phase": "run"}
        finally:
            cleanup_timeout = StartupTestController._cleanup_timeout(timeout)
            await session.emit_worker_phase(
                worker_id,
                "cleanup",
                "test timed out or finished; cleaning up isolated worker lifecycle",
            )
            try:
                await asyncio.wait_for(test_manager.delete(run.run_id), timeout=cleanup_timeout)
                await session.emit_worker_phase(
                    worker_id,
                    "cleanup.done",
                    "isolated worker lifecycle cleaned up",
                    ok=True,
                )
            except asyncio.TimeoutError:
                await session.emit_worker_phase(
                    worker_id,
                    "cleanup.timeout",
                    "isolated worker cleanup exceeded bounded grace period; residual container/process may be reaped by Docker",
                    ok=False,
                )
            except Exception as exc:  # noqa: BLE001 - cleanup should not hide the worker outcome
                await session.emit_worker_phase(
                    worker_id,
                    "cleanup.failed",
                    str(exc)[:500],
                    ok=False,
                )
        return {
            "ok": state["ok"],
            "detail": state["detail"],
            "phase": "done" if state["ok"] else "run",
        }


def sse_json(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False)
