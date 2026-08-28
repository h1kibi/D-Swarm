from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dswarm.core.events import Event, EventType
from dswarm.core.usage_journal import UsageCall, UsageJournal, UsageRecord
from dswarm.core.usage_ledger import (
    LedgerNotReady,
    SpawnGuard,
    UsageLedger,
)
from dswarm.swarm.budget import BudgetAction, ProfileBudgetGate


def _record(*, usage_id: str = "usage::run-1::internal::call-1", run_id: str = "run-1", profile: str = "p1", account: str | None = "acct-1", usd: float = 1.25, tokens: int = 100) -> UsageRecord:
    call = UsageCall(
        provider_call_id=usage_id.rsplit("::", 1)[-1],
        producer="internal",
        run_id=run_id,
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id="solver-1",
        profile_id=profile,
        configured_account_id=account,
        billing_account_id=account,
    )
    return UsageRecord.from_call(
        call,
        call_outcome="succeeded",
        usage_status="measured",
        input_tokens=tokens,
        output_tokens=0,
        usd=usd,
    )


def test_usage_ledger_rebuilds_five_projections_idempotently() -> None:
    ledger = UsageLedger(run_id="run-1")
    record = _record()
    event = Event(
        event_type=EventType.USAGE_RECORDED,
        run_id="run-1",
        payload=record.__dict__.copy(),
    )

    ledger.rebuild([event])
    assert ledger.state == "ready"
    assert ledger.apply_event(event) is False
    snap = ledger.snapshot()
    assert snap["global"]["usd"] == pytest.approx(1.25)
    assert snap["global"]["tokens"] == 100
    assert snap["challenge"]["challenge-1"]["calls"] == 1
    assert snap["solver"]["solver-1"]["calls"] == 1
    assert snap["profile"]["p1"]["usd"] == pytest.approx(1.25)
    assert snap["account"]["acct-1"]["usd"] == pytest.approx(1.25)


def test_usage_ledger_reconcile_recovers_started_only(tmp_path: Path) -> None:
    journal = UsageJournal(tmp_path / "run-recover-usage-journal.jsonl")
    call = UsageCall(
        provider_call_id="call-recover",
        producer="internal",
        run_id="run-recover",
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id="solver-1",
        profile_id="p1",
        configured_account_id="acct-1",
        billing_account_id="acct-1",
    )
    journal.append_started(call)
    ledger = UsageLedger(run_id="run-recover")
    recovered = ledger.rebuild([], journal=journal)
    assert recovered == 1
    assert ledger.state == "ready"
    record = ledger.records[call.usage_id]
    assert record.call_outcome == "interrupted"
    assert record.usage_status == "unknown"
    assert record.input_tokens is None
    assert ledger.snapshot()["global"]["unknown_calls"] == 1


def test_spawn_guard_blocks_failed_and_waits_for_rebuild() -> None:
    async def scenario() -> None:
        guard = SpawnGuard()
        guard.mark_rebuilding()
        waiter = asyncio.create_task(guard.ensure_ready("run-1", timeout=0.05))
        await asyncio.sleep(0)
        guard.mark_ready()
        await waiter
        guard.mark_failed("canonical_append_failed")
        with pytest.raises(LedgerNotReady, match="canonical_append_failed"):
            await guard.ensure_ready("run-1")

    asyncio.run(scenario())


def test_profile_budget_gate_has_independent_profile_and_account_blockers() -> None:
    gate = ProfileBudgetGate(
        profile_caps={"p1": 100},
        account_caps={"acct-1": 200},
        warn_ratio=0.8,
    )
    record = _record(usd=0.0, tokens=90)
    verdict = gate.apply(record)
    assert verdict.level == "warn"
    assert gate.authorize(profile_id="p1", account_id="acct-1").allowed
    record2 = _record(usage_id="usage::run-1::internal::call-2", usd=0.0, tokens=20)
    verdict2 = gate.apply(record2)
    assert verdict2.level == "cap"
    assert gate.authorize(profile_id="p1", account_id="acct-1").allowed is False
    assert gate.authorize(profile_id="p2", account_id="acct-1").allowed


