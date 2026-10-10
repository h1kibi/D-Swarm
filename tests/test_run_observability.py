from __future__ import annotations

import json

import httpx
import pytest
from types import SimpleNamespace

from apps.web.run_manager import RunManager
from apps.web.run_observability import project_run_observability, public_binding
from apps.web.server import create_app
from dswarm.core.events import Event, EventType
from dswarm.core.usage_journal import UsageCall, UsageRecord


class _Store:
    def __init__(self, events):
        self.events = events

    def read_events(self, run_id: str):
        return list(self.events)


def _run(*, events=(), records=None, binding=None, status="running", updated_at=0.0, finished=False):
    return SimpleNamespace(
        run_id="run-observe",
        store=_Store(events),
        ledger=SimpleNamespace(records=records or {}),
        binding_snapshot=binding or {},
        updated_at=updated_at,
        finished=finished,
        status=lambda: status,
        inherited_verified_facts=[],
    )


def _record(call_id: str, *, solver_id=None, profile_id=None, operation_kind="provider_call",
            usage_status="measured", input_tokens=10, output_tokens=5, usd=0.01):
    call = UsageCall(
        provider_call_id=call_id,
        producer="internal",
        run_id="run-observe",
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id=solver_id,
        profile_id=profile_id,
        configured_account_id=None,
        billing_account_id=None,
        operation_kind=operation_kind,
    )
    return UsageRecord.from_call(
        call,
        call_outcome="succeeded",
        usage_status=usage_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=usd,
    )


def test_observability_projects_lifecycle_and_usage_by_role():
    reason = _record("reason-call", solver_id="reason", profile_id="planner", operation_kind="reason", input_tokens=10, output_tokens=5, usd=0.02)
    worker = _record("worker-call", solver_id="worker-1", profile_id="web", input_tokens=20, output_tokens=7, usd=0.03)
    review = _record("review-call", solver_id="reviewer", profile_id="review", operation_kind="revalidation", input_tokens=4, output_tokens=2, usd=0.01)
    events = [
        Event(event_type=EventType.RUN_STARTED, run_id="run-observe", ts=100.0),
        Event(event_type=EventType.USAGE_RECORDED, run_id="run-observe", ts=105.0, payload={"usage_id": reason.usage_id}),
        Event(event_type=EventType.USAGE_RECORDED, run_id="run-observe", ts=106.0, payload={"usage_id": worker.usage_id}),
        Event(event_type=EventType.USAGE_RECORDED, run_id="run-observe", ts=107.0, payload={"usage_id": review.usage_id}),
        Event(event_type=EventType.RUN_FINISHED, run_id="run-observe", ts=112.5),
    ]
    snapshot = {
        "version": 1,
        "roles": {
            "reason": {"provider_ref": "relay-a", "model": "reason-v1"},
            "worker": {"provider_ref": "relay-b", "model": "worker-v1"},
            "review": {"provider_ref": "relay-c", "model": "review-v1"},
        },
    }

    result = project_run_observability(
        _run(events=events, records={r.usage_id: r for r in (reason, worker, review)}, binding=snapshot, status="finished"),
        now=999.0,
    )

    assert result["lifecycle"] == {
        "status": "finished",
        "started_at": 100.0,
        "finished_at": 112.5,
        "elapsed_ms": 12500,
        "updated_at": None,
    }
    assert result["usage"]["calls"] == 3
    assert result["usage"]["tokens"] == 48
    assert result["usage"]["input_tokens"] == 34
    assert result["usage"]["output_tokens"] == 14
    assert result["usage"]["usd"] == pytest.approx(0.06)
    roles = {row["role"]: row for row in result["binding"]["roles"]}
    assert roles["reason"]["usage"]["calls"] == 1
    assert roles["review"]["usage"]["tokens"] == 6
    assert roles["worker"]["status"] == "active"
    assert result["health"]["status"] == "healthy"




