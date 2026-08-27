from __future__ import annotations

import json

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.poc_verification import (
    VerificationFailure,
    normalize_reproduction_indicator,
    reproduction_id_for,
)
from dswarm.swarm.shared_graph import (
    EV_POC_REPRODUCTION_REGISTERED,
    EV_POC_REPRODUCTION_REJECTED,
    EV_POC_VERIFICATION_FAILED,
    EV_POC_VERIFICATION_STARTED,
    EV_POC_VERIFIED,
    SQLiteSharedGraph,
)


def _challenge() -> Challenge:
    return Challenge(id="pentest-1", name="pentest", category="web", mode="pentest")


def _graph(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    graph.save_poc(
        actor="worker-1",
        poc_id="poc-1",
        path="/workspace/poc.py",
        entry_command="python3 /workspace/poc.py",
        artifact_id="sha256:poc-body",
        intent_id=None,
        name="poc.py",
    )
    return graph


def test_normalize_reproduction_indicator_rejects_unsafe_values():
    assert normalize_reproduction_indicator("  vulnerable  ") == "vulnerable"

    for value in ("", "\nboom", "\x00boom", "flag{secret}", "[REDACTED]"):
        with pytest.raises(ValueError):
            normalize_reproduction_indicator(value)

    with pytest.raises(ValueError):
        normalize_reproduction_indicator("x" * 513)
    with pytest.raises(ValueError):
        normalize_reproduction_indicator("x" * 700)


def test_reproduction_identity_is_stable_and_command_bound():
    first = reproduction_id_for(
        artifact_id="sha256:poc-body",
        command="python3 /workspace/poc.py",
        indicator="vulnerable",
    )
    assert first == reproduction_id_for(
        artifact_id="sha256:poc-body",
        command="python3 /workspace/poc.py",
        indicator="vulnerable",
    )
    assert first.startswith("poc-repro::sha256:poc-body::")
    assert first != reproduction_id_for(
        artifact_id="sha256:poc-body",
        command="python3 /workspace/other.py",
        indicator="vulnerable",
    )


def test_register_reproduction_materializes_and_replays(tmp_path):
    graph = _graph(tmp_path)
    registration = graph.register_poc_reproduction(
        actor="worker-1", poc_id="poc-1", indicator="vulnerable"
    )

    assert registration["poc_id"] == "poc-1"
    assert registration["indicator"] == "vulnerable"
    assert registration["command"] == "python3 /workspace/poc.py"
    assert registration["artifact_id"] == "sha256:poc-body"
    assert registration["reproduction_id"] == reproduction_id_for(
        artifact_id="sha256:poc-body",
        command="python3 /workspace/poc.py",
        indicator="vulnerable",
    )
    materialized = graph.get_poc_reproduction("poc-1")
    assert {key: materialized[key] for key in registration} == registration
    assert materialized["path"] == "/workspace/poc.py"
    assert materialized["name"] == "poc.py"
    assert materialized["entry_command"] == "python3 /workspace/poc.py"
    assert [event["kind"] for event in graph.events_since(0)] == [
        "poc_saved",
        EV_POC_REPRODUCTION_REGISTERED,
    ]

    graph.close()
    reopened = SQLiteSharedGraph.open(
        db_path=tmp_path / "graph.db", challenge=_challenge()
    )
    replayed = reopened.get_poc_reproduction("poc-1")
    assert {key: replayed[key] for key in registration} == registration
    assert replayed["path"] == "/workspace/poc.py"
    assert replayed["name"] == "poc.py"
    assert replayed["entry_command"] == "python3 /workspace/poc.py"
    reopened.close()


def test_duplicate_registration_is_idempotent_and_conflict_is_immutable(tmp_path):
    graph = _graph(tmp_path)
    first = graph.register_poc_reproduction(
        actor="worker-1", poc_id="poc-1", indicator="vulnerable"
    )
    duplicate = graph.register_poc_reproduction(
        actor="worker-2", poc_id="poc-1", indicator="vulnerable"
    )
    assert duplicate == first
    assert len(graph.events_since(0, kinds=[EV_POC_REPRODUCTION_REGISTERED])) == 1

    with pytest.raises(ValueError, match="conflicting"):
        graph.register_poc_reproduction(
            actor="worker-2", poc_id="poc-1", indicator="still vulnerable"
        )
    materialized = graph.get_poc_reproduction("poc-1")
    assert {key: materialized[key] for key in first} == first
    assert materialized["path"] == "/workspace/poc.py"
    assert materialized["name"] == "poc.py"
    assert materialized["entry_command"] == "python3 /workspace/poc.py"
    rejected = graph.events_since(0, kinds=[EV_POC_REPRODUCTION_REJECTED])
    assert len(rejected) == 1
    assert rejected[0]["payload"]["poc_id"] == "poc-1"
    assert "still vulnerable" not in json.dumps(rejected[0]["payload"])
    graph.close()


def test_verification_activity_lease_and_terminal_state_are_append_only(tmp_path):
    graph = _graph(tmp_path)
    registration = graph.register_poc_reproduction(
        actor="worker-1", poc_id="poc-1", indicator="vulnerable"
    )
    started = graph.begin_poc_verification(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        reproduction_id=registration["reproduction_id"],
        worker_id="verifier-1",
        pool_identity="pool-a",
    )
    assert started["verification_id"] == "verification-1"
    assert graph.begin_poc_verification(
        actor="verifier-2",
        poc_id="poc-1",
        verification_id="verification-2",
        reproduction_id=registration["reproduction_id"],
        worker_id="verifier-2",
        pool_identity="pool-a",
    ) is None

    terminal_seq = graph.append_poc_verification_terminal(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        verified=True,
        exit_code=0,
        observed_location="stdout",
    )
    assert terminal_seq > 0
    status = graph.poc_verification_status("poc-1")
    assert status["status"] == "verified"
    assert status["verification_id"] == "verification-1"
    assert graph.append_poc_verification_terminal(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        verified=True,
        exit_code=0,
        observed_location="stdout",
    ) == -1
    assert [event["kind"] for event in graph.events_since(0)] == [
        "poc_saved",
        EV_POC_REPRODUCTION_REGISTERED,
        EV_POC_VERIFICATION_STARTED,
        EV_POC_VERIFIED,
    ]
    graph.close()


def test_failed_terminal_uses_closed_failure_enum(tmp_path):
    graph = _graph(tmp_path)
    registration = graph.register_poc_reproduction(
        actor="worker-1", poc_id="poc-1", indicator="vulnerable"
    )
    graph.begin_poc_verification(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        reproduction_id=registration["reproduction_id"],
        worker_id="verifier-1",
        pool_identity="pool-a",
    )
    seq = graph.append_poc_verification_terminal(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        verified=False,
        failure_reason=VerificationFailure.INDICATOR_NOT_OBSERVED,
        diagnostics="indicator was not present in captured output",
    )
    assert seq > 0
    status = graph.poc_verification_status("poc-1")
    assert status["status"] == "failed"
    assert status["failure_reason"] == VerificationFailure.INDICATOR_NOT_OBSERVED.value
    assert status["diagnostics"] == "indicator was not present in captured output"
    assert graph.events_since(0, kinds=[EV_POC_VERIFICATION_FAILED])[0]["payload"]["reason"] == (
        VerificationFailure.INDICATOR_NOT_OBSERVED.value
    )
    graph.close()


def test_poc_verification_does_not_change_fact_evidence(tmp_path):
    graph = _graph(tmp_path)
    graph.add_evidence(
        actor="worker-1", source="curl", fact="endpoint is reachable", verified=True
    )
    before = graph.verified_evidence()
    registration = graph.register_poc_reproduction(
        actor="worker-1", poc_id="poc-1", indicator="vulnerable"
    )
    graph.begin_poc_verification(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        reproduction_id=registration["reproduction_id"],
        worker_id="verifier-1",
        pool_identity="pool-a",
    )
    graph.append_poc_verification_terminal(
        actor="verifier-1",
        poc_id="poc-1",
        verification_id="verification-1",
        verified=True,
        exit_code=0,
        observed_location="stdout",
    )
    assert graph.verified_evidence() == before
    assert graph.snapshot().flag is None
    graph.close()
