from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver.poc_verifier import VerifierExecutionResult
from dswarm.swarm.blackboard_bridge import BlackboardBridgeMixin
from dswarm.swarm.poc_verification import VerificationFailure
from dswarm.swarm.poc_verification_runtime import run_poc_verification
from dswarm.swarm.shared_graph import (
    EV_POC_VERIFICATION_FAILED,
    EV_POC_VERIFICATION_STARTED,
    EV_POC_VERIFIED,
    EV_REVIEW_FINDING_VERIFIED,
    SQLiteSharedGraph,
)


class _Bridge(BlackboardBridgeMixin):
    pass


def _challenge() -> Challenge:
    return Challenge(id="pentest-orch", name="pentest", category="web", mode="pentest")


def _graph_with_reproduction(tmp_path: Path) -> tuple[SQLiteSharedGraph, dict]:
    workspace = tmp_path / "workspace"
    artifact = workspace / "shared" / "objects" / "ab" / "cd" / "artifact-1"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("#!/bin/sh\necho RUN_OK\n", encoding="utf-8")
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    graph.save_poc(
        actor="worker",
        poc_id="poc-1",
        path="shared/objects/ab/cd/artifact-1",
        entry_command="sh repro.sh",
        artifact_id="artifact-1",
        name="repro.sh",
    )
    registration = graph.register_poc_reproduction(
        actor="worker", poc_id="poc-1", indicator="RUN_OK"
    )
    return graph, {"workspace_root": workspace, **registration}


@dataclass
class _FakeLease:
    pool_id: str = "pool-1"
    pool_instance_id: str = "pool-instance-1"
    generation: int = 7
    worker_instance_id: str = "verifier-worker-1"
    released: bool = False

    async def release(self) -> None:
        self.released = True


class _LeaseFactory:
    def __init__(self, lease: _FakeLease | BaseException | None = None) -> None:
        self.lease = lease if lease is not None else _FakeLease()
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, worker_instance_id: str, operation_kind: str):
        self.calls.append((worker_instance_id, operation_kind))
        if isinstance(self.lease, BaseException):
            raise self.lease
        return self.lease