def test_observability_projects_read_only_inherited_target_facts():
    result = project_run_observability(
        _run(),
        now=20.0,
    )
    # Exercise the projection with a context snapshot while leaving binding
    # and event/ledger state independent.
    run = _run()
    run.inherited_verified_facts = [{
        "fact_id": "fact:run-old:7",
        "fact": "/admin requires auth",
        "provenance_status": "verified",
        "source_run_id": "run-old",
        "fact_seq": 7,
        "artifact_id": "artifact-7",
    }]
    result = project_run_observability(run, now=20.0)
    assert result["knowledge"]["read_only"] is True
    assert result["knowledge"]["source"] == "target_verified_facts"
    assert result["knowledge"]["inherited"][0]["fact_id"] == "fact:run-old:7"
    assert result["knowledge"]["inherited"][0]["source_run_id"] == "run-old"
    assert result["knowledge"]["inherited"][0]["fact_seq"] == 7

def test_observability_marks_unknown_and_estimated_usage():
    unknown = _record("unknown-call", solver_id="worker-1", usage_status="unknown", input_tokens=None, output_tokens=None, usd=None)
    estimated = SimpleNamespace(usage_id="estimated-call", solver_id="worker-2", profile_id="web", operation_kind="provider_call", usage_status="estimated", input_tokens=3, output_tokens=2, usd=0.0)
    result = project_run_observability(
        _run(records={unknown.usage_id: unknown, estimated.usage_id: estimated}, binding={"roles": {"worker": {"model": "m"}}}),
        now=50.0,
    )
    usage = result["usage"]
    assert usage["calls"] == 2
    assert usage["unknown_calls"] == 1
    assert usage["estimated_calls"] == 1
    assert usage["tokens"] == 5
    assert usage["usd"] == 0.0


def test_observability_projects_provider_health_without_raw_exception_text():
    event = Event(
        event_type=EventType.PROVIDER_ERROR,
        run_id="run-observe",
        solver_id="reviewer",
        ts=20.0,
        payload={
            "phase": "review",
            "severity": "fatal",
            "category": "authentication",
            "provider": "anthropic",
            "user_message": "Review provider authentication failed",
            "suggested_action": "Check the credential account",
            "raw_message": "Authorization: Bearer SUPER-SECRET",
            "api_key": "SUPER-SECRET",
        },
    )
    result = project_run_observability(
        _run(events=[event], binding={"roles": {"review": {"provider": "anthropic", "model": "claude"}}}, status="running"),
        now=30.0,
    )
    review = next(row for row in result["binding"]["roles"] if row["role"] == "review")
    assert review["status"] == "blocked"
    assert result["health"]["status"] == "blocked"
    assert review["issues"][0]["message"] == "Review provider authentication failed"
    encoded = json.dumps(result, ensure_ascii=False)
    assert "SUPER-SECRET" not in encoded
    assert "Authorization" not in encoded


def test_public_binding_allowlist_keeps_routing_metadata_and_drops_secrets():
    result = public_binding({
        "provider_ref": "relay",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://relay.invalid/v1?api_key=SECRET",
        "credential_account": "ops-account",
        "api_key": "SECRET",
        "token": "SECRET",
        "authorization": "Bearer SECRET",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "password": "SECRET",
        "nested": {"secret": "SECRET"},
    })
    assert result == {
        "provider_ref": "relay",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://relay.invalid",
        "credential_account": "ops-account",
    }
    assert "secret" not in json.dumps(result).lower()
    assert public_binding({"base_url": "https://relay.invalid/v1?api_key=SECRET"}) == {
        "base_url": "https://relay.invalid"
    }


def test_observability_handles_legacy_run_without_store_or_binding():
    run = SimpleNamespace(
        run_id="legacy",
        store=None,
        ledger=None,
        binding_snapshot=None,
        updated_at=42.0,
        finished=False,
        status=lambda: "draft",
    )
    result = project_run_observability(run, now=100.0)
    assert result["binding"]["frozen"] is False
    assert result["binding"]["source"] == "unavailable"
    assert result["lifecycle"]["started_at"] == 42.0
    assert result["lifecycle"]["elapsed_ms"] == 58000
    assert result["health"]["status"] == "unknown"



