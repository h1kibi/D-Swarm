"""Append-only, offline-only sidecar for the M8 Advisor experiment."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
import json
import math
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence
from uuid import uuid4

from dswarm.swarm.advisor_experiment import (
    AdvisorIntentTrace,
    AdvisorReasonTrace,
    compare_intent_traces,
)

_SCHEMA_VERSION = 1
_TRACE_KINDS = {
    "case_started", "suggestion_created",
    "baseline_started", "baseline_completed", "baseline_failed",
    "advisor_started", "advisor_completed", "advisor_failed",
    "suggestion_consumed", "suggestion_rejected",
    "case_interrupted", "case_completed",
}
_SENSITIVE_KEYS = {
    "graph_summary", "experimental_summary", "prompt", "prompt_text",
    "raw_flag", "flag_value", "reference_objectives", "reference_ids",
    "goal", "rationale", "audit_notes", "drift", "complete_why",
    "dispatches", "error_message",
}
_ARM_KINDS = {
    "baseline_started", "baseline_completed", "baseline_failed",
    "advisor_started", "advisor_completed", "advisor_failed",
}
_ALLOWED_KEYS = {
    "case_started": {
        "fixture_id", "summary_digest", "benchmark_run_id", "challenge_id",
        "source_kind", "source_event_seq", "source_intent_id",
        "source_route_hash", "eligible", "trigger_reason", "arm_order",
        "available_fact_seqs",
    },
    "suggestion_created": {
        "suggestion_id", "source_event_seq", "route_attribution",
    },
    "baseline_started": {"arm", "arm_index", "stage"},
    "advisor_started": {"arm", "arm_index", "stage"},
    "baseline_completed": {
        "arm", "arm_index", "call_outcome", "started_ts", "finished_ts",
        "wall_seconds", "safe_reason_trace", "usage",
    },
    "advisor_completed": {
        "arm", "arm_index", "call_outcome", "started_ts", "finished_ts",
        "wall_seconds", "safe_reason_trace", "usage",
    },
    "baseline_failed": {
        "arm", "arm_index", "call_outcome", "failure_stage", "error_code",
        "started_ts", "finished_ts", "wall_seconds", "usage",
    },
    "advisor_failed": {
        "arm", "arm_index", "call_outcome", "failure_stage", "error_code",
        "started_ts", "finished_ts", "wall_seconds", "usage",
    },
    "suggestion_consumed": {"suggestion_id", "arm"},
    "suggestion_rejected": {"suggestion_id", "reason_code"},
    "case_interrupted": {"interruption_code", "lifecycle_stage"},
    "case_completed": {
        "fixture_id", "summary_digest", "benchmark_run_id",
        "trace_result_digest", "comparison_digest", "terminal_status",
    },
}
_REQUIRED_KEYS = {kind: frozenset(keys) for kind, keys in _ALLOWED_KEYS.items()}
_REQUIRED_KEYS["case_started"] = frozenset(
    _ALLOWED_KEYS["case_started"] - {"available_fact_seqs"}
)


class AdvisorTraceError(RuntimeError):
    """Base fixed-code sidecar error."""


class AdvisorTraceCorrupt(AdvisorTraceError):
    """The proposed event or stored trace violates the frozen contract."""


class AdvisorTraceAlreadyExists(AdvisorTraceError):
    """M8 v1 traces are write-once per case root."""


class AdvisorWriterBusy(AdvisorTraceError):
    """Another writer owns this case root, including a stale lock owner."""


@dataclass(frozen=True, kw_only=True)
class AdvisorTraceEvent:
    schema_version: int
    event_id: str
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    kind: str
    ts: float
    payload: Mapping[str, object]


@dataclass(frozen=True, kw_only=True)
class AdvisorTraceFold:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    complete: bool
    reasons: tuple[str, ...]
    events: tuple[AdvisorTraceEvent, ...]
    case_started: AdvisorTraceEvent | None
    suggestion_created: AdvisorTraceEvent | None
    suggestion_terminal: AdvisorTraceEvent | None
    baseline_started: AdvisorTraceEvent | None
    baseline_terminal: AdvisorTraceEvent | None
    advisor_started: AdvisorTraceEvent | None
    advisor_terminal: AdvisorTraceEvent | None
    case_interrupted: AdvisorTraceEvent | None
    case_completed: AdvisorTraceEvent | None


def advisor_trace_path(case_root: str | os.PathLike[str]) -> Path:
    return Path(case_root) / "metrics" / "advisor-experiment.jsonl"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_digest(domain: str, value: Any) -> str:
    value_hash = blake2b(_canonical_json([domain, value]), digest_size=16).hexdigest()
    return f"{domain}::{value_hash}"


def _fixed_error(code: str) -> AdvisorTraceCorrupt:
    return AdvisorTraceCorrupt(code)


def _finite_number(value: Any, field: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fixed_error(f"invalid_{field}")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise _fixed_error(f"invalid_{field}")
    return number


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fixed_error(f"invalid_{field}")
    if positive and value <= 0:
        raise _fixed_error(f"invalid_{field}")
    return value


def _plain_json(value: Any) -> Any:
    try:
        return json.loads(_canonical_json(value).decode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _fixed_error("non_json_payload") from exc


def _reject_sensitive(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if key_text in _SENSITIVE_KEYS:
                raise _fixed_error(f"sensitive_key:{key_text}")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)


def _strict_keys(kind: str, payload: Mapping[str, Any]) -> None:
    keys = set(payload)
    if keys - _ALLOWED_KEYS[kind]:
        raise _fixed_error("unexpected_payload_key")
    if _REQUIRED_KEYS[kind] - keys:
        raise _fixed_error("missing_payload_key")


def _validate_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fixed_error("invalid_usage")
    allowed = {"usage_status", "input_tokens", "output_tokens", "usd"}
    if set(value) != allowed:
        raise _fixed_error("invalid_usage")
    status = value.get("usage_status")
    if status not in {"measured", "estimated", "unknown"}:
        raise _fixed_error("invalid_usage_status")
    output = dict(value)
    numeric = ("input_tokens", "output_tokens", "usd")
    if status == "unknown":
        if any(output.get(key) is not None for key in numeric):
            raise _fixed_error("invalid_unknown_usage")
    else:
        for key in numeric:
            item = output.get(key)
            if item is not None:
                _finite_number(item, key, nonnegative=True)
    return output


def reason_trace_payload(trace: AdvisorReasonTrace) -> dict[str, object]:
    return {
        "goal_met": trace.goal_met,
        "verdict": trace.verdict,
        "intents": [
            {
                "intent_key": intent.intent_key,
                "goal_fingerprint": intent.goal_fingerprint,
                "route_fingerprint": intent.route_fingerprint,
                "worker_class": intent.worker_class,
                "priority": intent.priority,
                "from_facts": list(intent.from_facts),
                "direction": intent.direction,
                "requires_recon": intent.requires_recon,
                "host_scan": intent.host_scan,
            }
            for intent in trace.intents
        ],
        "audit_note_count": trace.audit_note_count,
        "pinned_facts": list(trace.pinned_facts),
        "dispatch_count": trace.dispatch_count,
    }


def reason_trace_from_payload(payload: Any) -> AdvisorReasonTrace:
    if not isinstance(payload, Mapping):
        raise _fixed_error("invalid_safe_reason_trace")
    expected = {
        "goal_met", "verdict", "intents", "audit_note_count",
        "pinned_facts", "dispatch_count",
    }
    if set(payload) != expected or not isinstance(payload.get("intents"), list):
        raise _fixed_error("invalid_safe_reason_trace")
    intents: list[AdvisorIntentTrace] = []
    intent_keys = {
        "intent_key", "goal_fingerprint", "route_fingerprint", "worker_class",
        "priority", "from_facts", "direction", "requires_recon", "host_scan",
    }
    if not isinstance(payload["goal_met"], bool):
        raise _fixed_error("invalid_safe_reason_trace")
    if payload["verdict"] not in {"complete", "course_correct", "explore"}:
        raise _fixed_error("invalid_safe_reason_trace")
    for raw in payload["intents"]:
        if not isinstance(raw, Mapping) or set(raw) != intent_keys:
            raise _fixed_error("invalid_safe_reason_trace")
        if raw["worker_class"] not in {"code", "shell_agent", "verifier", "review"}:
            raise _fixed_error("invalid_safe_reason_trace")
        if raw["direction"] not in {"", "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"}:
            raise _fixed_error("invalid_safe_reason_trace")
        if not isinstance(raw["requires_recon"], bool) or not isinstance(raw["host_scan"], bool):
            raise _fixed_error("invalid_safe_reason_trace")
        facts = raw.get("from_facts")
        if not isinstance(facts, list):
            raise _fixed_error("invalid_safe_reason_trace")
        fact_tuple = tuple(_integer(item, "from_facts", positive=True) for item in facts)
        intents.append(AdvisorIntentTrace(
            intent_key=str(raw["intent_key"]),
            goal_fingerprint=str(raw["goal_fingerprint"]),
            route_fingerprint=str(raw["route_fingerprint"]),
            worker_class=str(raw["worker_class"]),  # type: ignore[arg-type]
            priority=_finite_number(raw["priority"], "priority"),
            from_facts=fact_tuple,
            direction=str(raw["direction"]),  # type: ignore[arg-type]
            requires_recon=bool(raw["requires_recon"]),
            host_scan=bool(raw["host_scan"]),
        ))
    pins = payload.get("pinned_facts")
    if not isinstance(pins, list):
        raise _fixed_error("invalid_safe_reason_trace")
    return AdvisorReasonTrace(
        goal_met=bool(payload["goal_met"]),
        verdict=str(payload["verdict"]),  # type: ignore[arg-type]
        intents=tuple(intents),
        audit_note_count=_integer(payload["audit_note_count"], "audit_note_count"),
        pinned_facts=tuple(_integer(item, "pinned_facts", positive=True) for item in pins),
        dispatch_count=_integer(payload["dispatch_count"], "dispatch_count"),
    )


def _validate_payload(kind: str, value: Any) -> dict[str, Any]:
    if kind not in _TRACE_KINDS or not isinstance(value, Mapping):
        raise _fixed_error("invalid_event_kind")
    _reject_sensitive(value)
    payload = _plain_json(value)
    _strict_keys(kind, payload)

    if kind == "case_started":
        if payload["source_kind"] != "flag_found":
            raise _fixed_error("invalid_source_kind")
        _integer(payload["source_event_seq"], "source_event_seq", positive=True)
        if not isinstance(payload["eligible"], bool):
            raise _fixed_error("invalid_eligible")
        order = payload["arm_order"]
        expected = {"baseline", "advisor"} if payload["eligible"] else {"baseline"}
        if not isinstance(order, list) or len(order) != len(expected) or set(order) != expected:
            raise _fixed_error("invalid_arm_order")
        facts = payload.get("available_fact_seqs", [])
        if not isinstance(facts, list):
            raise _fixed_error("invalid_available_fact_seqs")
        payload["available_fact_seqs"] = sorted(set(
            _integer(item, "available_fact_seqs", positive=True) for item in facts
        ))
    elif kind == "suggestion_created":
        _integer(payload["source_event_seq"], "source_event_seq", positive=True)
        if payload["route_attribution"] not in {"explicit", "unattributed"}:
            raise _fixed_error("invalid_route_attribution")
    elif kind in {"baseline_started", "advisor_started"}:
        expected_arm = kind.split("_", 1)[0]
        if payload["arm"] != expected_arm or payload["stage"] != "setup":
            raise _fixed_error("invalid_arm_start")
        _integer(payload["arm_index"], "arm_index")
    elif kind in {"baseline_completed", "advisor_completed"}:
        expected_arm = kind.split("_", 1)[0]
        if payload["arm"] != expected_arm or payload["call_outcome"] != "succeeded":
            raise _fixed_error("invalid_arm_completion")
        _integer(payload["arm_index"], "arm_index")
        _finite_number(payload["started_ts"], "started_ts")
        _finite_number(payload["finished_ts"], "finished_ts")
        _finite_number(payload["wall_seconds"], "wall_seconds", nonnegative=True)
        payload["safe_reason_trace"] = reason_trace_payload(
            reason_trace_from_payload(payload["safe_reason_trace"])
        )
        payload["usage"] = _validate_usage(payload["usage"])
    elif kind in {"baseline_failed", "advisor_failed"}:
        expected_arm = kind.split("_", 1)[0]
        if payload["arm"] != expected_arm:
            raise _fixed_error("invalid_arm_failure")
        if payload["call_outcome"] not in {"planner_error", "timeout", "setup_error"}:
            raise _fixed_error("invalid_call_outcome")
        if payload["failure_stage"] not in {"pre_submit", "post_submit"}:
            raise _fixed_error("invalid_failure_stage")
        _integer(payload["arm_index"], "arm_index")
        _finite_number(payload["started_ts"], "started_ts")
        _finite_number(payload["finished_ts"], "finished_ts")
        _finite_number(payload["wall_seconds"], "wall_seconds", nonnegative=True)
        payload["usage"] = _validate_usage(payload["usage"])
    elif kind == "suggestion_consumed":
        if payload["arm"] != "advisor":
            raise _fixed_error("invalid_suggestion_consumer")
    elif kind == "case_completed":
        if payload["terminal_status"] not in {"clean", "incomplete"}:
            raise _fixed_error("invalid_terminal_status")
    return payload


def _identity_from_payload(kind: str, payload: Mapping[str, Any]) -> str:
    if kind in {"case_started", "case_interrupted", "case_completed"}:
        return "case"
    if kind.startswith("suggestion_"):
        return str(payload.get("suggestion_id", ""))
    if kind in _ARM_KINDS:
        return f"{payload.get('arm', '')}:{payload.get('arm_index', '')}"
    raise _fixed_error("invalid_event_kind")


def _event_object(event: AdvisorTraceEvent) -> dict[str, Any]:
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "fixture_id": event.fixture_id,
        "summary_digest": event.summary_digest,
        "benchmark_run_id": event.benchmark_run_id,
        "kind": event.kind,
        "ts": event.ts,
        "payload": dict(event.payload),
    }


def _make_event(*, fixture_id: str, summary_digest: str, benchmark_run_id: str,
                kind: str, identity: str, payload: Mapping[str, Any],
                ts: float) -> AdvisorTraceEvent:
    payload_copy = _validate_payload(kind, payload)
    expected_identity = _identity_from_payload(kind, payload_copy)
    if identity != expected_identity:
        raise _fixed_error("identity_mismatch")
    event_id = _stable_digest("m8-event", [
        fixture_id, summary_digest, benchmark_run_id, kind, identity,
    ])
    return AdvisorTraceEvent(
        schema_version=_SCHEMA_VERSION,
        event_id=event_id,
        fixture_id=str(fixture_id),
        summary_digest=str(summary_digest),
        benchmark_run_id=str(benchmark_run_id),
        kind=kind,
        ts=_finite_number(ts, "event_ts"),
        payload=MappingProxyType(payload_copy),
    )


def _event_from_object(value: Any) -> AdvisorTraceEvent:
    if not isinstance(value, Mapping):
        raise _fixed_error("invalid_event")
    required = {
        "schema_version", "event_id", "fixture_id", "summary_digest",
        "benchmark_run_id", "kind", "ts", "payload",
    }
    if set(value) != required or value.get("schema_version") != _SCHEMA_VERSION:
        raise _fixed_error("invalid_event")
    kind = str(value["kind"])
    payload = _validate_payload(kind, value["payload"])
    identity = _identity_from_payload(kind, payload)
    expected_id = _stable_digest("m8-event", [
        str(value["fixture_id"]), str(value["summary_digest"]),
        str(value["benchmark_run_id"]), kind, identity,
    ])
    if value["event_id"] != expected_id:
        raise _fixed_error("event_id_mismatch")
    return AdvisorTraceEvent(
        schema_version=_SCHEMA_VERSION,
        event_id=expected_id,
        fixture_id=str(value["fixture_id"]),
        summary_digest=str(value["summary_digest"]),
        benchmark_run_id=str(value["benchmark_run_id"]),
        kind=kind,
        ts=_finite_number(value["ts"], "event_ts"),
        payload=MappingProxyType(payload),
    )


def _trace_digests(events: Sequence[AdvisorTraceEvent]) -> tuple[str, str]:
    terminals: dict[str, Mapping[str, object]] = {}
    available: Sequence[int] = ()
    for event in events:
        if event.kind == "case_started":
            raw_facts = event.payload.get("available_fact_seqs", [])
            if isinstance(raw_facts, list):
                available = tuple(int(item) for item in raw_facts)
        elif event.kind in {
            "baseline_completed", "baseline_failed",
            "advisor_completed", "advisor_failed",
        }:
            terminals[event.kind.split("_", 1)[0]] = event.payload
    digest_input = {
        arm: dict(terminals[arm]) for arm in ("baseline", "advisor")
        if arm in terminals
    }
    trace_digest = _stable_digest("m8-trace-result", digest_input)
    comparison: Any = None
    baseline = terminals.get("baseline")
    advisor = terminals.get("advisor")
    if baseline is not None and advisor is not None:
        baseline_trace = baseline.get("safe_reason_trace")
        advisor_trace = advisor.get("safe_reason_trace")
        if baseline_trace is not None and advisor_trace is not None:
            compared = compare_intent_traces(
                reason_trace_from_payload(baseline_trace),
                reason_trace_from_payload(advisor_trace),
                available_fact_seqs=available,
            )
            comparison = {
                "baseline_count": compared.baseline_count,
                "advisor_count": compared.advisor_count,
                "overlap_count": compared.overlap_count,
                "baseline_duplicate_count": compared.baseline_duplicate_count,
                "advisor_duplicate_count": compared.advisor_duplicate_count,
                "baseline_unsupported_citation_count": (
                    compared.baseline_unsupported_citation_count
                ),
                "advisor_unsupported_citation_count": (
                    compared.advisor_unsupported_citation_count
                ),
                "advisor_only_intent_indexes": list(
                    compared.advisor_only_intent_indexes
                ),
                "baseline_only_intent_indexes": list(
                    compared.baseline_only_intent_indexes
                ),
                "jaccard": compared.jaccard,
            }
    return trace_digest, _stable_digest("m8-comparison", comparison)


def _empty_fold(reason: str) -> AdvisorTraceFold:
    return AdvisorTraceFold(
        fixture_id="", summary_digest="", benchmark_run_id="",
        dataset_status="incomplete", complete=False, reasons=(reason,), events=(),
        case_started=None, suggestion_created=None, suggestion_terminal=None,
        baseline_started=None, baseline_terminal=None, advisor_started=None,
        advisor_terminal=None, case_interrupted=None, case_completed=None,
    )


def _add_reason(target: list[str], reason: str) -> None:
    if reason not in target:
        target.append(reason)


def _fold_events(events: Sequence[AdvisorTraceEvent], *, partial_tail: bool = False,
                 parse_corrupt: bool = False) -> AdvisorTraceFold:
    incomplete: list[str] = []
    corrupt: list[str] = []
    if partial_tail:
        _add_reason(incomplete, "partial_tail")
    if parse_corrupt:
        _add_reason(corrupt, "malformed_line")

    case_started = None
    suggestion_created = None
    suggestion_terminal = None
    baseline_started = None
    baseline_terminal = None
    advisor_started = None
    advisor_terminal = None
    case_interrupted = None
    case_completed = None

    fixture_id = events[0].fixture_id if events else ""
    summary_digest = events[0].summary_digest if events else ""
    benchmark_run_id = events[0].benchmark_run_id if events else ""
    seen_ids: dict[str, bytes] = {}
    for index, event in enumerate(events):
        encoded = _canonical_json(_event_object(event))
        prior = seen_ids.get(event.event_id)
        if prior is not None:
            _add_reason(corrupt, "duplicate_event")
            if prior != encoded:
                _add_reason(corrupt, "conflicting_duplicate")
        else:
            seen_ids[event.event_id] = encoded
        if (event.fixture_id != fixture_id or event.summary_digest != summary_digest
                or event.benchmark_run_id != benchmark_run_id):
            _add_reason(corrupt, "identity_mismatch")
        if case_completed is not None:
            _add_reason(corrupt, "event_after_completion")
        if event.kind == "case_started":
            if case_started is not None or index != 0:
                _add_reason(corrupt, "duplicate_case_started")
            else:
                case_started = event
        elif event.kind == "suggestion_created":
            if suggestion_created is not None:
                _add_reason(corrupt, "duplicate_suggestion_created")
            suggestion_created = suggestion_created or event
        elif event.kind in {"suggestion_consumed", "suggestion_rejected"}:
            if suggestion_terminal is not None:
                _add_reason(corrupt, "duplicate_suggestion_terminal")
            suggestion_terminal = suggestion_terminal or event
        elif event.kind == "baseline_started":
            if baseline_started is not None:
                _add_reason(corrupt, "duplicate_arm_start")
            baseline_started = baseline_started or event
        elif event.kind in {"baseline_completed", "baseline_failed"}:
            if baseline_terminal is not None:
                _add_reason(corrupt, "duplicate_arm_terminal")
            baseline_terminal = baseline_terminal or event
        elif event.kind == "advisor_started":
            if advisor_started is not None:
                _add_reason(corrupt, "duplicate_arm_start")
            advisor_started = advisor_started or event
        elif event.kind in {"advisor_completed", "advisor_failed"}:
            if advisor_terminal is not None:
                _add_reason(corrupt, "duplicate_arm_terminal")
            advisor_terminal = advisor_terminal or event
        elif event.kind == "case_interrupted":
            if case_interrupted is not None:
                _add_reason(corrupt, "duplicate_case_interrupted")
            case_interrupted = case_interrupted or event
        elif event.kind == "case_completed":
            if case_completed is not None:
                _add_reason(corrupt, "duplicate_case_completed")
            case_completed = case_completed or event

    if case_started is None:
        _add_reason(incomplete, "missing_case_started")
    if baseline_terminal is not None and baseline_started is None:
        _add_reason(corrupt, "terminal_without_start")
    if advisor_terminal is not None and advisor_started is None:
        _add_reason(corrupt, "terminal_without_start")
    if baseline_started is not None and baseline_terminal is None:
        _add_reason(incomplete, "orphan_arm_start")
    if advisor_started is not None and advisor_terminal is None:
        _add_reason(incomplete, "orphan_arm_start")
    if case_interrupted is not None:
        _add_reason(incomplete, "interrupted_case")
    if case_completed is None:
        _add_reason(incomplete, "missing_case_completed")
    elif events and events[-1] is not case_completed:
        _add_reason(corrupt, "case_completed_not_last")

    if case_started is not None:
        payload = case_started.payload
        eligible = payload.get("eligible") is True
        order = list(payload.get("arm_order", []))
        arm_starts = {"baseline": baseline_started, "advisor": advisor_started}
        arm_terminals = {"baseline": baseline_terminal, "advisor": advisor_terminal}
        if eligible:
            if suggestion_created is None:
                _add_reason(incomplete, "missing_suggestion_created")
            if suggestion_created is not None and suggestion_terminal is None:
                _add_reason(incomplete, "missing_suggestion_terminal")
            if set(order) != {"baseline", "advisor"} or len(order) != 2:
                _add_reason(corrupt, "invalid_arm_order")
        else:
            if order != ["baseline"]:
                _add_reason(corrupt, "invalid_arm_order")
            if suggestion_created is not None or suggestion_terminal is not None:
                _add_reason(corrupt, "ineligible_suggestion_lifecycle")
            if advisor_started is not None or advisor_terminal is not None:
                _add_reason(corrupt, "ineligible_advisor_arm")
        for arm_index, arm in enumerate(order):
            start = arm_starts.get(arm)
            terminal = arm_terminals.get(arm)
            if start is None:
                _add_reason(incomplete, f"missing_{arm}_start")
                continue
            if start.payload.get("arm_index") != arm_index:
                _add_reason(corrupt, "arm_index_mismatch")
            if terminal is None:
                continue
            if terminal.payload.get("arm_index") != arm_index:
                _add_reason(corrupt, "arm_index_mismatch")
            start_pos = events.index(start)
            terminal_pos = events.index(terminal)
            if start_pos >= terminal_pos:
                _add_reason(corrupt, "invalid_arm_sequence")
            if arm_index and arm_terminals.get(order[arm_index - 1]) is not None:
                if events.index(arm_terminals[order[arm_index - 1]]) >= start_pos:
                    _add_reason(corrupt, "overlapping_arms")
        if suggestion_created is not None and suggestion_terminal is not None:
            if suggestion_created.payload.get("suggestion_id") != suggestion_terminal.payload.get("suggestion_id"):
                _add_reason(corrupt, "suggestion_identity_mismatch")
            if events.index(suggestion_created) >= events.index(suggestion_terminal):
                _add_reason(corrupt, "invalid_suggestion_sequence")
            if advisor_started is not None and suggestion_terminal.kind == "suggestion_consumed":
                if events.index(advisor_started) >= events.index(suggestion_terminal):
                    _add_reason(corrupt, "invalid_suggestion_sequence")
        if suggestion_created is not None:
            starts = [item for item in (baseline_started, advisor_started) if item is not None]
            if starts and events.index(suggestion_created) >= min(events.index(item) for item in starts):
                _add_reason(corrupt, "invalid_suggestion_sequence")
        if advisor_terminal is not None:
            admitted = suggestion_terminal is not None and suggestion_terminal.kind == "suggestion_consumed"
            pre_submit_failure = (
                advisor_terminal.kind == "advisor_failed"
                and advisor_terminal.payload.get("failure_stage") == "pre_submit"
                and advisor_terminal.payload.get("call_outcome") == "setup_error"
            )
            rejected = suggestion_terminal is not None and suggestion_terminal.kind == "suggestion_rejected"
            if admitted:
                if events.index(suggestion_terminal) >= events.index(advisor_terminal):
                    _add_reason(corrupt, "invalid_suggestion_sequence")
            elif not (pre_submit_failure and rejected):
                _add_reason(corrupt, "advisor_terminal_without_admission")

    if case_completed is not None:
        if (case_completed.payload.get("fixture_id") != fixture_id
                or case_completed.payload.get("summary_digest") != summary_digest
                or case_completed.payload.get("benchmark_run_id") != benchmark_run_id):
            _add_reason(corrupt, "identity_mismatch")
        expected_trace, expected_comparison = _trace_digests(
            [event for event in events if event.kind != "case_completed"]
        )
        if (case_completed.payload.get("trace_result_digest") != expected_trace
                or case_completed.payload.get("comparison_digest") != expected_comparison):
            _add_reason(corrupt, "digest_mismatch")
        pre_status = "incomplete" if incomplete else "clean"
        if case_completed.payload.get("terminal_status") != pre_status:
            _add_reason(corrupt, "terminal_status_mismatch")

    status: Literal["clean", "incomplete", "corrupt"]
    if corrupt:
        status = "corrupt"
    elif incomplete:
        status = "incomplete"
    else:
        status = "clean"
    reasons = tuple(corrupt + incomplete)
    return AdvisorTraceFold(
        fixture_id=fixture_id, summary_digest=summary_digest,
        benchmark_run_id=benchmark_run_id, dataset_status=status,
        complete=status == "clean", reasons=reasons, events=tuple(events),
        case_started=case_started, suggestion_created=suggestion_created,
        suggestion_terminal=suggestion_terminal, baseline_started=baseline_started,
        baseline_terminal=baseline_terminal, advisor_started=advisor_started,
        advisor_terminal=advisor_terminal, case_interrupted=case_interrupted,
        case_completed=case_completed,
    )


def fold_advisor_trace(case_root: str | os.PathLike[str]) -> AdvisorTraceFold:
    """Read and fold a trace without creating or modifying any local artifact."""

    path = advisor_trace_path(case_root)
    if not path.exists():
        return _empty_fold("missing_trace")
    try:
        data = path.read_bytes()
    except OSError:
        return _empty_fold("trace_read_failed")
    if not data:
        return _empty_fold("missing_trace")

    partial_tail = not data.endswith(b"\n")
    chunks = data.split(b"\n")
    complete_lines = chunks[:-1]
    events: list[AdvisorTraceEvent] = []
    parse_corrupt = False
    for raw in complete_lines:
        if not raw:
            parse_corrupt = True
            continue
        try:
            decoded = json.loads(raw.decode("utf-8"))
            events.append(_event_from_object(decoded))
        except (UnicodeDecodeError, json.JSONDecodeError, AdvisorTraceCorrupt):
            parse_corrupt = True
    return _fold_events(
        events, partial_tail=partial_tail, parse_corrupt=parse_corrupt
    )


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


class AdvisorTraceSink:
    """Single-process, single-writer durable append sink for one M8 case."""

    def __init__(self, case_root: str | os.PathLike[str], *, fixture_id: str,
                 summary_digest: str, benchmark_run_id: str) -> None:
        self.path = advisor_trace_path(case_root)
        self.lock_path = self.path.parent / "advisor-experiment.writer.lock"
        self.fixture_id = str(fixture_id)
        self.summary_digest = str(summary_digest)
        self.benchmark_run_id = str(benchmark_run_id)
        self.owner = str(uuid4())
        self._closed = False
        self._events: list[AdvisorTraceEvent] = []
        self._encoded_by_id: dict[str, bytes] = {}
        self._process_lock = _process_lock(self.lock_path)
        if not self._process_lock.acquire(blocking=False):
            raise AdvisorWriterBusy("writer_busy")
        owns_file_lock = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(
                    self.lock_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                )
            except FileExistsError as exc:
                raise AdvisorWriterBusy("writer_busy") from exc
            owns_file_lock = True
            with os.fdopen(fd, "wb") as handle:
                lock_payload = _canonical_json({
                    "owner": self.owner,
                    "pid": os.getpid(),
                }) + b"\n"
                handle.write(lock_payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists() and self.path.stat().st_size:
                raise AdvisorTraceAlreadyExists("trace_already_exists")
        except BaseException:
            if owns_file_lock:
                self._remove_owned_lock()
            self._process_lock.release()
            raise

    def __enter__(self) -> "AdvisorTraceSink":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _remove_owned_lock(self) -> None:
        try:
            value = json.loads(self.lock_path.read_text(encoding="utf-8"))
            if value.get("owner") == self.owner:
                self.lock_path.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError):
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_owned_lock()
        self._process_lock.release()

    def _validate_binding(self, event: AdvisorTraceEvent) -> None:
        if (event.fixture_id != self.fixture_id
                or event.summary_digest != self.summary_digest
                or event.benchmark_run_id != self.benchmark_run_id):
            raise _fixed_error("identity_mismatch")
        for key, expected in (
            ("fixture_id", self.fixture_id),
            ("summary_digest", self.summary_digest),
            ("benchmark_run_id", self.benchmark_run_id),
        ):
            if key in event.payload and event.payload[key] != expected:
                raise _fixed_error("identity_mismatch")

    def _validate_live_transition(self, event: AdvisorTraceEvent) -> None:
        if any(item.kind == "case_completed" for item in self._events):
            raise _fixed_error("event_after_completion")
        if event.kind in {"suggestion_consumed", "suggestion_rejected"}:
            opposite = (
                "suggestion_rejected" if event.kind == "suggestion_consumed"
                else "suggestion_consumed"
            )
            if any(item.kind == opposite for item in self._events):
                raise _fixed_error("duplicate_suggestion_terminal")
        if event.kind in {"baseline_completed", "baseline_failed", "advisor_completed", "advisor_failed"}:
            arm = event.kind.split("_", 1)[0]
            if any(
                item.kind in {f"{arm}_completed", f"{arm}_failed"}
                for item in self._events
            ):
                raise _fixed_error("duplicate_arm_terminal")

    def append(self, *, kind: str, identity: str,
               payload: Mapping[str, Any], ts: float) -> AdvisorTraceEvent:
        if self._closed:
            raise AdvisorTraceError("sink_closed")
        event = _make_event(
            fixture_id=self.fixture_id,
            summary_digest=self.summary_digest,
            benchmark_run_id=self.benchmark_run_id,
            kind=kind,
            identity=identity,
            payload=payload,
            ts=ts,
        )
        self._validate_binding(event)
        encoded = _canonical_json(_event_object(event))
        prior = self._encoded_by_id.get(event.event_id)
        if prior is not None:
            if prior == encoded:
                return next(
                    item for item in self._events if item.event_id == event.event_id
                )
            raise _fixed_error("conflicting_duplicate")
        self._validate_live_transition(event)
        with self.path.open("ab") as handle:
            handle.write(encoded + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)
        self._encoded_by_id[event.event_id] = encoded
        return event

    def current_digests(self) -> tuple[str, str]:
        return _trace_digests(self._events)


__all__ = [
    "AdvisorTraceAlreadyExists", "AdvisorTraceCorrupt", "AdvisorTraceError",
    "AdvisorTraceEvent", "AdvisorTraceFold", "AdvisorTraceSink",
    "AdvisorWriterBusy", "advisor_trace_path", "fold_advisor_trace",
    "reason_trace_from_payload", "reason_trace_payload",
]