class _FakeVerifier:
    def __init__(self, result: VerifierExecutionResult | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def verify(self, registration, lease, *, timeout: float):
        self.calls.append({
            "poc_id": registration.poc_id,
            "reproduction_id": registration.reproduction_id,
            "argv": registration.argv,
            "lease": lease,
            "timeout": timeout,
        })
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _intent(meta: dict, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "intent_id": "intent-verifier-1",
        "worker_id": "verifier-worker-1",
        "poc_id": "poc-1",
        "reproduction_id": meta["reproduction_id"],
        "source_finding_id": "finding-1",
    }
    data.update(overrides)
    return data


def _usage(meta: dict, **overrides: object) -> SimpleNamespace:
    data = {
        "workspace_root": meta["workspace_root"],
        "worker_id": "verifier-worker-1",
        "operation_kind": "review",
        "timeout": 33,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_success_appends_started_before_execution_terminal_before_delta_and_review_link(
    tmp_path: Path,
):
    graph, meta = _graph_with_reproduction(tmp_path)
    emitted: list[tuple[str, dict]] = []

    async def emit_delta(kind: str, **fields):
        terminal = graph.events_since(0, kinds=[EV_POC_VERIFIED])
        assert terminal, "success delta must wait for durable poc_verified"
        emitted.append((kind, fields))

    class InspectingVerifier(_FakeVerifier):
        async def verify(self, registration, lease, *, timeout: float):
            status = graph.poc_verification_status("poc-1")
            assert status is not None
            assert status["status"] == "started"
            assert graph.events_since(0, kinds=[EV_POC_VERIFICATION_STARTED])
            return await super().verify(registration, lease, timeout=timeout)

    verifier = InspectingVerifier(
        VerifierExecutionResult(
            status="verified",
            exit_code=0,
            observed_location="stdout",
            provenance_artifact_ids=("artifact-out",),
            elapsed_ms=12,
            stdout="RUN_OK",
        )
    )

    outcome = await run_poc_verification(
        _intent(meta),
        graph=graph,
        verifier=verifier,
        runtime_lease_factory=_LeaseFactory(),
        usage_context=_usage(meta, emit_delta=emit_delta),
    )

    assert outcome.status == "verified"
    assert outcome.verified is True
    assert len(verifier.calls) == 1
    kinds = [event["kind"] for event in graph.events_since(0)]
    assert kinds.index(EV_POC_VERIFICATION_STARTED) < kinds.index(EV_POC_VERIFIED)
    assert kinds.index(EV_POC_VERIFIED) < kinds.index(EV_REVIEW_FINDING_VERIFIED)
    assert emitted and emitted[-1][0] == "poc_verified"
    assert "RUN_OK" not in repr(emitted)
    status = graph.poc_verification_status("poc-1")
    assert status["status"] == "verified"
    assert status["terminal_seq"] == outcome.terminal_seq
    graph.close()


@pytest.mark.asyncio
async def test_failure_after_execution_appends_closed_reason_without_fact_or_dead_end(
    tmp_path: Path,
):
    graph, meta = _graph_with_reproduction(tmp_path)
    verifier = _FakeVerifier(
        VerifierExecutionResult(
            status=VerificationFailure.INDICATOR_NOT_OBSERVED.value,
            exit_code=0,
            diagnostics="marker not in captured output",
            elapsed_ms=15,
            stdout="no marker",
        )
    )

    outcome = await run_poc_verification(
        _intent(meta, source_finding_id=""),
        graph=graph,
        verifier=verifier,
        runtime_lease_factory=_LeaseFactory(),
        usage_context=_usage(meta),
    )

    assert outcome.status == "indicator_not_observed"
    assert outcome.verified is False
    events = graph.events_since(0)
    assert EV_POC_VERIFICATION_FAILED in [event["kind"] for event in events]
    terminal = graph.events_since(0, kinds=[EV_POC_VERIFICATION_FAILED])[0]
    assert terminal["payload"]["reason"] == "indicator_not_observed"
    assert not graph.events_since(0, kinds=["fact_added", "dead_end"])
    graph.close()


@pytest.mark.asyncio
async def test_duplicate_started_reproduction_returns_lease_unavailable_without_execution(
    tmp_path: Path,
):
    graph, meta = _graph_with_reproduction(tmp_path)
    assert graph.begin_poc_verification(
        actor="other", poc_id="poc-1", verification_id="verification-existing",
        reproduction_id=meta["reproduction_id"], worker_id="other",
    )
    verifier = _FakeVerifier(VerifierExecutionResult(status="verified"))
    lease_factory = _LeaseFactory()

    outcome = await run_poc_verification(
        _intent(meta),
        graph=graph,
        verifier=verifier,
        runtime_lease_factory=lease_factory,
        usage_context=_usage(meta),
    )

    assert outcome.status == "lease_unavailable"
    assert verifier.calls == []
    assert lease_factory.calls == []
    assert len(graph.events_since(0, kinds=[EV_POC_VERIFICATION_STARTED])) == 1
    graph.close()


@pytest.mark.asyncio
async def test_terminal_append_failure_suppresses_success_delta(tmp_path: Path, monkeypatch):
    graph, meta = _graph_with_reproduction(tmp_path)
    emitted: list[tuple[str, dict]] = []

    async def emit_delta(kind: str, **fields):
        emitted.append((kind, fields))

    def boom(**_kwargs):
        raise RuntimeError("sqlite unavailable")

    monkeypatch.setattr(graph, "append_poc_verification_terminal", boom)
    verifier = _FakeVerifier(VerifierExecutionResult(status="verified", exit_code=0))

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        await run_poc_verification(
            _intent(meta),
            graph=graph,
            verifier=verifier,
            runtime_lease_factory=_LeaseFactory(),
            usage_context=_usage(meta, emit_delta=emit_delta),
        )

    assert emitted == []
    assert not graph.events_since(0, kinds=[EV_POC_VERIFIED])
    graph.close()


@pytest.mark.asyncio
async def test_cancellation_records_bounded_failure_when_graph_writable_and_reraises(
    tmp_path: Path,
):
    graph, meta = _graph_with_reproduction(tmp_path)
    verifier = _FakeVerifier(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await run_poc_verification(
            _intent(meta),
            graph=graph,
            verifier=verifier,
            runtime_lease_factory=_LeaseFactory(),
            usage_context=_usage(meta),
        )

    terminal = graph.events_since(0, kinds=[EV_POC_VERIFICATION_FAILED])[0]
    assert terminal["payload"]["reason"] == "cancelled"
    assert terminal["payload"]["diagnostics"] == "cancelled"
    graph.close()


def test_verification_lifecycle_replays_and_public_deltas_are_redacted(tmp_path: Path):
    graph, meta = _graph_with_reproduction(tmp_path)
    started = graph.begin_poc_verification(
        actor="verifier", poc_id="poc-1", verification_id="verification-1",
        reproduction_id=meta["reproduction_id"], worker_id="verifier-worker-1",
        finding_id="finding-1", intent_id="intent-verifier-1", pool_identity="pool-1/gen-7",
    )
    assert started is not None
    graph.append_poc_verification_terminal(
        actor="verifier", poc_id="poc-1", verification_id="verification-1",
        verified=True, exit_code=0, observed_location="stdout",
        provenance_artifact_ids=["artifact-out"], diagnostics="raw RUN_OK should stay bounded",
        elapsed_ms=20,
    )
    graph.mark_review_finding_verified(
        actor="verifier", finding_id="finding-1", poc_id="poc-1",
        reproduction_id=meta["reproduction_id"], verification_id="verification-1",
    )
    expected = graph.poc_verification_status("poc-1")
    events = graph.events_since(0)
    graph.close()

    reopened = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    assert reopened.poc_verification_status("poc-1") == expected
    assert not reopened.verified_evidence()

    bridge = _Bridge()
    deltas = [delta for event in events for delta in bridge._graph_event_to_bb(event)]
    lifecycle_deltas = [
        delta for delta in deltas
        if delta[0] in {
            "poc_verification_started", "poc_verified",
            "poc_verification_failed", "review_finding_verified",
        }
    ]
    kinds = [kind for kind, _fields in lifecycle_deltas]
    assert "poc_verification_started" in kinds
    assert "poc_verified" in kinds
    assert "review_finding_verified" in kinds
    public = repr(lifecycle_deltas)
    assert "RUN_OK" not in public
    assert "sh repro.sh" not in public
    assert str(meta["workspace_root"]) not in public
    reopened.close()