def test_budget_action_is_durable_semantics() -> None:
    gate = ProfileBudgetGate(profile_caps={"p1": 10})
    gate.apply(_record(usd=0.0, tokens=10))
    assert not gate.authorize(profile_id="p1", account_id=None).allowed
    gate.apply_action(BudgetAction(action="raise_cap", profile_id="p1", value=20))
    assert gate.authorize(profile_id="p1", account_id=None).allowed
    assert gate.snapshot()["actions"][-1]["action"] == "raise_cap"




def test_budget_resume_without_explicit_action_does_not_clear_blocker() -> None:
    gate = ProfileBudgetGate(profile_caps={"p1": 10})
    gate.apply(_record(usd=0.0, tokens=10, profile="p1"))
    assert gate.authorize(profile_id="p1", account_id=None).allowed is False

    # A generic/operator resume is not a budget override. Only the durable
    # raise_cap/override actions are allowed to clear the blocker.
    gate.apply_action(BudgetAction(action="resume", profile_id="p1"))
    assert gate.authorize(profile_id="p1", account_id=None).allowed is False


def test_profile_budget_alerts_are_edge_triggered() -> None:
    gate = ProfileBudgetGate(profile_caps={"p1": 100}, warn_ratio=0.8)
    first = gate.apply(_record(usd=0.0, tokens=80))
    assert first.level == "warn"
    assert len(first.alerts) == 1
    repeated = gate.apply(_record(usage_id="usage::run-1::internal::call-2", usd=0.0, tokens=1))
    assert repeated.level == "warn"
    assert repeated.alerts == ()
    capped = gate.apply(_record(usage_id="usage::run-1::internal::call-3", usd=0.0, tokens=30))
    assert capped.level == "cap"
    assert len(capped.alerts) == 1


@pytest.mark.asyncio
async def test_run_manager_usage_drives_budget_alert_and_action(tmp_path: Path) -> None:
    from apps.web.run_manager import RunManager

    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("budget-runtime")
    run.budget_gate.profile_caps["p1"] = 10
    seen: list[EventType] = []

    async def sink(event: Event) -> None:
        if event.event_type is EventType.BUDGET_ALERT:
            seen.append(event.event_type)

    run.bus.add_sink(sink)
    record = _record(usd=0.0, tokens=10, profile="p1", run_id=run.run_id, usage_id=f"usage::{run.run_id}::internal::call-1")
    await run.bus.emit(Event(
        event_type=EventType.USAGE_RECORDED,
        run_id=run.run_id,
        payload=record.__dict__.copy(),
    ))
    await asyncio.sleep(0)
    assert run.budget_gate.authorize(profile_id="p1", account_id=None).allowed is False
    assert seen == [EventType.BUDGET_ALERT]

    await run.bus.emit(Event(
        event_type=EventType.BUDGET_ACTION,
        run_id=run.run_id,
        payload={"action": "raise_cap", "profile_id": "p1", "value": 20},
    ))
    assert run.budget_gate.authorize(profile_id="p1", account_id=None).allowed


