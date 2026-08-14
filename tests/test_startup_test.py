"""Worker startup-test controller and API."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from apps.web.startup_test import StartupTestController, StartupTestSession


def _enabled_worker_config(mgr: RunManager) -> None:
    mgr.worker_config.set(
        engines=["pi-a", "pi-b"],
        worker_profiles=[
            {
                "id": "pi-a",
                "name": "pi-a",
                "engine": "pi",
                "transport": "pi_cli",
                "runtime": "local",
                "credential_account": "",
                "enabled": True,
            },
            {
                "id": "pi-b",
                "name": "pi-b",
                "engine": "pi",
                "transport": "pi_cli",
                "runtime": "local",
                "credential_account": "",
                "enabled": True,
            },
            {
                "id": "pi-off",
                "name": "pi-off",
                "engine": "pi",
                "transport": "pi_cli",
                "runtime": "local",
                "credential_account": "",
                "enabled": False,
            },
        ],
    )


def test_startup_test_normalizes_profile_health_preflight_diagnostics(tmp_path, monkeypatch):
    from apps.web import startup_test as startup_mod
    from dswarm.solver.profile_health import ProfileHealth

    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    def fake_backend_for_profile(profile, *, runtime_profiles, worker_backend, in_web_container):
        return "container"

    def fake_is_web_container():
        return False

    def fake_evaluate_profile_health(profile, *, backend, sessions_root, depth, llm_providers):
        return ProfileHealth(
            profile_id="pi-a",
            engine="pi",
            backend=backend,
            status="blocked",
            layer="binding",
            blocker="账号 pi-main 未登记",
            detail="账号 pi-main 未登记",
            model="deepseek-chat",
            account_id="pi-main",
            binding_kind="missing",
            effective_credential_id="pi-main",
        )

    monkeypatch.setattr("apps.web.worker_config.backend_for_profile", fake_backend_for_profile)
    monkeypatch.setattr("dswarm.core.runtime_env.is_web_container", fake_is_web_container)
    monkeypatch.setattr("dswarm.solver.profile_health.evaluate_profile_health", fake_evaluate_profile_health)

    controller = StartupTestController(mgr)
    result = asyncio.run(controller._preflight(mgr.worker_config.get()["worker_profiles"][0]))

    assert result == {
        "ok": False,
        "detail": "账号 pi-main 未登记",
        "status": "blocked",
        "layer": "binding",
        "blocker": "账号 pi-main 未登记",
        "backend": "container",
        "model": "deepseek-chat",
        "account_id": "pi-main",
        "binding_kind": "missing",
        "effective_credential_id": "pi-main",
    }


@pytest.mark.asyncio
async def test_startup_test_runs_all_enabled_workers_and_summarizes(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    calls = []

    async def fake_run_worker_test(profile, *, timeout, session):
        calls.append(profile["id"])
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start()
    await session.task

    assert calls == ["pi-a", "pi-b"]
    assert session.summary["ok"] is True
    assert [row["worker_id"] for row in session.summary["results"]] == ["pi-a", "pi-b"]
    assert all(row["ok"] for row in session.summary["results"])


@pytest.mark.asyncio
async def test_startup_test_continues_after_worker_failure(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        if profile["id"] == "pi-a":
            return {"ok": False, "detail": "auth failed", "phase": "run"}
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start()
    await session.task

    by_id = {row["worker_id"]: row for row in session.summary["results"]}
    assert by_id["pi-a"]["ok"] is False
    assert by_id["pi-b"]["ok"] is True
    assert session.summary["passed"] == 1
    assert session.summary["failed"] == 1


@pytest.mark.asyncio
async def test_startup_test_emits_provider_errors_for_preflight_failures(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        raise AssertionError("unhealthy preflight workers must be skipped")

    async def fake_preflight(profile):
        return {
            "ok": False,
            "detail": "403 authentication failed: invalid api key",
            "status": "auth_failed",
            "layer": "auth",
            "backend": "container",
            "model": "deepseek-chat",
            "account_id": "pi-main",
            "effective_credential_id": "pi-main",
        }

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start()
    await session.task

    events = []
    while not session.events.empty():
        ev = await session.events.get()
        if ev:
            events.append(ev)

    provider_errors = [ev for ev in events if ev.get("type") == "provider.error"]
    batch_alerts = [ev for ev in events if ev.get("type") == "provider.batch_alert"]
    assert len(provider_errors) == 2
    assert {ev["worker_id"] for ev in provider_errors} == {"pi-a", "pi-b"}
    assert all(ev["category"] == "auth_invalid" for ev in provider_errors)
    assert all(ev["should_pause_dispatch"] is True for ev in provider_errors)
    assert batch_alerts
    assert batch_alerts[-1]["category"] == "auth_invalid"
    assert batch_alerts[-1]["should_pause_dispatch"] is True


@pytest.mark.asyncio
async def test_startup_test_surfaces_preflight_diagnostics_and_skips_unhealthy_worker(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    calls = []

    async def fake_run_worker_test(profile, *, timeout, session):
        calls.append(profile["id"])
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        if profile["id"] == "pi-a":
            return {
                "ok": False,
                "detail": "worker image missing: dswarm-worker-pi",
                "status": "auth_failed",
                "layer": "image",
                "blocker": "docker image is unavailable",
                "backend": "container",
                "model": "deepseek-chat",
                "account_id": "pi-main",
                "binding_kind": "explicit",
                "effective_credential_id": "pi-main",
            }
        return {"ok": True, "detail": "ok", "status": "ok", "backend": "local"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start()
    await session.task

    assert calls == ["pi-b"]
    by_id = {row["worker_id"]: row for row in session.summary["results"]}
    assert by_id["pi-a"] == {
        "worker_id": "pi-a",
        "ok": False,
        "phase": "preflight",
        "detail": "worker image missing: dswarm-worker-pi",
        "status": "auth_failed",
        "layer": "image",
        "blocker": "docker image is unavailable",
        "backend": "container",
        "model": "deepseek-chat",
        "account_id": "pi-main",
        "binding_kind": "explicit",
        "effective_credential_id": "pi-main",
    }

    failed_events = []
    while not session.events.empty():
        ev = await session.events.get()
        if ev and ev.get("type") == "worker.phase" and ev.get("worker_id") == "pi-a" and ev.get("phase") == "failed":
            failed_events.append(ev)
    assert failed_events
    assert failed_events[-1]["layer"] == "image"
    assert failed_events[-1]["backend"] == "container"
    assert failed_events[-1]["blocker"] == "docker image is unavailable"




@pytest.mark.asyncio
async def test_startup_test_emits_provider_errors_and_batch_alert(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    mgr.worker_config.set(
        engines=["pi-a", "pi-b", "pi-c"],
        worker_profiles=[
            {"id": "pi-a", "name": "pi-a", "engine": "pi", "enabled": True},
            {"id": "pi-b", "name": "pi-b", "engine": "pi", "enabled": True},
            {"id": "pi-c", "name": "pi-c", "engine": "pi", "enabled": True},
        ],
    )

    async def fake_run_worker_test(profile, *, timeout, session):
        return {
            "ok": False,
            "detail": "402 insufficient balance: please recharge",
            "phase": "run",
            "provider": "deepseek",
            "account_id": "main",
        }

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start()
    await session.task

    events = []
    while not session.events.empty():
        ev = await session.events.get()
        if ev:
            events.append(ev)

    provider_errors = [ev for ev in events if ev.get("type") == "provider.error"]
    assert len(provider_errors) == 3
    assert provider_errors[0]["category"] == "insufficient_quota"
    assert provider_errors[0]["should_pause_dispatch"] is True
    assert "余额" in provider_errors[0]["user_message"]

    alerts = [ev for ev in events if ev.get("type") == "provider.batch_alert"]
    assert alerts
    assert alerts[-1]["category"] == "insufficient_quota"
    assert alerts[-1]["count"] == 3
    assert alerts[-1]["affected_workers"] == 3
    assert alerts[-1]["active_workers"] == 3
    assert alerts[-1]["should_pause_dispatch"] is True




@pytest.mark.asyncio
async def test_startup_test_full_flow_mode_emits_mode_and_required_checks(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start(mode="full_flow", benchmark="local-smoke")
    await session.task

    events = []
    while not session.events.empty():
        ev = await session.events.get()
        if ev:
            events.append(ev)

    started = next(ev for ev in events if ev.get("type") == "test.started")
    assert started["mode"] == "full_flow"
    assert started["benchmark"] == "local-smoke"

    checks = [ev for ev in events if ev.get("type") == "flow.check"]
    assert {ev["check_id"] for ev in checks} >= {
        "benchmark.loaded",
        "blackboard.checked",
        "reason.checked",
        "hint.checked",
        "btw.checked",
        "stop.checked",
        "resume.checked",
        "recovery.checked",
    }
    assert session.summary["mode"] == "full_flow"
    assert session.summary["benchmark"] == "local-smoke"
    assert {c["id"] for c in session.summary["checks"]} >= {ev["check_id"] for ev in checks}


@pytest.mark.asyncio
async def test_startup_test_api_accepts_full_flow_mode(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    monkeypatch.setattr(
        "apps.web.startup_test.StartupTestController.default_run_worker_test",
        staticmethod(fake_run_worker_test),
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        start = await client.post(
            "/api/startup-test",
            json={"mode": "full_flow", "benchmark": "local-smoke"},
        )
        assert start.status_code == 200
        test_id = start.json()["test_id"]

        frames = []
        async with client.stream("GET", f"/api/startup-test/{test_id}/events") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(line.removeprefix("data: "))
                if "test.done" in line:
                    break

    joined = "\n".join(frames)
    assert '"mode": "full_flow"' in joined
    assert '"type": "flow.check"' in joined




@pytest.mark.asyncio
async def test_full_flow_runner_summarizes_required_checks(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    async def fake_full_flow(session):
        checks = []
        checks.append(await session.emit_flow_check("blackboard.checked", ok=True, detail="fact/directive persisted"))
        checks.append(await session.emit_flow_check("reason.checked", ok=True, detail="intent planned and claimed"))
        checks.append(await session.emit_flow_check("hint.checked", ok=True, detail="directive bound to next worker"))
        checks.append(await session.emit_flow_check("btw.checked", ok=True, detail="progress summary available"))
        checks.append(await session.emit_flow_check("stop.checked", ok=True, detail="workers settled"))
        checks.append(await session.emit_flow_check("resume.checked", ok=True, detail="continued from graph"))
        checks.append(await session.emit_flow_check("recovery.checked", ok=True, detail="crash/restart continuation covered"))
        return {"checks": checks}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(
        mgr,
        run_worker_test=fake_run_worker_test,
        run_full_flow_test=fake_full_flow,
    )
    session = await controller.start(mode="full_flow")
    await session.task

    assert session.summary["ok"] is True
    checks = {c["id"]: c for c in session.summary["checks"]}
    assert checks["blackboard.checked"]["detail"] == "fact/directive persisted"
    assert checks["hint.checked"]["detail"] == "directive bound to next worker"
    assert checks["recovery.checked"]["ok"] is True


@pytest.mark.asyncio
async def test_full_flow_summary_fails_when_required_check_fails(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    async def fake_full_flow(session):
        return {"checks": [
            await session.emit_flow_check("resume.checked", ok=False, detail="dispatch body missing"),
        ]}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(
        mgr,
        run_worker_test=fake_run_worker_test,
        run_full_flow_test=fake_full_flow,
    )
    session = await controller.start(mode="full_flow")
    await session.task

    assert session.summary["ok"] is False
    assert session.summary["checks"] == [
        {"id": "resume.checked", "ok": False, "detail": "dispatch body missing"}
    ]


@pytest.mark.asyncio
async def test_default_full_flow_runner_exercises_blackboard_hint_btw_and_recovery(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start(mode="full_flow", benchmark="local-smoke")
    await session.task

    checks = {c["id"]: c for c in session.summary["checks"]}
    assert checks["blackboard.checked"]["ok"] is True
    assert "evidence_seq=" in checks["blackboard.checked"]["detail"]
    assert "directive_id=D-" in checks["blackboard.checked"]["detail"]

    assert checks["hint.checked"]["ok"] is True
    assert "queued immediately" in checks["hint.checked"]["detail"]
    assert "bound_worker=pi-a" in checks["hint.checked"]["detail"]

    assert checks["reason.checked"]["ok"] is True
    assert "intent_id=I-" in checks["reason.checked"]["detail"]
    assert "claimed=True" in checks["reason.checked"]["detail"]

    assert checks["btw.checked"]["ok"] is True
    assert "facts=" in checks["btw.checked"]["detail"]
    assert "intents=" in checks["btw.checked"]["detail"]

    assert checks["resume.checked"]["ok"] is True
    assert "dispatch body reload ok" in checks["resume.checked"]["detail"]
    assert checks["recovery.checked"]["ok"] is True
    assert "graph reopen ok" in checks["recovery.checked"]["detail"]


@pytest.mark.asyncio
async def test_full_flow_reports_worker_coverage_and_all_recovery_modes(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    controller = StartupTestController(mgr, run_worker_test=fake_run_worker_test)
    session = await controller.start(mode="full_flow", benchmark="local-smoke")
    await session.task

    checks = {c["id"]: c for c in session.summary["checks"]}
    assert checks["workers.checked"]["ok"] is True
    assert "passed_workers=2/2" in checks["workers.checked"]["detail"]

    for check_id in (
        "recovery.user_stop_resume.checked",
        "recovery.worker_crash.checked",
        "recovery.backend_restart.checked",
        "recovery.desktop_restart.checked",
    ):
        assert checks[check_id]["ok"] is True

    assert "stop artifact" in checks["recovery.user_stop_resume.checked"]["detail"]
    assert "crash marker" in checks["recovery.worker_crash.checked"]["detail"]
    assert "new manager" in checks["recovery.backend_restart.checked"]["detail"]
    assert "desktop relaunch" in checks["recovery.desktop_restart.checked"]["detail"]



def test_startup_test_default_worker_runner_is_static_callable_to_avoid_timeout_collision(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    controller = StartupTestController(mgr)

    assert getattr(controller.run_worker_test, "__self__", None) is None


@pytest.mark.asyncio
async def test_startup_test_default_worker_runner_does_not_pass_legacy_stage_policy(tmp_path, monkeypatch):
    from dswarm.core.events import Event, EventType, blackboard_delta_payload

    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    controller = StartupTestController(mgr, timeout_per_worker=5.0)
    session = StartupTestSession("startup-test-unit", controller)

    captured: dict = {}

    def fake_build_driver(body, mgr=None):
        captured.update(body)

        async def driver(run):
            await run.bus.emit(Event(
                event_type=EventType.SHARED_GRAPH_DELTA,
                run_id=run.run_id,
                payload=blackboard_delta_payload(
                    "evidence",
                    actor="test",
                    fact="VERIFIED_FACT=startup_test_ok",
                ),
            ))

        return driver

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)

    result = await StartupTestController.default_run_worker_test(
        mgr.worker_config.get()["worker_profiles"][0],
        timeout=5.0,
        session=session,
    )

    assert result["ok"] is True
    assert "stage_policy" not in captured
    assert captured["wall_clock_budget"] == 5.0
    assert captured["max_total_workers"] == 1


@pytest.mark.asyncio
async def test_startup_test_default_worker_runner_returns_as_soon_as_marker_is_seen(tmp_path, monkeypatch):
    from dswarm.core.events import Event, EventType, shared_graph_delta_payload

    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    controller = StartupTestController(mgr, timeout_per_worker=5.0)
    session = StartupTestSession("startup-test-marker-unit", controller)
    worker_run_ids: list[str] = []

    def fake_build_driver(body, mgr=None):
        async def driver(run):
            worker_run_ids.append(run.run_id)
            await run.bus.emit(Event(
                event_type=EventType.SHARED_GRAPH_DELTA,
                run_id=run.run_id,
                payload=shared_graph_delta_payload(
                    "startup_test_ok",
                    verified=True,
                    confidence=1.0,
                    actor="test",
                ),
            ))
            await asyncio.Event().wait()

        return driver

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)

    result = await asyncio.wait_for(
        StartupTestController.default_run_worker_test(
            mgr.worker_config.get()["worker_profiles"][0],
            timeout=5.0,
            session=session,
        ),
        timeout=1.0,
    )

    assert result == {"ok": True, "detail": "startup_test_ok", "phase": "done"}
    assert worker_run_ids
    assert controller._test_manager.get(worker_run_ids[0]) is None



def test_startup_test_worker_cleanup_timeout_allows_real_container_teardown():
    assert StartupTestController._cleanup_timeout(12.0) >= 20.0
    assert StartupTestController._cleanup_timeout(90.0) >= 20.0
    assert StartupTestController._cleanup_timeout(180.0) <= 30.0


@pytest.mark.asyncio
async def test_startup_test_default_worker_runner_waits_for_cleanup_grace_and_reports_phase(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    controller = StartupTestController(mgr, timeout_per_worker=0.01)
    session = StartupTestSession("startup-test-cleanup-grace-unit", controller)
    delete_completed = asyncio.Event()

    def fake_build_driver(body, mgr=None):
        async def driver(run):
            await asyncio.Event().wait()

        return driver

    async def slow_but_finite_delete(run_id):
        run = controller._test_manager.get(run_id)
        if run is not None and run.task is not None:
            run.task.cancel()
        await asyncio.sleep(0.08)
        controller._test_manager.runs.pop(run_id, None)
        delete_completed.set()
        return True

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(controller._test_manager, "delete", slow_but_finite_delete)
    monkeypatch.setattr(StartupTestController, "_cleanup_timeout", staticmethod(lambda timeout: 0.2), raising=False)

    result = await asyncio.wait_for(
        StartupTestController.default_run_worker_test(
            mgr.worker_config.get()["worker_profiles"][0],
            timeout=0.01,
            session=session,
        ),
        timeout=0.5,
    )

    phases = []
    while not session.events.empty():
        item = await session.events.get()
        if item is not None and item.get("type") == "worker.phase":
            phases.append(item.get("phase"))

    assert delete_completed.is_set()
    assert "cleanup" in phases
    assert "cleanup.done" in phases
    assert result == {"ok": False, "detail": "worker startup test timed out", "phase": "run"}


@pytest.mark.asyncio
async def test_startup_test_default_worker_runner_bounds_cleanup_after_timeout(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    controller = StartupTestController(mgr, timeout_per_worker=0.01)
    session = StartupTestSession("startup-test-cleanup-timeout-unit", controller)
    delete_called = asyncio.Event()

    def fake_build_driver(body, mgr=None):
        async def driver(run):
            await asyncio.Event().wait()

        return driver

    async def hanging_delete(run_id):
        delete_called.set()
        run = controller._test_manager.get(run_id)
        if run is not None and run.task is not None:
            run.task.cancel()
        await asyncio.Event().wait()

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(controller._test_manager, "delete", hanging_delete)

    result = await asyncio.wait_for(
        StartupTestController.default_run_worker_test(
            mgr.worker_config.get()["worker_profiles"][0],
            timeout=0.01,
            session=session,
        ),
        timeout=0.2,
    )

    assert delete_called.is_set()
    assert result == {"ok": False, "detail": "worker startup test timed out", "phase": "run"}

@pytest.mark.asyncio
async def test_startup_test_default_worker_runner_routes_smoke_category_to_profile(tmp_path, monkeypatch):
    from dswarm.core.events import Event, EventType, shared_graph_delta_payload
    from dswarm.solver.worker_profiles import direction_profile_name

    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)
    controller = StartupTestController(mgr, timeout_per_worker=5.0)
    captured_bodies: list[dict] = []

    def fake_build_driver(body, mgr=None):
        captured_bodies.append(body)

        async def driver(run):
            await run.bus.emit(Event(
                event_type=EventType.SHARED_GRAPH_DELTA,
                run_id=run.run_id,
                payload=shared_graph_delta_payload(
                    "startup_test_ok",
                    verified=True,
                    confidence=1.0,
                    actor="test",
                ),
            ))

        return driver

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)

    generic_profile = {
        "id": "seat_pi_generic",
        "name": "seat_pi_generic",
        "label": "pi-worker",
        "engine": "pi",
        "transport": "pi",
        "image": "ctf-swarm-pi:0.2.0",
        "enabled": True,
    }
    web_profile = {
        "id": "seat_pi_web",
        "name": "seat_pi_web",
        "label": "pi-web",
        "engine": "pi",
        "transport": "pi",
        "image": "ctf-swarm-pi-web:0.2.0",
        "enabled": True,
    }

    assert (await StartupTestController.default_run_worker_test(
        generic_profile,
        timeout=5.0,
        session=StartupTestSession("startup-test-generic-route", controller),
    ))["ok"] is True
    assert (await StartupTestController.default_run_worker_test(
        web_profile,
        timeout=5.0,
        session=StartupTestSession("startup-test-web-route", controller),
    ))["ok"] is True

    generic_category = captured_bodies[0]["challenge"]["category"]
    web_category = captured_bodies[1]["challenge"]["category"]

    assert generic_category == "misc"
    assert direction_profile_name(generic_category) == "pi-misc"
    assert direction_profile_name(web_category) == "pi-web"
    assert captured_bodies[0]["worker_profiles"][0]["name"] == "seat_pi_generic"
    assert captured_bodies[0]["worker_profiles"][0]["label"] == "pi-misc"
    assert captured_bodies[1]["worker_profiles"][0]["name"] == "seat_pi_web"
    assert captured_bodies[1]["worker_profiles"][0]["label"] == "pi-web"
    assert captured_bodies[0]["engines"] == ["seat_pi_generic"]
    assert captured_bodies[1]["engines"] == ["seat_pi_web"]

@pytest.mark.asyncio
async def test_startup_test_api_returns_id_and_sse_frames(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        await session.emit_worker_phase(profile["id"], "running", "started")
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    monkeypatch.setattr(
        "apps.web.startup_test.StartupTestController.default_run_worker_test",
        staticmethod(fake_run_worker_test),
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        start = await client.post("/api/startup-test")
        assert start.status_code == 200
        test_id = start.json()["test_id"]

        frames = []
        async with client.stream("GET", f"/api/startup-test/{test_id}/events") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    frames.append(line.removeprefix("data: "))
                if "test.done" in line:
                    break

    joined = "\n".join(frames)
    assert "test.started" in joined
    assert "worker.phase" in joined
    assert "test.done" in joined


@pytest.mark.asyncio
async def test_startup_test_events_replay_for_reopened_panel_after_first_stream(tmp_path, monkeypatch):
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        await session.emit_worker_phase(profile["id"], "running", "started")
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    monkeypatch.setattr(
        "apps.web.startup_test.StartupTestController.default_run_worker_test",
        staticmethod(fake_run_worker_test),
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        start = await client.post("/api/startup-test")
        assert start.status_code == 200
        test_id = start.json()["test_id"]

        async def collect_until_done() -> list[str]:
            frames: list[str] = []
            async with client.stream("GET", f"/api/startup-test/{test_id}/events") as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        frames.append(line.removeprefix("data: "))
                    if "test.done" in line:
                        break
            return frames

        first_frames = await asyncio.wait_for(collect_until_done(), timeout=3.0)

        # Reopening the modal or reconnecting SSE must not leave the UI stuck with
        # an empty stream; it should replay the completed test history.
        second_frames = await asyncio.wait_for(collect_until_done(), timeout=3.0)

    assert any("test.started" in frame for frame in first_frames)
    assert any("test.done" in frame for frame in first_frames)
    assert any("test.started" in frame for frame in second_frames)
    assert any("test.done" in frame for frame in second_frames)


@pytest.mark.asyncio
async def test_startup_test_events_require_valid_ticket_when_auth_enabled(tmp_path, monkeypatch):
    from apps.web import auth as A

    monkeypatch.setenv(A.PASSWORD_ENV, "letmein")
    monkeypatch.delenv(A.BIND_ENV, raising=False)
    mgr = RunManager(sessions_root=tmp_path)
    _enabled_worker_config(mgr)

    async def fake_run_worker_test(profile, *, timeout, session):
        return {"ok": True, "detail": "startup_test_ok", "phase": "done"}

    async def fake_preflight(profile):
        return {"ok": True, "detail": "ok"}

    monkeypatch.setattr(StartupTestController, "_preflight", staticmethod(fake_preflight))
    monkeypatch.setattr(
        "apps.web.startup_test.StartupTestController.default_run_worker_test",
        staticmethod(fake_run_worker_test),
    )

    app = create_app(mgr)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        timeout=10,
        trust_env=False,
    ) as client:
        login = await client.post("/api/auth/login", json={"password": "letmein"})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        start = await client.post("/api/startup-test", headers=headers)
        test_id = start.json()["test_id"]

        without_ticket = await client.get(f"/api/startup-test/{test_id}/events")
        assert without_ticket.status_code == 401

        bad_ticket = await client.get(f"/api/startup-test/{test_id}/events?ticket=bogus")
        assert bad_ticket.status_code == 401

        ticket = (await client.post("/api/auth/ticket", headers=headers)).json()["ticket"]
        async with client.stream("GET", f"/api/startup-test/{test_id}/events?ticket={ticket}") as resp:
            assert resp.status_code == 200

        reused_ticket = await client.get(f"/api/startup-test/{test_id}/events?ticket={ticket}")
        assert reused_ticket.status_code == 401
