"""Safe, offline-only models for the M8 Advisor experiment.

This module deliberately has no production scheduler, graph, event-bus, or gate
imports.  It freezes operator-provided experiment inputs, builds an untrusted
Advisor-only prompt suffix, and converts planner output into an explicit
free-text-free allowlist suitable for local sidecar storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
import json
import math
import re
import unicodedata
from typing import Any, Iterable, Literal, Sequence

from dswarm.solver.reason import Intent, ReasonResult


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_WORKER_CLASSES = {"code", "shell_agent", "verifier", "review"}
_DIRECTIONS = {"", "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"}
_VERDICTS = {"complete", "course_correct", "explore"}


class AdvisorSensitiveOutput(ValueError):
    """A fixed-code validation failure that never echoes rejected content."""

    def __init__(self, code: str, field: str) -> None:
        self.code = str(code)
        self.field = str(field)
        super().__init__(f"{self.code}:{self.field}")


@dataclass(frozen=True, kw_only=True)
class AdvisorReferenceObjective:
    objective_id: str
    route_hash: str = ""
    goal: str = ""


@dataclass(frozen=True, kw_only=True)
class AdvisorFixture:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    challenge_mode: str
    expected_flags: int
    captured_flags_before_source: int
    source_event_seq: int
    source_event_ts: float
    source_kind: Literal["flag_found"]
    source_intent_id: str
    source_route_hash: str
    next_cycle_id: str
    graph_summary: str
    fact_index: str
    available_fact_seqs: tuple[int, ...]
    max_intents: int
    goal: str
    reference_objectives: tuple[AdvisorReferenceObjective, ...]


@dataclass(frozen=True, kw_only=True)
class AdvisorySuggestion:
    suggestion_id: str
    fixture_id: str
    source_event_seq: int
    kind: Literal["flag_scout"]
    source_intent_id: str
    source_route_hash: str
    route_attribution: Literal["explicit", "unattributed"]
    prompt_text: str


@dataclass(frozen=True, kw_only=True)
class SuggestionTrigger:
    eligible: bool
    reason: Literal["eligible", "single_flag_run", "no_remaining_flag_after_source"]
    suggestion: AdvisorySuggestion | None


@dataclass(frozen=True, kw_only=True)
class AdvisorIntentTrace:
    intent_key: str
    goal_fingerprint: str
    route_fingerprint: str
    worker_class: Literal["code", "shell_agent", "verifier", "review"]
    priority: float
    from_facts: tuple[int, ...]
    direction: Literal["", "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"]
    requires_recon: bool
    host_scan: bool


@dataclass(frozen=True, kw_only=True)
class AdvisorReasonTrace:
    goal_met: bool
    verdict: Literal["complete", "course_correct", "explore"]
    intents: tuple[AdvisorIntentTrace, ...]
    audit_note_count: int
    pinned_facts: tuple[int, ...]
    dispatch_count: int


@dataclass(frozen=True, kw_only=True)
class IntentComparison:
    baseline_count: int
    advisor_count: int
    overlap_count: int
    baseline_duplicate_count: int
    advisor_duplicate_count: int
    baseline_unsupported_citation_count: int
    advisor_unsupported_citation_count: int
    advisor_only_intent_indexes: tuple[int, ...]
    baseline_only_intent_indexes: tuple[int, ...]
    jaccard: float


@dataclass(frozen=True, kw_only=True)
class CaseAssessment:
    verdict: Literal[
        "accepted_reference_gain", "unchanged", "mixed", "regressed",
        "rejected_baseline_already_equivalent", "rejected_no_supported_delta",
        "rejected_advisor_empty", "indeterminate_planner_error",
    ]
    reason: Literal[
        "new_supported_reference_without_loss", "no_planning_delta",
        "gain_with_regression", "lost_reference", "baseline_already_covers_target",
        "delta_has_no_supported_citation", "advisor_has_no_intents",
        "planner_arm_not_successful",
    ]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(domain: str, value: Any) -> str:
    digest = blake2b(_canonical_json([domain, value]), digest_size=16).hexdigest()
    return f"{domain}::{digest}"


def _text_digest(domain: str, text: str) -> str:
    return _digest(domain, str(text))


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value))


def _contains_control(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _identity(value: Any, *, field: str, max_bytes: int = 256) -> str:
    text = _nfkc(value).strip()
    if not text or "\n" in text or "\r" in text or _contains_control(text):
        raise ValueError(f"invalid {field}")
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError(f"invalid {field}")
    return text


def _source_token(value: Any, *, field: str, max_bytes: int, lower: bool) -> str:
    text = _nfkc(value).strip()
    if not text:
        return ""
    if lower:
        text = text.lower()
    if ("\n" in text or "\r" in text or _contains_control(text)
            or len(text.encode("utf-8")) > max_bytes
            or _TOKEN_RE.fullmatch(text) is None):
        raise ValueError(f"invalid {field}")
    return text


def _strict_positive_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {field}")
    return value


def _fact_tuple(values: Iterable[Any], *, field: str,
                sensitive: bool = False) -> tuple[int, ...]:
    result: set[int] = set()
    try:
        iterator = iter(values)
    except TypeError:
        if sensitive:
            raise AdvisorSensitiveOutput("invalid_fact_reference", field) from None
        raise ValueError(f"invalid {field}") from None
    for raw in iterator:
        if type(raw) is not int or raw <= 0:
            if sensitive:
                raise AdvisorSensitiveOutput("invalid_fact_reference", field)
            raise ValueError(f"invalid {field}")
        result.add(raw)
    return tuple(sorted(result))


def _goal_tokens(value: Any, *, field: str) -> tuple[str, ...]:
    text = _nfkc(value).casefold().strip()
    tokens = tuple(sorted(set(_WORD_RE.findall(text))))
    if not tokens:
        raise AdvisorSensitiveOutput("invalid_fingerprint_input", field)
    return tokens


def _route_value(value: Any) -> str:
    text = _nfkc(value).strip().lower()
    if not text:
        return ""
    if ("\n" in text or "\r" in text or _contains_control(text)
            or len(text.encode("utf-8")) > 256
            or _TOKEN_RE.fullmatch(text) is None):
        raise AdvisorSensitiveOutput("invalid_fingerprint_input", "route_hash")
    return text


def make_advisor_fixture(
    *,
    benchmark_run_id: str,
    challenge_id: str,
    challenge_mode: str,
    expected_flags: int,
    captured_flags_before_source: int,
    source_event_seq: int,
    source_event_ts: float,
    source_intent_id: str,
    source_route_hash: str,
    next_cycle_id: str,
    graph_summary: str,
    fact_index: str,
    available_fact_seqs: Sequence[int],
    max_intents: int,
    goal: str,
    reference_objectives: Sequence[AdvisorReferenceObjective] = (),
) -> AdvisorFixture:
    """Validate and freeze one next-cycle experiment fixture."""

    run_id = _identity(benchmark_run_id, field="benchmark_run_id")
    challenge = _identity(challenge_id, field="challenge_id")
    mode = _identity(challenge_mode, field="challenge_mode", max_bytes=64).lower()
    cycle = _identity(next_cycle_id, field="next_cycle_id")
    if type(expected_flags) is not int or expected_flags < 1:
        raise ValueError("invalid expected_flags")
    if (type(captured_flags_before_source) is not int
            or captured_flags_before_source < 0
            or captured_flags_before_source >= expected_flags):
        raise ValueError("invalid captured_flags_before_source")
    source_seq = _strict_positive_int(source_event_seq, field="source_event_seq")
    timestamp = float(source_event_ts)
    if not math.isfinite(timestamp):
        raise ValueError("invalid source_event_ts")
    summary = str(graph_summary)
    if not summary:
        raise ValueError("invalid graph_summary")
    if type(max_intents) is not int or max_intents < 1:
        raise ValueError("invalid max_intents")
    intent_id = _source_token(
        source_intent_id, field="source_intent_id", max_bytes=128, lower=False
    )
    route_hash = _source_token(
        source_route_hash, field="source_route_hash", max_bytes=256, lower=True
    )
    facts = _fact_tuple(available_fact_seqs, field="available_fact_seqs")

    refs = tuple(reference_objectives)
    seen_ids: set[str] = set()
    for ref in refs:
        if not isinstance(ref, AdvisorReferenceObjective):
            raise ValueError("invalid reference_objectives")
        objective_id = _identity(ref.objective_id, field="objective_id", max_bytes=128)
        if objective_id in seen_ids:
            raise ValueError("duplicate objective_id")
        seen_ids.add(objective_id)
        has_route = bool(_nfkc(ref.route_hash).strip())
        has_goal = bool(_nfkc(ref.goal).strip())
        if not has_route and not has_goal:
            raise ValueError("empty reference fingerprint")
        if has_route:
            _route_value(ref.route_hash)
        if has_goal:
            _goal_tokens(ref.goal, field="reference_goal")

    summary_digest = _text_digest("m8-summary", summary)
    identity_payload = {
        "benchmark_run_id": run_id,
        "challenge_id": challenge,
        "challenge_mode": mode,
        "expected_flags": expected_flags,
        "captured_flags_before_source": captured_flags_before_source,
        "source_event_seq": source_seq,
        "source_event_ts": timestamp,
        "source_kind": "flag_found",
        "source_intent_id": intent_id,
        "source_route_hash": route_hash,
        "next_cycle_id": cycle,
        "summary_digest": summary_digest,
        "fact_index_digest": _text_digest("m8-fact-index", str(fact_index)),
        "goal_digest": _text_digest("m8-goal", str(goal)),
        "available_fact_seqs": facts,
        "max_intents": max_intents,
    }
    fixture_id = _digest("m8-fixture", identity_payload)
    return AdvisorFixture(
        fixture_id=fixture_id,
        summary_digest=summary_digest,
        benchmark_run_id=run_id,
        challenge_id=challenge,
        challenge_mode=mode,
        expected_flags=expected_flags,
        captured_flags_before_source=captured_flags_before_source,
        source_event_seq=source_seq,
        source_event_ts=timestamp,
        source_kind="flag_found",
        source_intent_id=intent_id,
        source_route_hash=route_hash,
        next_cycle_id=cycle,
        graph_summary=summary,
        fact_index=str(fact_index),
        available_fact_seqs=facts,
        max_intents=max_intents,
        goal=str(goal),
        reference_objectives=refs,
    )


def flag_scout_trigger(fixture: AdvisorFixture) -> SuggestionTrigger:
    """Apply the exact multi-flag trigger without inspecting graph text."""

    if fixture.expected_flags == 1:
        return SuggestionTrigger(
            eligible=False, reason="single_flag_run", suggestion=None
        )
    remaining_after_source = fixture.expected_flags - (
        fixture.captured_flags_before_source + 1
    )
    if remaining_after_source <= 0:
        return SuggestionTrigger(
            eligible=False,
            reason="no_remaining_flag_after_source",
            suggestion=None,
        )
    attribution = "explicit" if fixture.source_route_hash else "unattributed"
    block_payload = {
        "kind": "flag_scout",
        "notice": "untrusted planning suggestion; not evidence",
        "remaining_after_source": remaining_after_source,
        "request": (
            "consider sibling, neighboring, or distinct remaining objectives; "
            "cite only frozen graph facts"
        ),
        "source_event_seq": fixture.source_event_seq,
        "source_intent_id": fixture.source_intent_id or "unattributed",
        "source_route_hash": fixture.source_route_hash or "unattributed",
    }
    prompt_text = "## Open suggestions\n" + json.dumps(
        block_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    suggestion_id = _digest("m8-suggestion", {
        "fixture_id": fixture.fixture_id,
        "source_event_seq": fixture.source_event_seq,
        "source_kind": fixture.source_kind,
        "source_intent_id": fixture.source_intent_id,
        "source_route_hash": fixture.source_route_hash,
    })
    return SuggestionTrigger(
        eligible=True,
        reason="eligible",
        suggestion=AdvisorySuggestion(
            suggestion_id=suggestion_id,
            fixture_id=fixture.fixture_id,
            source_event_seq=fixture.source_event_seq,
            kind="flag_scout",
            source_intent_id=fixture.source_intent_id,
            source_route_hash=fixture.source_route_hash,
            route_attribution=attribution,
            prompt_text=prompt_text,
        ),
    )


def build_experimental_summary(
    fixture: AdvisorFixture, suggestion: AdvisorySuggestion
) -> str:
    if (suggestion.fixture_id != fixture.fixture_id
            or suggestion.source_event_seq != fixture.source_event_seq):
        raise ValueError("suggestion_identity_mismatch")
    return f"{fixture.graph_summary}\n\n{suggestion.prompt_text}"


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item)


def _raw_planner_strings(result: ReasonResult) -> Iterable[str]:
    yield from (
        str(result.drift), str(result.complete_why),
        *(str(note) for note in result.audit_notes),
    )
    yield from _walk_strings(result.dispatches)
    for intent in result.intents:
        yield from (
            str(intent.intent_id), str(intent.goal), str(intent.rationale),
            str(intent.route_hash), str(intent.mode), str(intent.task_kind),
            str(intent.reopen_because), str(intent.surface_target),
        )


def _check_forbidden(result: ReasonResult, forbidden_fragments: Sequence[str]) -> None:
    fragments = tuple(fragment for fragment in forbidden_fragments if fragment)
    if not fragments:
        return
    for raw in _raw_planner_strings(result):
        if any(fragment in raw for fragment in fragments):
            raise AdvisorSensitiveOutput(
                "sensitive_output_redacted", "planner_output"
            )


def _intent_trace(intent: Intent) -> AdvisorIntentTrace:
    worker_class = str(intent.worker_class)
    if worker_class not in _WORKER_CLASSES:
        raise AdvisorSensitiveOutput("invalid_worker_class", "worker_class")
    direction = str(intent.canonical_direction or intent.direction).strip().lower()
    if direction not in _DIRECTIONS:
        raise AdvisorSensitiveOutput("unsafe_direction", "direction")
    try:
        priority = float(intent.priority)
    except (TypeError, ValueError, OverflowError):
        raise AdvisorSensitiveOutput("invalid_priority", "priority") from None
    if not math.isfinite(priority):
        raise AdvisorSensitiveOutput("invalid_priority", "priority")
    facts = _fact_tuple(intent.from_facts, field="from_facts", sensitive=True)
    goal_tokens = _goal_tokens(intent.goal, field="goal")
    route = _route_value(intent.route_hash)
    raw_id = _nfkc(intent.intent_id).strip()
    if not raw_id or _contains_control(raw_id):
        raise AdvisorSensitiveOutput("invalid_fingerprint_input", "intent_id")
    mode = _nfkc(intent.mode).strip().lower()
    task_kind = _nfkc(intent.task_kind).strip().lower()
    return AdvisorIntentTrace(
        intent_key=_digest("m8-intent-key", [raw_id, goal_tokens, route, mode, task_kind]),
        goal_fingerprint=_digest("m8-goal-fingerprint", goal_tokens),
        route_fingerprint=_digest("m8-route-fingerprint", route) if route else "",
        worker_class=worker_class,  # type: ignore[arg-type]
        priority=priority,
        from_facts=facts,
        direction=direction,  # type: ignore[arg-type]
        requires_recon=bool(intent.requires_recon),
        host_scan=bool(intent.host_scan),
    )


def safe_reason_trace(
    result: ReasonResult,
    *,
    available_fact_seqs: Sequence[int],
    forbidden_fragments: Sequence[str] = (),
) -> AdvisorReasonTrace:
    """Convert Reason output field-by-field into a free-text-free trace."""

    if not isinstance(result, ReasonResult):
        raise AdvisorSensitiveOutput("invalid_planner_output", "reason_result")
    _fact_tuple(available_fact_seqs, field="available_fact_seqs", sensitive=True)
    _check_forbidden(result, forbidden_fragments)
    verdict = str(result.verdict).strip().lower()
    if verdict not in _VERDICTS:
        raise AdvisorSensitiveOutput("invalid_verdict", "verdict")
    pins = _fact_tuple(result.pinned_facts, field="pinned_facts", sensitive=True)
    intents = tuple(_intent_trace(intent) for intent in result.intents)
    return AdvisorReasonTrace(
        goal_met=bool(result.goal_met),
        verdict=verdict,  # type: ignore[arg-type]
        intents=intents,
        audit_note_count=len(result.audit_notes),
        pinned_facts=pins,
        dispatch_count=len(result.dispatches),
    )


def intent_trace_equivalent(
    left: AdvisorIntentTrace, right: AdvisorIntentTrace
) -> bool:
    return bool(
        (left.route_fingerprint
         and left.route_fingerprint == right.route_fingerprint)
        or left.goal_fingerprint == right.goal_fingerprint
    )


def _duplicate_count(intents: Sequence[AdvisorIntentTrace]) -> int:
    duplicates = 0
    for index, intent in enumerate(intents):
        if any(intent_trace_equivalent(prior, intent) for prior in intents[:index]):
            duplicates += 1
    return duplicates


def _unsupported_count(
    intents: Sequence[AdvisorIntentTrace], available: set[int]
) -> int:
    return sum(
        1 for intent in intents
        if not intent.from_facts or any(seq not in available for seq in intent.from_facts)
    )


def compare_intent_traces(
    baseline: AdvisorReasonTrace,
    advisor: AdvisorReasonTrace,
    *,
    available_fact_seqs: Sequence[int],
) -> IntentComparison:
    available = set(_fact_tuple(
        available_fact_seqs, field="available_fact_seqs", sensitive=True
    ))
    matched_advisor: set[int] = set()
    matched_baseline: set[int] = set()
    for baseline_index, baseline_intent in enumerate(baseline.intents):
        for advisor_index, advisor_intent in enumerate(advisor.intents):
            if advisor_index in matched_advisor:
                continue
            if intent_trace_equivalent(baseline_intent, advisor_intent):
                matched_baseline.add(baseline_index)
                matched_advisor.add(advisor_index)
                break
    overlap = len(matched_advisor)
    union = len(baseline.intents) + len(advisor.intents) - overlap
    return IntentComparison(
        baseline_count=len(baseline.intents),
        advisor_count=len(advisor.intents),
        overlap_count=overlap,
        baseline_duplicate_count=_duplicate_count(baseline.intents),
        advisor_duplicate_count=_duplicate_count(advisor.intents),
        baseline_unsupported_citation_count=_unsupported_count(
            baseline.intents, available
        ),
        advisor_unsupported_citation_count=_unsupported_count(
            advisor.intents, available
        ),
        advisor_only_intent_indexes=tuple(
            index for index in range(len(advisor.intents))
            if index not in matched_advisor
        ),
        baseline_only_intent_indexes=tuple(
            index for index in range(len(baseline.intents))
            if index not in matched_baseline
        ),
        jaccard=(overlap / union if union else 1.0),
    )


def assess_suggestion(
    *,
    baseline_success: bool,
    advisor_success: bool,
    advisor_intent_count: int,
    gained_count: int,
    lost_count: int,
    supported_gain: bool,
    planning_delta: bool,
) -> CaseAssessment:
    """Apply the frozen reporter assessment precedence to aggregate counts."""

    if not baseline_success or not advisor_success:
        return CaseAssessment(
            verdict="indeterminate_planner_error",
            reason="planner_arm_not_successful",
        )
    if advisor_intent_count == 0:
        return CaseAssessment(
            verdict="rejected_advisor_empty", reason="advisor_has_no_intents"
        )
    if gained_count and lost_count:
        return CaseAssessment(verdict="mixed", reason="gain_with_regression")
    if lost_count:
        return CaseAssessment(verdict="regressed", reason="lost_reference")
    if gained_count:
        if supported_gain:
            return CaseAssessment(
                verdict="accepted_reference_gain",
                reason="new_supported_reference_without_loss",
            )
        return CaseAssessment(
            verdict="rejected_no_supported_delta",
            reason="delta_has_no_supported_citation",
        )
    if not planning_delta:
        return CaseAssessment(verdict="unchanged", reason="no_planning_delta")
    return CaseAssessment(
        verdict="rejected_baseline_already_equivalent",
        reason="baseline_already_covers_target",
    )


__all__ = [
    "AdvisorFixture", "AdvisorIntentTrace", "AdvisorReasonTrace",
    "AdvisorReferenceObjective", "AdvisorSensitiveOutput", "AdvisorySuggestion",
    "CaseAssessment", "IntentComparison", "SuggestionTrigger",
    "assess_suggestion", "build_experimental_summary", "compare_intent_traces",
    "flag_scout_trigger", "intent_trace_equivalent", "make_advisor_fixture",
    "safe_reason_trace",
]
