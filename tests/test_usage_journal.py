"""M5 Phase 2: crash-safe two-phase usage journal acceptance tests."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dswarm.core.usage_journal import (
    AccountingUnavailable,
    UsageCall,
    UsageJournal,
    UsageJournalCorrupt,
    UsageRecord,
)


def _call(index: int = 1, *, run_id: str = "run-1") -> UsageCall:
    return UsageCall(
        provider_call_id=f"call-{index}",
        producer="internal",
        run_id=run_id,
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id="solver-1",
        profile_id="pi-web",
        configured_account_id="deepseek-primary",
        billing_account_id="deepseek-primary",
    )


def _finished(call: UsageCall, *, usd: float = 0.0125) -> UsageRecord:
    return UsageRecord.from_call(
        call,
        call_outcome="succeeded",
        usage_status="measured",
        input_tokens=120,
        output_tokens=30,
        usd=usd,
    )


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_started_is_fsynced_before_mock_upstream_receives_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run-1-usage-journal.jsonl"
    journal = UsageJournal(path)
    durable = False
    upstream_calls = 0
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        nonlocal durable
        real_fsync(fd)
        durable = True

    def mock_upstream() -> None:
        nonlocal upstream_calls
        upstream_calls += 1
        assert durable is True
        rows = _rows(path)
        assert rows[0] == {"format": "usage-journal", "version": 1}
        assert rows[-1]["phase"] == "started"
        assert rows[-1]["provider_call_id"] == "call-1"

    monkeypatch.setattr(os, "fsync", recording_fsync)
    journal.append_started(_call())
    mock_upstream()

    assert upstream_calls == 1


def test_started_only_reconciles_to_interrupted_unknown_without_zero_cost(
    tmp_path: Path,
) -> None:
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    call = _call()
    journal.append_started(call)

    reopened = UsageJournal(journal.path)
    pending = reopened.reconcile(set())

    assert len(pending) == 1
    recovered = pending[0]
    assert recovered.usage_id == call.usage_id
    assert recovered.provider_call_id == call.provider_call_id
    assert recovered.call_outcome == "interrupted"
    assert recovered.usage_status == "unknown"
    assert recovered.input_tokens is None
    assert recovered.output_tokens is None
    assert recovered.usd is None


def test_started_prewrite_failure_is_accounting_unavailable_before_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    upstream_calls = 0

    def failing_fsync(_fd: int) -> None:
        raise OSError("disk unavailable")

    def mock_upstream() -> None:
        nonlocal upstream_calls
        upstream_calls += 1

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(AccountingUnavailable) as caught:
        journal.append_started(_call())
        mock_upstream()

    assert upstream_calls == 0
    assert caught.value.status_code == 503
    assert caught.value.code == "accounting_unavailable"
    assert caught.value.alert_payload["level"] == "error"
    assert caught.value.alert_payload["category"] == "accounting_unavailable"


def test_concurrent_journal_instances_serialize_same_file_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "run-1-usage-journal.jsonl"
    journals = [UsageJournal(path), UsageJournal(path)]
    state_lock = threading.Lock()
    active_fsync = 0
    max_active_fsync = 0
    real_fsync = os.fsync

    def slow_fsync(fd: int) -> None:
        nonlocal active_fsync, max_active_fsync
        with state_lock:
            active_fsync += 1
            max_active_fsync = max(max_active_fsync, active_fsync)
        time.sleep(0.003)
        real_fsync(fd)
        with state_lock:
            active_fsync -= 1

    monkeypatch.setattr(os, "fsync", slow_fsync)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(journals[index % 2].append_started, _call(index))
            for index in range(1, 41)
        ]
        for future in futures:
            future.result(timeout=5)

    rows = _rows(path)
    assert rows[0] == {"format": "usage-journal", "version": 1}
    assert len(rows) == 41
    assert {row["provider_call_id"] for row in rows[1:]} == {
        f"call-{index}" for index in range(1, 41)
    }
    assert max_active_fsync == 1


def test_reconcile_is_idempotent_by_usage_id(tmp_path: Path) -> None:
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    call = _call()
    terminal = _finished(call)
    journal.append_started(call)
    journal.append_started(call)
    journal.append_finished(terminal)
    journal.append_finished(terminal)

    reopened = UsageJournal(journal.path)
    first = reopened.reconcile(set())
    repeated = reopened.reconcile(set())
    after_canonical = reopened.reconcile({call.usage_id})

    assert first == [terminal]
    assert repeated == first
    assert after_canonical == []


@pytest.mark.parametrize("field", ["configured_account_id", "billing_account_id"])
def test_empty_account_ids_are_rejected_in_started_claims(field: str) -> None:
    values = {
        "provider_call_id": "call-empty-account",
        "producer": "internal",
        "run_id": "run-1",
        "challenge_id": "challenge-1",
        "worker_instance_id": "worker-1",
        "solver_id": "solver-1",
        "profile_id": "pi-web",
        "configured_account_id": None,
        "billing_account_id": None,
    }
    values[field] = ""

    with pytest.raises(ValueError, match="account ids must be None or non-empty"):
        UsageCall(**values)


@pytest.mark.parametrize("field", ["configured_account_id", "billing_account_id"])
def test_empty_account_ids_are_rejected_in_terminal_records(field: str) -> None:
    values = {
        "usage_id": "usage::run-1::fallback::invocation-1",
        "producer": "fallback",
        "record_kind": "invocation_aggregate",
        "provider_call_id": None,
        "invocation_id": "invocation-1",
        "run_id": "run-1",
        "challenge_id": "challenge-1",
        "worker_instance_id": "worker-1",
        "solver_id": "solver-1",
        "profile_id": "pi-web",
        "configured_account_id": None,
        "billing_account_id": None,
        "call_outcome": "succeeded",
        "usage_status": "estimated",
        "input_tokens": 1,
        "output_tokens": 1,
        "usd": 0.01,
    }
    values[field] = ""

    with pytest.raises(ValueError, match="account ids must be None or non-empty"):
        UsageRecord(**values)


def test_fallback_cannot_claim_provider_call_identity() -> None:
    with pytest.raises(ValueError, match="provider_call requires internal or gateway"):
        UsageRecord(
            usage_id="usage::run-1::fallback::call-invalid",
            producer="fallback",
            record_kind="provider_call",
            provider_call_id="call-invalid",
            invocation_id=None,
            run_id="run-1",
            challenge_id="challenge-1",
            worker_instance_id="worker-1",
            solver_id="solver-1",
            profile_id="pi-web",
            configured_account_id=None,
            billing_account_id=None,
            call_outcome="succeeded",
            usage_status="unknown",
            input_tokens=None,
            output_tokens=None,
            usd=None,
        )


@pytest.mark.parametrize(
    ("producer", "record_kind", "usage_status"),
    [
        ("internal", "provider_call", "estimated"),
        ("gateway", "provider_call", "estimated"),
        ("fallback", "invocation_aggregate", "measured"),
    ],
)
def test_usage_status_must_match_producer_contract(
    producer: str, record_kind: str, usage_status: str
) -> None:
    provider_call_id = "call-invalid" if record_kind == "provider_call" else None
    invocation_id = "invocation-invalid" if record_kind == "invocation_aggregate" else None
    if provider_call_id is not None:
        usage_id = f"usage::run-1::{producer}::{provider_call_id}"
    else:
        usage_id = f"usage::run-1::fallback::{invocation_id}"

    with pytest.raises(ValueError, match="usage_status is incompatible with producer"):
        UsageRecord(
            usage_id=usage_id,
            producer=producer,
            record_kind=record_kind,
            provider_call_id=provider_call_id,
            invocation_id=invocation_id,
            run_id="run-1",
            challenge_id="challenge-1",
            worker_instance_id="worker-1",
            solver_id="solver-1",
            profile_id="pi-web",
            configured_account_id=None,
            billing_account_id=None,
            call_outcome="succeeded",
            usage_status=usage_status,
            input_tokens=1,
            output_tokens=1,
            usd=0.01,
        )


def test_malformed_started_row_raises_usage_journal_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "run-1-usage-journal.jsonl"
    path.write_text(
        '\n'.join(
            [
                json.dumps({"format": "usage-journal", "version": 1}),
                json.dumps(
                    {
                        "phase": "started",
                        "provider_call_id": "call-missing-producer",
                        "run_id": "run-1",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(UsageJournalCorrupt, match="invalid started usage record"):
        UsageJournal(path).reconcile(set())

from dswarm.core.usage_journal import InvocationCall


def _invocation(index: int = 1, *, run_id: str = "run-1") -> InvocationCall:
    return InvocationCall(
        invocation_id=f"invocation-{index}",
        run_id=run_id,
        challenge_id="challenge-1",
        worker_instance_id="worker-1",
        solver_id="solver-1",
        profile_id="pi-web",
        configured_account_id=None,
        billing_account_id=None,
    )


def test_fallback_invocation_terminal_can_be_journaled_and_reconciled(tmp_path: Path) -> None:
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    invocation = _invocation()
    journal.append_started(invocation)
    terminal = UsageRecord.from_invocation(
        invocation,
        call_outcome="succeeded",
        usage_status="estimated",
        input_tokens=12,
        output_tokens=4,
        usd=0.003,
    )
    journal.append_finished(terminal)

    assert journal.reconcile(set()) == [terminal]
    assert journal.reconcile({terminal.usage_id}) == []


def test_fallback_started_only_reconciles_to_unknown_without_zero_cost(tmp_path: Path) -> None:
    journal = UsageJournal(tmp_path / "run-1-usage-journal.jsonl")
    invocation = _invocation()
    journal.append_started(invocation)

    recovered = journal.reconcile(set())[0]

    assert recovered.usage_id == invocation.usage_id
    assert recovered.record_kind == "invocation_aggregate"
    assert recovered.provider_call_id is None
    assert recovered.invocation_id == invocation.invocation_id
    assert recovered.call_outcome == "interrupted"
    assert recovered.usage_status == "unknown"
    assert recovered.input_tokens is None
    assert recovered.output_tokens is None
    assert recovered.usd is None