@pytest.mark.asyncio
async def test_run_manager_budget_snapshot_endpoint(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("budget-api")
    run.budget_gate.profile_caps["p1"] = 10
    app = create_app(manager)
    record = _record(usd=0.0, tokens=3, profile="p1", run_id=run.run_id, usage_id=f"usage::{run.run_id}::internal::call-1")
    await run.bus.emit(Event(
        event_type=EventType.USAGE_RECORDED,
        run_id=run.run_id,
        payload=record.__dict__.copy(),
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run.run_id}/budget")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ledger"]["global"]["tokens"] == 3
    assert payload["budget"]["profile"]["p1"]["tokens"] == 3
    assert payload["ledger_state"] == "ready"


@pytest.mark.asyncio
async def test_run_manager_budget_rebuild_endpoint_restores_ready_state(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("budget-rebuild-api")
    run.spawn_guard.mark_failed("canonical_append_failed")
    run.ledger.mark_failed("canonical_append_failed")
    app = create_app(manager)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/budget/rebuild")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ledger_state"] == "ready"
    assert payload["ledger_error"] is None
    assert run.spawn_guard.ledger_state == "ready"



@pytest.mark.asyncio
async def test_budget_rebuild_endpoint_returns_404_for_unknown_run(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    app = create_app(RunManager(sessions_root=str(tmp_path / "sessions")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/runs/missing/budget/rebuild")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_budget_rebuild_endpoint_returns_503_when_rebuild_fails(tmp_path: Path) -> None:
    from httpx import ASGITransport, AsyncClient
    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("budget-rebuild-fail-api")

    async def fail_rebuild(run_id: str):
        raise RuntimeError("canonical_append_failed")

    manager.rebuild_ledger = fail_rebuild  # type: ignore[method-assign]
    app = create_app(manager)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/budget/rebuild")
    assert response.status_code == 503
    assert "canonical_append_failed" in response.json()["detail"]



@pytest.mark.asyncio
async def test_run_manager_reconciles_journal_into_canonical_events_before_start(tmp_path: Path) -> None:
    from apps.web.run_manager import RunManager

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    journal = UsageJournal(sessions / "reconcile-run-usage-journal.jsonl")
    call = UsageCall(
        provider_call_id="call-reconcile",
        producer="internal",
        run_id="reconcile-run",
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id="solver-1",
        profile_id="p1",
        configured_account_id="acct-1",
        billing_account_id="acct-1",
    )
    journal.append_started(call)
    manager = RunManager(sessions_root=str(sessions))
    run = manager.create("reconcile-run")
    assert run.spawn_guard is not None
    assert run.spawn_guard.ledger_state == "rebuilding"
    observed: list[EventType] = []

    async def driver(handle) -> None:
        observed.append(EventType.USAGE_RECORDED)

    await manager.start("reconcile-run", driver)
    await run.task
    assert observed == [EventType.USAGE_RECORDED]
    assert run.spawn_guard.ledger_state == "ready"
    assert any(event.event_type is EventType.USAGE_RECORDED
               for event in run.store.read_events("reconcile-run"))


def test_swarm_make_cli_worker_budget_rejection_does_not_reserve_spawn(tmp_path: Path) -> None:
    from dswarm.models.solve_graph import Challenge
    from dswarm.core.llm import ModelSpec
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.solver.result import ArtifactStore
    from dswarm.swarm.errors import WorkerSpawnRejected
    from dswarm.swarm.swarm import Swarm

    challenge = Challenge(id="budget-spawn", name="budget-spawn", category="web")
    gate = ProfileBudgetGate(profile_caps={"p1": 10})
    profile = {
        "id": "p1", "engine": "pi", "roles": ["bootstrap"],
        "runtime": "local", "enabled": True, "max_running": 2,
    }
    sw = Swarm(
        challenge,
        llm=None, sandbox=SandboxManager(root=tmp_path / "sandbox"),
        artifacts=ArtifactStore(root=tmp_path / "artifacts"), executor="cli",
        engines=["p1"], worker_profiles=[profile], budget_gate=gate,
        graph_dir=tmp_path / "graph",
    )
    gate.apply(_record(usage_id="usage::budget-spawn::internal::call-1",
                       run_id="budget-spawn", profile="p1", account=None,
                       tokens=10, usd=0.0))

    with pytest.raises(WorkerSpawnRejected, match="budget blocked.*p1"):
        sw._make_cli_worker("p1", mode="bootstrap")
    assert sw._spawned_total == 0


def test_swarm_make_cli_worker_billing_account_block_is_independent(tmp_path: Path) -> None:
    from dswarm.models.solve_graph import Challenge
    from dswarm.core.llm import ModelSpec
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.solver.result import ArtifactStore
    from dswarm.swarm.errors import WorkerSpawnRejected
    from dswarm.swarm.swarm import Swarm

    challenge = Challenge(id="account-spawn", name="account-spawn", category="web")
    gate = ProfileBudgetGate(profile_caps={"p1": 100}, account_caps={"billing-1": 10})
    profile = {
        "id": "p1", "engine": "pi", "roles": ["bootstrap"],
        "runtime": "local", "enabled": True, "max_running": 2,
        "billing_account_id": "billing-1",
    }
    sw = Swarm(
        challenge,
        llm=None, sandbox=SandboxManager(root=tmp_path / "sandbox"),
        artifacts=ArtifactStore(root=tmp_path / "artifacts"), executor="cli",
        engines=["p1"], worker_profiles=[profile], budget_gate=gate,
        graph_dir=tmp_path / "graph",
    )
    sw._profile_for_engine = lambda engine, role=None: profile  # type: ignore[method-assign]
    gate.apply(_record(usage_id="usage::account-spawn::internal::call-1",
                       run_id="account-spawn", profile="p1", account="billing-1",
                       tokens=10, usd=0.0))

    with pytest.raises(WorkerSpawnRejected, match="budget blocked.*p1"):
        sw._make_cli_worker("p1", mode="bootstrap")
    assert sw._spawned_total == 0
    assert gate.authorize(profile_id="p1", account_id="other-billing").allowed


@pytest.mark.asyncio
async def test_budget_gate_replays_duplicate_usage_and_actions_without_double_charge(tmp_path: Path) -> None:
    from apps.web.run_manager import RunManager

    manager = RunManager(sessions_root=str(tmp_path / "sessions"))
    run = manager.create("budget-replay")
    run.budget_gate.profile_caps["p1"] = 10
    record = _record(run_id=run.run_id, profile="p1", account=None,
                     usage_id=f"usage::{run.run_id}::internal::call-1",
                     tokens=10, usd=0.0)
    usage = Event(event_type=EventType.USAGE_RECORDED, run_id=run.run_id,
                  payload=record.__dict__.copy())
    action = Event(event_type=EventType.BUDGET_ACTION, run_id=run.run_id,
                   payload={"action": "raise_cap", "profile_id": "p1", "value": 20})
    await run.bus.emit(usage)
    await run.bus.emit(action)

    manager2 = RunManager(sessions_root=str(tmp_path / "sessions"))
    run2 = manager2.create(run.run_id)
    snapshot = run2.budget_gate.snapshot()
    assert snapshot["profile"]["p1"]["tokens"] == 10
    assert snapshot["profile"]["p1"]["calls"] == 1
    assert len(snapshot["actions"]) == 1
    assert run.budget_gate.authorize(profile_id="p1", account_id=None).allowed


@pytest.mark.asyncio
async def test_ledger_reconcile_failure_blocks_spawn_but_allows_stop_finalize(tmp_path: Path) -> None:
    from apps.web.run_manager import RunManager
    from dswarm.core.usage_journal import UsageCall
    from dswarm.core.usage_ledger import LedgerNotReady

    sessions = tmp_path / "sessions"
    manager = RunManager(sessions_root=str(sessions))
    run = manager.create("reconcile-fail")
    call = UsageCall(
        provider_call_id="call-fail", producer="internal", run_id=run.run_id,
        challenge_id="challenge-1", worker_instance_id="worker-1",
        solver_id="solver-1", profile_id="p1", configured_account_id=None,
        billing_account_id=None,
    )
    run.usage_journal.append_started(call)
    run.ledger.rebuild(run.store.read_events(run.run_id), journal=run.usage_journal)
    run.spawn_guard.mark_rebuilding()

    async def fail_emit_checked(event: Event) -> None:
        raise OSError("canonical_append_failed")

    run.bus.emit_checked = fail_emit_checked  # type: ignore[method-assign]
    with pytest.raises(OSError, match="canonical_append_failed"):
        await manager._reconcile_ledger(run)
    assert run.spawn_guard.ledger_state == "failed"
    with pytest.raises(LedgerNotReady, match="canonical_append_failed"):
        run.spawn_guard.check_now(operation="spawn")
    run.spawn_guard.check_now(operation="stop")
    run.spawn_guard.check_now(operation="finalize")



def test_profile_budget_gate_rebuilds_projection_without_private_state_access() -> None:
    gate = ProfileBudgetGate(profile_caps={"p1": 10})
    record = _record(usd=0.0, tokens=10, profile="p1", account=None)
    gate.rebuild([record], [{"action": "raise_cap", "profile_id": "p1", "value": 20}])
    snapshot = gate.snapshot()
    assert snapshot["profile"]["p1"]["tokens"] == 10
    assert snapshot["profile"]["p1"]["cap_tokens"] == 20
    assert gate.authorize(profile_id="p1", account_id=None).allowed
