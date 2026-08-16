from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_experiment import safe_reason_trace
from dswarm.swarm.advisor_sidecar import (
    AdvisorTraceAlreadyExists,
    AdvisorTraceCorrupt,
    AdvisorTraceSink,
    AdvisorWriterBusy,
    advisor_trace_path,
    fold_advisor_trace,
    reason_trace_payload,
)


FIXTURE = "m8-fixture::fixture"
SUMMARY = "m8-summary::summary"
RUN = "bench-run-1"


def _case_started(*, eligible=True, order=("baseline", "advisor")):
    return {
        "fixture_id": FIXTURE,
        "summary_digest": SUMMARY,
        "benchmark_run_id": RUN,
        "challenge_id": "challenge-1",
        "source_kind": "flag_found",
        "source_event_seq": 42,
        "source_intent_id": "intent-1",
        "source_route_hash": "route-web",
        "eligible": eligible,
        "trigger_reason": "eligible" if eligible else "single_flag_run",
        "arm_order": list(order),
        "available_fact_seqs": [10],
    }


def _trace(route="route-admin", facts=(10,)):
    result = ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id=f"intent-{route}", goal=f"inspect {route}",
            route_hash=route, worker_class="code", direction="web",
            priority=0.5, from_facts=list(facts),
        )],
        audit_notes=[],
        pinned_facts=[10],
        dispatches=[],
    )
    return safe_reason_trace(result, available_fact_seqs=(10,))


def _usage(status="measured"):
    if status == "unknown":
        return {"usage_status": "unknown", "input_tokens": None,
                "output_tokens": None, "usd": None}
    return {"usage_status": status, "input_tokens": 10,
            "output_tokens": 2, "usd": 0.01}


def _completed(arm, index, route="route-admin"):
    return {
        "arm": arm,
        "arm_index": index,
        "call_outcome": "succeeded",
        "started_ts": 1.0,
        "finished_ts": 2.0,
        "wall_seconds": 0.5,
        "safe_reason_trace": reason_trace_payload(_trace(route)),
        "usage": _usage(),
    }


def _append_clean(sink, *, order=("baseline", "advisor"), eligible=True):
    sink.append(kind="case_started", identity="case",
                payload=_case_started(eligible=eligible, order=order), ts=1.0)
    if eligible:
        sink.append(kind="suggestion_created", identity="suggestion-1", payload={
            "suggestion_id": "suggestion-1", "source_event_seq": 42,
            "route_attribution": "explicit",
        }, ts=1.1)
    for index, arm in enumerate(order):
        sink.append(kind=f"{arm}_started", identity=f"{arm}:{index}", payload={
            "arm": arm, "arm_index": index, "stage": "setup",
        }, ts=1.2 + index)
        if arm == "advisor":
            sink.append(kind="suggestion_consumed", identity="suggestion-1", payload={
                "suggestion_id": "suggestion-1", "arm": "advisor",
            }, ts=1.25 + index)
        sink.append(kind=f"{arm}_completed", identity=f"{arm}:{index}",
                    payload=_completed(arm, index, route=f"route-{arm}"),
                    ts=1.3 + index)
    trace_digest, comparison_digest = sink.current_digests()
    sink.append(kind="case_completed", identity="case", payload={
        "fixture_id": FIXTURE, "summary_digest": SUMMARY,
        "benchmark_run_id": RUN, "trace_result_digest": trace_digest,
        "comparison_digest": comparison_digest,
        "terminal_status": "clean",
    }, ts=5.0)


def test_trace_path_is_metrics_sidecar(tmp_path):
    assert advisor_trace_path(tmp_path) == (
        tmp_path / "metrics" / "advisor-experiment.jsonl"
    )


def test_append_flushes_and_fsyncs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    with AdvisorTraceSink(
        tmp_path, fixture_id=FIXTURE,
        summary_digest=SUMMARY, benchmark_run_id=RUN,
    ) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(), ts=1.0)
    assert calls