def test_observability_projects_reason_health_counts_and_blocked_state():
    """docs/17 §5.4/§5.5: Run Health must expose the Reason state machine's
    failure counts, thresholds and operator-blocked state — without model text."""
    from dswarm.core.events import blackboard_delta_payload

    def delta(kind: str, **fields):
        return Event(
            event_type=EventType.BLACKBOARD_DELTA,
            run_id="run-observe",
            ts=float(fields.pop("_ts", 10.0)),
            payload=blackboard_delta_payload(kind, actor="reason", delta_type=kind, **fields),
        )

    events = [
        delta("reason_health_changed", _ts=10.0, health_state="degraded", failures=1,
              max_failures=6, request_failures=1, request_limit=3, retry_window_s=60),
        delta("reason_health_changed", _ts=12.0, health_state="retrying", failures=2,
              max_failures=6, request_failures=3, request_limit=3, retry_window_s=60,
              operator_attention=True),
    ]
    result = project_run_observability(_run(events=events), now=20.0)
    reason_health = result["health"]["reason"]
    assert reason_health["state"] == "retrying"
    assert reason_health["failures"] == 2
    assert reason_health["max_failures"] == 6
    assert reason_health["request_failures"] == 3
    assert reason_health["request_limit"] == 3
    assert reason_health["retry_window_s"] == 60
    assert reason_health["retry_not_before"] == 72.0
    assert result["health"]["status"] == "degraded"
    reason_row = next(row for row in result["binding"]["roles"] if row["role"] == "reason")
    assert reason_row["status"] == "degraded"

    blocked = project_run_observability(
        _run(events=[
            *events,
            delta("reason_health_changed", _ts=14.0, health_state="circuit_open",
                  failures=6, max_failures=6, request_failures=3, request_limit=3),
            delta("reason_blocked", _ts=15.0, stage="reason", reason="planner_unavailable"),
        ]),
        now=20.0,
    )
    assert blocked["health"]["reason"]["state"] == "reason_blocked"
    assert blocked["health"]["reason"]["operator_attention"] is True
    assert blocked["health"]["status"] == "blocked"
    blocked_row = next(row for row in blocked["binding"]["roles"] if row["role"] == "reason")
    assert blocked_row["status"] == "blocked"
    assert blocked_row["operator_attention"] is True

    # A recovered planner clears the block and the failure counters.
    recovered = project_run_observability(
        _run(events=[*events,
                     delta("reason_health_changed", _ts=14.0, health_state="circuit_open",
                           failures=6, max_failures=6),
                     delta("reason_recovered", _ts=16.0, reason="planner_cycle_succeeded")]),
        now=20.0,
    )
    assert recovered["health"]["reason"]["state"] == "healthy"
    assert recovered["health"]["reason"]["failures"] == 0
    assert recovered["health"]["reason"]["operator_attention"] is False

    # A Reviewer intent-quality block (intent id on a non-reason stage) is not a
    # run-level Reason block.
    scoped = project_run_observability(
        _run(events=[delta("reason_health_changed", _ts=10.0, health_state="healthy"),
                     delta("reason_blocked", _ts=11.0, stage="review",
                           intent_id="I-1", reason="reviewer unavailable")]),
        now=20.0,
    )
    assert scoped["health"]["reason"]["state"] == "healthy"


@pytest.mark.asyncio
async def test_observability_api_is_read_only_and_secret_free(tmp_path):
    manager = RunManager(sessions_root=tmp_path)
    run = manager.create_new()
    run.binding_snapshot = {
        "version": 1,
        "roles": {
            "worker": {
                "provider": "openai",
                "model": "gpt-test",
                "api_key": "DO-NOT-SHIP",
            }
        },
    }
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run.run_id}/observability")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run.run_id
    assert payload["binding"]["frozen"] is True
    assert any(row["role"] == "worker" for row in payload["binding"]["roles"])
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "DO-NOT-SHIP" not in encoded
    assert "api_key" not in encoded

    # The endpoint is a projection: fetching it cannot mutate the launch snapshot.
    assert run.binding_snapshot["roles"]["worker"]["api_key"] == "DO-NOT-SHIP"
