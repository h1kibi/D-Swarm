"""Conservative, read-only reports for the offline M8 Advisor experiment.

Hidden reference objectives are consulted only while constructing an in-memory
case estimate.  They are never copied into the sidecar or serialized report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import blake2b
import json
import math
from pathlib import Path
import random
import re
import unicodedata
from typing import Any, Literal, Mapping, Sequence

from dswarm.swarm.advisor_experiment import (
    AdvisorFixture,
    AdvisorIntentTrace,
    AdvisorReasonTrace,
    AdvisorReferenceObjective,
    assess_suggestion,
    compare_intent_traces,
    flag_scout_trigger,
)
from dswarm.swarm.advisor_sidecar import (
    AdvisorTraceCorrupt,
    AdvisorTraceEvent,
    advisor_trace_path,
    fold_advisor_trace,
    reason_trace_from_payload,
)

NA = "N/A"
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_BOOTSTRAP_SEED = 20260816
_BOOTSTRAP_RESAMPLES = 2000
_MISSING_TRACE_FAILURE_CODES = {
    "sidecar_append_failed", "sidecar_init_failed", "writer_busy",
    "trace_already_exists", "case_factory_failed", "planner_factory_failed",
    "missing_trace", "trace_read_failed",
    "sidecar_unavailable", "advisor_writer_busy",
    "existing_trace_incomplete", "existing_trace_corrupt",
    "existing_trace_partial", "existing_trace_identity_mismatch",
}


@dataclass(frozen=True, kw_only=True)
class AdvisorCaseEstimate:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    source_event_seq: int
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    trigger_eligible: bool
    trace_only: bool
    eligible_for_quality: bool
    exclusion_reasons: tuple[str, ...]
    assessment_verdict: str
    assessment_reason: str
    baseline_reference_coverage: float | str
    advisor_reference_coverage: float | str
    reference_coverage_delta: float | str
    baseline_intent_count: int | str
    advisor_intent_count: int | str
    intent_jaccard: float | str
    advisor_first_reference_count: int | str
    baseline_only_reference_count: int | str
    baseline_duplicate_count: int | str
    advisor_duplicate_count: int | str
    baseline_unsupported_citation_count: int | str
    advisor_unsupported_citation_count: int | str
    wall_seconds_delta: float | str
    input_tokens_delta: int | str
    output_tokens_delta: int | str
    usd_delta: float | str


@dataclass(frozen=True, kw_only=True)
class AdvisorAggregateReport:
    kind: Literal["m8_offline_next_cycle_planning_estimate"]
    total_cases: int
    total_run_count: int
    clean_cases: int
    incomplete_cases: int
    corrupt_cases: int
    trigger_ineligible_cases: int
    trace_only_cases: int
    planner_unsuccessful_cases: int
    quality_eligible_cases: int
    quality_eligible_run_count: int
    accepted_reference_gain_cases: int
    accepted_reference_gain_denominator_cases: int
    wall_pair_cases: int
    wall_pair_run_count: int
    input_tokens_measured_pair_cases: int
    input_tokens_measured_pair_run_count: int
    output_tokens_measured_pair_cases: int
    output_tokens_measured_pair_run_count: int
    usd_measured_pair_cases: int
    usd_measured_pair_run_count: int
    evidence_tier: Literal["insufficient", "exploratory", "reportable"]
    mean_reference_coverage_delta: float | str
    reference_coverage_delta_ci95_low: float | str
    reference_coverage_delta_ci95_high: float | str
    accepted_reference_gain_rate: float | str
    mean_wall_seconds_delta: float | str
    mean_input_tokens_delta: float | str
    mean_output_tokens_delta: float | str
    mean_usd_delta: float | str
    real_flag_latency_improvement: Literal["N/A"]
    solve_rate_delta: Literal["N/A"]
    worker_starts_saved: Literal["N/A"]
    tokens_saved: Literal["N/A"]
    actual_focused_dispatch_latency: Literal["N/A"]
    race_outcome: Literal["N/A"]
    production_pause_stop_budget_correctness: Literal["N/A"]
    ooda_wakeup_latency: Literal["N/A"]
    cases: tuple[AdvisorCaseEstimate, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(domain: str, value: Any) -> str:
    value_hash = blake2b(_canonical_json([domain, value]), digest_size=16).hexdigest()
    return f"{domain}::{value_hash}"


def _goal_fingerprint(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    tokens = tuple(sorted(set(_WORD_RE.findall(text))))
    return _digest("m8-goal-fingerprint", tokens) if tokens else ""


def _route_fingerprint(value: str) -> str:
    route = unicodedata.normalize("NFKC", str(value)).strip().lower()
    if not route:
        return ""
    if ("\n" in route or "\r" in route
            or any(unicodedata.category(char) == "Cc" for char in route)
            or len(route.encode("utf-8")) > 256
            or _TOKEN_RE.fullmatch(route) is None):
        return ""
    return _digest("m8-route-fingerprint", route)


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _all_na_estimate(
    fixture: AdvisorFixture, *, dataset_status: Literal["incomplete", "corrupt"],
    trigger_eligible: bool, trace_only: bool, reasons: Sequence[str],
) -> AdvisorCaseEstimate:
    return AdvisorCaseEstimate(
        fixture_id=fixture.fixture_id,
        summary_digest=fixture.summary_digest,
        benchmark_run_id=fixture.benchmark_run_id,
        challenge_id=fixture.challenge_id,
        source_event_seq=fixture.source_event_seq,
        dataset_status=dataset_status,
        trigger_eligible=trigger_eligible,
        trace_only=trace_only,
        eligible_for_quality=False,
        exclusion_reasons=tuple(dict.fromkeys(str(item) for item in reasons if item)),
        assessment_verdict=NA,
        assessment_reason=NA,
        baseline_reference_coverage=NA,
        advisor_reference_coverage=NA,
        reference_coverage_delta=NA,
        baseline_intent_count=NA,
        advisor_intent_count=NA,
        intent_jaccard=NA,
        advisor_first_reference_count=NA,
        baseline_only_reference_count=NA,
        baseline_duplicate_count=NA,
        advisor_duplicate_count=NA,
        baseline_unsupported_citation_count=NA,
        advisor_unsupported_citation_count=NA,
        wall_seconds_delta=NA,
        input_tokens_delta=NA,
        output_tokens_delta=NA,
        usd_delta=NA,
    )


def build_missing_trace_estimate(
    fixture: AdvisorFixture, failure_code: str,
) -> AdvisorCaseEstimate:
    """Represent a fixed-code case-local failure without reading references."""

    code = str(failure_code)
    if code not in _MISSING_TRACE_FAILURE_CODES:
        raise ValueError("invalid missing trace failure code")
    trigger = flag_scout_trigger(fixture)
    return _all_na_estimate(
        fixture,
        dataset_status="incomplete",
        trigger_eligible=trigger.eligible,
        trace_only=False,
        reasons=(code,),
    )


def _completed_trace(event: AdvisorTraceEvent | None) -> AdvisorReasonTrace | None:
    if event is None or not event.kind.endswith("_completed"):
        return None
    return reason_trace_from_payload(event.payload.get("safe_reason_trace"))


def _successful(event: AdvisorTraceEvent | None) -> bool:
    return bool(event is not None and event.kind.endswith("_completed")
                and event.payload.get("call_outcome") == "succeeded")


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _wall_delta(baseline: AdvisorTraceEvent | None,
                advisor: AdvisorTraceEvent | None) -> float | str:
    if not _successful(baseline) or not _successful(advisor):
        return NA
    left = _finite_nonnegative(baseline.payload.get("wall_seconds"))
    right = _finite_nonnegative(advisor.payload.get("wall_seconds"))
    return right - left if left is not None and right is not None else NA


def _usage_delta(baseline: AdvisorTraceEvent | None,
                 advisor: AdvisorTraceEvent | None, field: str) -> int | float | str:
    if not _successful(baseline) or not _successful(advisor):
        return NA
    left_usage = baseline.payload.get("usage")
    right_usage = advisor.payload.get("usage")
    if not isinstance(left_usage, Mapping) or not isinstance(right_usage, Mapping):
        return NA
    if (left_usage.get("usage_status") != "measured"
            or right_usage.get("usage_status") != "measured"):
        return NA
    left = left_usage.get(field)
    right = right_usage.get(field)
    if field in {"input_tokens", "output_tokens"}:
        if type(left) is not int or type(right) is not int or left < 0 or right < 0:
            return NA
        return right - left
    left_number = _finite_nonnegative(left)
    right_number = _finite_nonnegative(right)
    return right_number - left_number if left_number is not None and right_number is not None else NA


def _valid_references(
    references: Sequence[AdvisorReferenceObjective],
) -> tuple[tuple[str, str, str], ...]:
    candidates = tuple(
        reference for reference in references
        if isinstance(reference, AdvisorReferenceObjective)
    )
    id_counts: dict[str, int] = {}
    for reference in candidates:
        objective_id = unicodedata.normalize(
            "NFKC", str(reference.objective_id)
        ).strip()
        if objective_id:
            id_counts[objective_id] = id_counts.get(objective_id, 0) + 1
    valid: list[tuple[str, str, str]] = []
    for reference in candidates:
        objective_id = unicodedata.normalize(
            "NFKC", str(reference.objective_id)
        ).strip()
        if not objective_id or id_counts.get(objective_id) != 1:
            continue
        route = _route_fingerprint(reference.route_hash)
        goal = _goal_fingerprint(reference.goal)
        if not route and not goal:
            continue
        # The objective id remains an in-memory identity only.
        valid.append((objective_id, route, goal))
    return tuple(valid)


def _covers(intent: AdvisorIntentTrace, reference: tuple[str, str, str]) -> bool:
    _, route, goal = reference
    return bool((route and intent.route_fingerprint == route)
                or (goal and intent.goal_fingerprint == goal))


def _covered_indexes(
    trace: AdvisorReasonTrace, references: Sequence[tuple[str, str, str]],
) -> set[int]:
    return {
        index for index, reference in enumerate(references)
        if any(_covers(intent, reference) for intent in trace.intents)
    }


def _identity_reasons(fixture: AdvisorFixture, fold: Any) -> tuple[str, ...]:
    reasons: list[str] = []
    start = fold.case_started
    trigger = flag_scout_trigger(fixture)
    if not fold.events:
        return ()
    if (fold.fixture_id != fixture.fixture_id
            or fold.summary_digest != fixture.summary_digest
            or fold.benchmark_run_id != fixture.benchmark_run_id):
        _add_reason(reasons, "identity_mismatch")
    if start is None:
        return tuple(reasons)
    payload = start.payload
    checks = (
        (payload.get("fixture_id"), fixture.fixture_id),
        (payload.get("summary_digest"), fixture.summary_digest),
        (payload.get("benchmark_run_id"), fixture.benchmark_run_id),
        (payload.get("challenge_id"), fixture.challenge_id),
        (payload.get("source_event_seq"), fixture.source_event_seq),
        (payload.get("eligible"), trigger.eligible),
        (payload.get("trigger_reason"), trigger.reason),
        (payload.get("available_fact_seqs", []), list(fixture.available_fact_seqs)),
    )
    if any(actual != expected for actual, expected in checks):
        _add_reason(reasons, "identity_mismatch")
    return tuple(reasons)


def build_case_estimate(
    fixture: AdvisorFixture, trace_path: str | Path,
) -> AdvisorCaseEstimate:
    """Rebuild one case from safe sidecar fields without modifying the trace."""

    if not isinstance(fixture, AdvisorFixture):
        raise ValueError("invalid fixture")
    root = Path(trace_path)
    if root.name == advisor_trace_path(".").name:
        root = root.parent.parent
    fold = fold_advisor_trace(root)
    trigger = flag_scout_trigger(fixture)
    identity_reasons = _identity_reasons(fixture, fold)
    if identity_reasons:
        return _all_na_estimate(
            fixture, dataset_status="corrupt",
            trigger_eligible=trigger.eligible, trace_only=False,
            reasons=tuple(fold.reasons) + identity_reasons,
        )
    if fold.dataset_status != "clean" or not fold.complete:
        return _all_na_estimate(
            fixture, dataset_status=fold.dataset_status,
            trigger_eligible=trigger.eligible, trace_only=False,
            reasons=fold.reasons,
        )
    if not trigger.eligible:
        return replace(
            _all_na_estimate(
                fixture, dataset_status="incomplete", trigger_eligible=False,
                trace_only=False, reasons=("trigger_ineligible",),
            ),
            dataset_status="clean",
        )

    baseline_event = fold.baseline_terminal
    advisor_event = fold.advisor_terminal
    baseline_success = _successful(baseline_event)
    advisor_success = _successful(advisor_event)
    wall_delta = _wall_delta(baseline_event, advisor_event)
    input_delta = _usage_delta(baseline_event, advisor_event, "input_tokens")
    output_delta = _usage_delta(baseline_event, advisor_event, "output_tokens")
    usd_delta = _usage_delta(baseline_event, advisor_event, "usd")

    try:
        baseline_trace = _completed_trace(baseline_event)
        advisor_trace = _completed_trace(advisor_event)
    except AdvisorTraceCorrupt:
        return _all_na_estimate(
            fixture, dataset_status="corrupt",
            trigger_eligible=True, trace_only=False,
            reasons=("invalid_safe_reason_trace",),
        )

    references = _valid_references(fixture.reference_objectives)
    trace_only = not references
    if trace_only:
        estimate = _all_na_estimate(
            fixture, dataset_status="incomplete", trigger_eligible=True,
            trace_only=True, reasons=("missing_reference_objectives",),
        )
        return replace(
            estimate,
            dataset_status="clean",
            wall_seconds_delta=wall_delta,
            input_tokens_delta=input_delta,
            output_tokens_delta=output_delta,
            usd_delta=usd_delta,
        )

    if not baseline_success or not advisor_success or baseline_trace is None or advisor_trace is None:
        assessment = assess_suggestion(
            baseline_success=baseline_success, advisor_success=advisor_success,
            advisor_intent_count=(len(advisor_trace.intents) if advisor_trace else 0),
            gained_count=0, lost_count=0, supported_gain=False,
            planning_delta=False,
        )
        estimate = _all_na_estimate(
            fixture, dataset_status="incomplete", trigger_eligible=True,
            trace_only=False, reasons=("planner_arm_not_successful",),
        )
        return replace(
            estimate,
            dataset_status="clean",
            assessment_verdict=assessment.verdict,
            assessment_reason=assessment.reason,
            wall_seconds_delta=wall_delta,
            input_tokens_delta=input_delta,
            output_tokens_delta=output_delta,
            usd_delta=usd_delta,
        )

    comparison = compare_intent_traces(
        baseline_trace, advisor_trace,
        available_fact_seqs=fixture.available_fact_seqs,
    )
    baseline_covered = _covered_indexes(baseline_trace, references)
    advisor_covered = _covered_indexes(advisor_trace, references)
    gained = advisor_covered - baseline_covered
    lost = baseline_covered - advisor_covered
    available = set(fixture.available_fact_seqs)
    supported_gain = any(
        any(
            _covers(advisor_trace.intents[intent_index], references[ref_index])
            and bool(advisor_trace.intents[intent_index].from_facts)
            and all(seq in available for seq in advisor_trace.intents[intent_index].from_facts)
            for intent_index in comparison.advisor_only_intent_indexes
        )
        for ref_index in gained
    )
    assessment = assess_suggestion(
        baseline_success=True, advisor_success=True,
        advisor_intent_count=len(advisor_trace.intents),
        gained_count=len(gained), lost_count=len(lost),
        supported_gain=supported_gain,
        planning_delta=baseline_trace.intents != advisor_trace.intents,
    )
    reference_count = len(references)
    return AdvisorCaseEstimate(
        fixture_id=fixture.fixture_id, summary_digest=fixture.summary_digest,
        benchmark_run_id=fixture.benchmark_run_id,
        challenge_id=fixture.challenge_id,
        source_event_seq=fixture.source_event_seq,
        dataset_status="clean", trigger_eligible=True, trace_only=False,
        eligible_for_quality=True, exclusion_reasons=(),
        assessment_verdict=assessment.verdict,
        assessment_reason=assessment.reason,
        baseline_reference_coverage=len(baseline_covered) / reference_count,
        advisor_reference_coverage=len(advisor_covered) / reference_count,
        reference_coverage_delta=(len(advisor_covered) - len(baseline_covered)) / reference_count,
        baseline_intent_count=comparison.baseline_count,
        advisor_intent_count=comparison.advisor_count,
        intent_jaccard=comparison.jaccard,
        advisor_first_reference_count=len(gained),
        baseline_only_reference_count=len(lost),
        baseline_duplicate_count=comparison.baseline_duplicate_count,
        advisor_duplicate_count=comparison.advisor_duplicate_count,
        baseline_unsupported_citation_count=comparison.baseline_unsupported_citation_count,
        advisor_unsupported_citation_count=comparison.advisor_unsupported_citation_count,
        wall_seconds_delta=wall_delta,
        input_tokens_delta=input_delta, output_tokens_delta=output_delta,
        usd_delta=usd_delta,
    )


def _is_number(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def _run_means(cases: Sequence[AdvisorCaseEstimate], field: str) -> tuple[float, ...]:
    by_run: dict[str, list[float]] = {}
    for case in cases:
        value = getattr(case, field)
        if case.dataset_status == "clean" and _is_number(value):
            by_run.setdefault(case.benchmark_run_id, []).append(float(value))
    return tuple(
        sum(values) / len(values)
        for _, values in sorted(by_run.items())
        if values
    )


def _mean(values: Sequence[float]) -> float | str:
    return sum(values) / len(values) if values else NA


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _bootstrap_ci(values: Sequence[float]) -> tuple[float, float]:
    rng = random.Random(_BOOTSTRAP_SEED)
    count = len(values)
    samples = sorted(
        sum(values[rng.randrange(count)] for _ in range(count)) / count
        for _ in range(_BOOTSTRAP_RESAMPLES)
    )
    return _percentile(samples, 0.025), _percentile(samples, 0.975)


def build_advisor_report(
    cases: Sequence[AdvisorCaseEstimate],
) -> AdvisorAggregateReport:
    ordered = tuple(sorted(cases, key=lambda item: (
        item.benchmark_run_id, item.fixture_id,
    )))
    if any(not isinstance(case, AdvisorCaseEstimate) for case in ordered):
        raise ValueError("invalid advisor case estimate")

    quality = tuple(case for case in ordered if case.eligible_for_quality)
    reference_run_means = _run_means(quality, "reference_coverage_delta")
    wall_run_means = _run_means(ordered, "wall_seconds_delta")
    input_run_means = _run_means(ordered, "input_tokens_delta")
    output_run_means = _run_means(ordered, "output_tokens_delta")
    usd_run_means = _run_means(ordered, "usd_delta")
    run_count = len(reference_run_means)
    evidence_tier: Literal["insufficient", "exploratory", "reportable"]
    if run_count < 5:
        evidence_tier = "insufficient"
    elif run_count < 20:
        evidence_tier = "exploratory"
    else:
        evidence_tier = "reportable"
    ci_low: float | str = NA
    ci_high: float | str = NA
    if evidence_tier == "reportable":
        ci_low, ci_high = _bootstrap_ci(reference_run_means)

    accepted = sum(
        case.assessment_verdict == "accepted_reference_gain" for case in quality
    )
    quality_count = len(quality)
    status_counts = {
        status: sum(case.dataset_status == status for case in ordered)
        for status in ("clean", "incomplete", "corrupt")
    }
    return AdvisorAggregateReport(
        kind="m8_offline_next_cycle_planning_estimate",
        total_cases=len(ordered),
        total_run_count=len({case.benchmark_run_id for case in ordered}),
        clean_cases=status_counts["clean"],
        incomplete_cases=status_counts["incomplete"],
        corrupt_cases=status_counts["corrupt"],
        trigger_ineligible_cases=sum(not case.trigger_eligible for case in ordered),
        trace_only_cases=sum(case.trace_only for case in ordered),
        planner_unsuccessful_cases=sum(
            case.assessment_verdict == "indeterminate_planner_error"
            for case in ordered
        ),
        quality_eligible_cases=quality_count,
        quality_eligible_run_count=run_count,
        accepted_reference_gain_cases=accepted,
        accepted_reference_gain_denominator_cases=quality_count,
        wall_pair_cases=sum(
            case.dataset_status == "clean" and _is_number(case.wall_seconds_delta)
            for case in ordered
        ),
        wall_pair_run_count=len(wall_run_means),
        input_tokens_measured_pair_cases=sum(
            case.dataset_status == "clean" and _is_number(case.input_tokens_delta)
            for case in ordered
        ),
        input_tokens_measured_pair_run_count=len(input_run_means),
        output_tokens_measured_pair_cases=sum(
            case.dataset_status == "clean" and _is_number(case.output_tokens_delta)
            for case in ordered
        ),
        output_tokens_measured_pair_run_count=len(output_run_means),
        usd_measured_pair_cases=sum(
            case.dataset_status == "clean" and _is_number(case.usd_delta)
            for case in ordered
        ),
        usd_measured_pair_run_count=len(usd_run_means),
        evidence_tier=evidence_tier,
        mean_reference_coverage_delta=_mean(reference_run_means),
        reference_coverage_delta_ci95_low=ci_low,
        reference_coverage_delta_ci95_high=ci_high,
        accepted_reference_gain_rate=(accepted / quality_count if quality_count else NA),
        mean_wall_seconds_delta=_mean(wall_run_means),
        mean_input_tokens_delta=_mean(input_run_means),
        mean_output_tokens_delta=_mean(output_run_means),
        mean_usd_delta=_mean(usd_run_means),
        real_flag_latency_improvement=NA,
        solve_rate_delta=NA,
        worker_starts_saved=NA,
        tokens_saved=NA,
        actual_focused_dispatch_latency=NA,
        race_outcome=NA,
        production_pause_stop_budget_correctness=NA,
        ooda_wakeup_latency=NA,
        cases=ordered,
    )


def advisor_report_json(report: AdvisorAggregateReport) -> str:
    if not isinstance(report, AdvisorAggregateReport):
        raise ValueError("invalid advisor report")
    return json.dumps(
        asdict(report), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


__all__ = [
    "NA", "AdvisorAggregateReport", "AdvisorCaseEstimate",
    "advisor_report_json", "build_advisor_report", "build_case_estimate",
    "build_missing_trace_estimate",
]