def test_second_writer_and_stale_lock_are_rejected(tmp_path):
    first = AdvisorTraceSink(
        tmp_path, fixture_id=FIXTURE,
        summary_digest=SUMMARY, benchmark_run_id=RUN,
    )
    with pytest.raises(AdvisorWriterBusy):
        AdvisorTraceSink(
            tmp_path, fixture_id=FIXTURE,
            summary_digest=SUMMARY, benchmark_run_id=RUN,
        )
    first.close()
    lock = tmp_path / "metrics" / "advisor-experiment.writer.lock"
    lock.write_text("stale", encoding="utf-8")
    with pytest.raises(AdvisorWriterBusy):
        AdvisorTraceSink(
            tmp_path, fixture_id=FIXTURE,
            summary_digest=SUMMARY, benchmark_run_id=RUN,
        )


def test_context_close_releases_owned_lock(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN):
        pass
    assert not (tmp_path / "metrics" / "advisor-experiment.writer.lock").exists()


def test_preexisting_nonempty_trace_is_write_once_and_lock_is_cleaned(tmp_path):
    trace = advisor_trace_path(tmp_path)
    trace.parent.mkdir(parents=True)
    trace.write_text("{}\n", encoding="utf-8")
    with pytest.raises(AdvisorTraceAlreadyExists):
        AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                         summary_digest=SUMMARY, benchmark_run_id=RUN)
    assert not (trace.parent / "advisor-experiment.writer.lock").exists()


def test_exact_duplicate_is_idempotent_but_conflicting_duplicate_is_corrupt(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        first = sink.append(kind="case_started", identity="case",
                            payload=_case_started(), ts=1.0)
        second = sink.append(kind="case_started", identity="case",
                             payload=_case_started(), ts=1.0)
        assert first == second
        assert advisor_trace_path(tmp_path).read_text(encoding="utf-8").count("\n") == 1
        changed = _case_started()
        changed["challenge_id"] = "different"
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="case_started", identity="case",
                        payload=changed, ts=1.0)


def test_payload_rejects_sensitive_keys_at_any_depth(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        payload = _case_started()
        payload["extra"] = {"prompt_text": "secret"}
        with pytest.raises(AdvisorTraceCorrupt) as caught:
            sink.append(kind="case_started", identity="case", payload=payload, ts=1.0)
        assert "secret" not in str(caught.value)


@pytest.mark.parametrize("order", [("baseline", "advisor"), ("advisor", "baseline")])
def test_eligible_complete_sequences_fold_clean(tmp_path, order):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        _append_clean(sink, order=order)
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "clean"
    assert fold.complete is True
    assert fold.case_completed is not None


def test_ineligible_baseline_only_sequence_folds_clean(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        _append_clean(sink, eligible=False, order=("baseline",))
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "clean"
    assert fold.advisor_started is None


def test_started_without_completion_and_partial_tail_are_incomplete(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(), ts=1.0)
    path = advisor_trace_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b'{"unterminated":')
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "incomplete"
    assert "partial_tail" in fold.reasons
    assert "missing_case_completed" in fold.reasons


def test_malformed_middle_line_is_corrupt(tmp_path):
    path = advisor_trace_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"bad":}\n{"also":"line"}\n')
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "corrupt"
    assert "malformed_line" in fold.reasons


def test_terminal_without_start_and_event_after_completion_are_corrupt(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(eligible=False, order=("baseline",)), ts=1.0)
        sink.append(kind="baseline_completed", identity="baseline:0",
                    payload=_completed("baseline", 0), ts=2.0)
    assert fold_advisor_trace(tmp_path).dataset_status == "corrupt"

    other = tmp_path / "other"
    with AdvisorTraceSink(other, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        _append_clean(sink, eligible=False, order=("baseline",))
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="case_interrupted", identity="case", payload={
                "interruption_code": "cancelled", "lifecycle_stage": "done",
            }, ts=6.0)


def test_consumed_and_rejected_or_ineligible_suggestion_is_corrupt(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(), ts=1.0)
        sink.append(kind="suggestion_created", identity="suggestion-1", payload={
            "suggestion_id": "suggestion-1", "source_event_seq": 42,
            "route_attribution": "explicit",
        }, ts=1.1)
        sink.append(kind="suggestion_consumed", identity="suggestion-1", payload={
            "suggestion_id": "suggestion-1", "arm": "advisor",
        }, ts=1.2)
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="suggestion_rejected", identity="suggestion-1", payload={
                "suggestion_id": "suggestion-1", "reason_code": "cancelled",
            }, ts=1.3)


def test_identity_mismatch_and_digest_mismatch_are_corrupt(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="case_started", identity="case", payload={
                **_case_started(), "fixture_id": "other",
            }, ts=1.0)

    other = tmp_path / "digest"
    with AdvisorTraceSink(other, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(eligible=False, order=("baseline",)), ts=1.0)
        sink.append(kind="baseline_started", identity="baseline:0", payload={
            "arm": "baseline", "arm_index": 0, "stage": "setup",
        }, ts=1.1)
        sink.append(kind="baseline_completed", identity="baseline:0",
                    payload=_completed("baseline", 0), ts=2.0)
        sink.append(kind="case_completed", identity="case", payload={
            "fixture_id": FIXTURE, "summary_digest": SUMMARY,
            "benchmark_run_id": RUN, "trace_result_digest": "wrong",
            "comparison_digest": "wrong", "terminal_status": "clean",
        }, ts=3.0)
    fold = fold_advisor_trace(other)
    assert fold.dataset_status == "corrupt"
    assert "digest_mismatch" in fold.reasons


def test_readonly_fold_missing_path_creates_nothing(tmp_path):
    before = set(tmp_path.rglob("*"))
    fold = fold_advisor_trace(tmp_path)
    after = set(tmp_path.rglob("*"))
    assert before == after
    assert fold.dataset_status == "incomplete"
    assert "missing_trace" in fold.reasons


def test_advisor_consumption_before_arm_start_is_corrupt(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(order=("advisor", "baseline")), ts=1.0)
        sink.append(kind="suggestion_created", identity="suggestion-1", payload={
            "suggestion_id": "suggestion-1", "source_event_seq": 42,
            "route_attribution": "explicit",
        }, ts=1.1)
        sink.append(kind="suggestion_consumed", identity="suggestion-1", payload={
            "suggestion_id": "suggestion-1", "arm": "advisor",
        }, ts=1.2)
        sink.append(kind="advisor_started", identity="advisor:0", payload={
            "arm": "advisor", "arm_index": 0, "stage": "setup",
        }, ts=1.3)
        sink.append(kind="advisor_completed", identity="advisor:0",
                    payload=_completed("advisor", 0), ts=1.4)
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "corrupt"
    assert "invalid_suggestion_sequence" in fold.reasons


def test_completed_and_failed_are_one_exclusive_live_terminal(tmp_path):
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(eligible=False, order=("baseline",)), ts=1.0)
        sink.append(kind="baseline_started", identity="baseline:0", payload={
            "arm": "baseline", "arm_index": 0, "stage": "setup",
        }, ts=1.1)
        sink.append(kind="baseline_completed", identity="baseline:0",
                    payload=_completed("baseline", 0), ts=1.2)
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="baseline_failed", identity="baseline:0", payload={
                "arm": "baseline", "arm_index": 0,
                "call_outcome": "planner_error", "failure_stage": "post_submit",
                "error_code": "planner_error", "started_ts": 1.0,
                "finished_ts": 2.0, "wall_seconds": 1.0,
                "usage": _usage("unknown"),
            }, ts=1.3)


def test_safe_trace_payload_rejects_tampered_enums_and_non_bool(tmp_path):
    completed = _completed("baseline", 0)
    completed["safe_reason_trace"]["verdict"] = "raw-model-value"
    completed["safe_reason_trace"]["intents"][0]["requires_recon"] = "yes"
    with AdvisorTraceSink(tmp_path, fixture_id=FIXTURE,
                          summary_digest=SUMMARY, benchmark_run_id=RUN) as sink:
        sink.append(kind="case_started", identity="case",
                    payload=_case_started(eligible=False, order=("baseline",)), ts=1.0)
        sink.append(kind="baseline_started", identity="baseline:0", payload={
            "arm": "baseline", "arm_index": 0, "stage": "setup",
        }, ts=1.1)
        with pytest.raises(AdvisorTraceCorrupt):
            sink.append(kind="baseline_completed", identity="baseline:0",
                        payload=completed, ts=1.2)
