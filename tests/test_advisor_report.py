from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_experiment import (
    AdvisorReferenceObjective,
    make_advisor_fixture,
)
from dswarm.swarm.advisor_report import (
    NA,
    AdvisorCaseEstimate,
    advisor_report_json,
    build_advisor_report,
    build_case_estimate,
    build_missing_trace_estimate,
)
from dswarm.swarm.advisor_runner import (
    AdvisorPlannerResult,
    AdvisorUsage,
    run_advisor_case,
)


def _fixture(*, run="run-1", reference_route="route-advisor", eligible=True):
    return make_advisor_fixture(
        benchmark_run_id=run, challenge_id=f"challenge-{run}",
        challenge_mode="ctf", expected_flags=3 if eligible else 1,
        captured_flags_before_source=0, source_event_seq=42,
        source_event_ts=1.0, source_intent_id="intent-source",
        source_route_hash="route-source", next_cycle_id="reason-2",
        graph_summary="OPAQUE RAWFLAG{never-report}", fact_index="[10] fact",
        available_fact_seqs=(10,), max_intents=4, goal="find remaining",
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="SECRET-OBJECTIVE", route_hash=reference_route,
            goal="secret hidden route",
        ),),
    )


def _result(route):
    return ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id=f"intent-{route}", goal=f"inspect {route}",
            route_hash=route, worker_class="code", direction="web",
            priority=0.5, from_facts=[10],
        )], audit_notes=[], pinned_facts=[10], dispatches=[],
    )


async def _write_case(root, fixture, *, baseline="route-baseline", advisor="route-advisor"):
    def factory(arm):
        route = baseline if arm == "baseline" else advisor

        async def planner(_request):
            return AdvisorPlannerResult(
                result=_result(route),
                usage=AdvisorUsage(
                    usage_status="measured", input_tokens=10 if arm == "baseline" else 12,
                    output_tokens=2 if arm == "baseline" else 3,
                    usd=0.01 if arm == "baseline" else 0.015,
                ),
            )
        return planner

    return await run_advisor_case(
        fixture, case_root=root, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )


@pytest.mark.asyncio
async def test_case_estimate_reconstructs_supported_reference_gain_readonly(tmp_path):
    fixture = _fixture()
    await _write_case(tmp_path, fixture)
    trace = tmp_path / "metrics" / "advisor-experiment.jsonl"
    before = (trace.stat().st_mtime_ns, trace.read_bytes())
    estimate = build_case_estimate(fixture, tmp_path)
    after = (trace.stat().st_mtime_ns, trace.read_bytes())
    assert before == after
    assert estimate.dataset_status == "clean"
    assert estimate.eligible_for_quality is True
    assert estimate.assessment_verdict == "accepted_reference_gain"
    assert estimate.reference_coverage_delta == 1.0
    assert estimate.input_tokens_delta == 2
    assert estimate.output_tokens_delta == 1
    assert estimate.usd_delta == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_report_never_serializes_hidden_reference_or_opaque_text(tmp_path):
    fixture = _fixture()
    await _write_case(tmp_path, fixture)
    report = build_advisor_report((build_case_estimate(fixture, tmp_path),))
    encoded = advisor_report_json(report)
    for forbidden in (
        "SECRET-OBJECTIVE", "secret hidden route", "route-advisor",
        "RAWFLAG{never-report}", "OPAQUE",
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
async def test_fixture_identity_mismatch_is_corrupt_before_reference_matching(tmp_path):
    fixture = _fixture()
    await _write_case(tmp_path, fixture)
    mismatched = replace(fixture, challenge_id="different-challenge")
    estimate = build_case_estimate(mismatched, tmp_path)
    assert estimate.dataset_status == "corrupt"
    assert estimate.eligible_for_quality is False
    assert "identity_mismatch" in estimate.exclusion_reasons
    assert estimate.assessment_verdict == NA


def test_build_case_estimate_missing_trace_remains_incomplete(tmp_path):
    estimate = build_case_estimate(_fixture(), tmp_path)
    assert estimate.dataset_status == "incomplete"
    assert estimate.exclusion_reasons == ("missing_trace",)


@pytest.mark.asyncio
async def test_duplicate_hidden_reference_ids_are_not_valid_objectives(tmp_path):
    fixture = _fixture()
    await _write_case(tmp_path, fixture)
    duplicate_refs = replace(
        fixture,
        reference_objectives=(
            AdvisorReferenceObjective(
                objective_id="duplicate", route_hash="route-advisor",
            ),
            AdvisorReferenceObjective(
                objective_id="duplicate", route_hash="route-baseline",
            ),
        ),
    )
    estimate = build_case_estimate(duplicate_refs, tmp_path)
    assert estimate.dataset_status == "clean"
    assert estimate.trace_only is True
    assert estimate.eligible_for_quality is False


@pytest.mark.asyncio
async def test_invalid_hidden_route_without_goal_is_not_a_reference(tmp_path):
    fixture = _fixture()
    await _write_case(tmp_path, fixture)
    invalid_reference = replace(
        fixture,
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="invalid-route", route_hash="bad\nroute", goal="",
        ),),
    )
    estimate = build_case_estimate(invalid_reference, tmp_path)
    assert estimate.trace_only is True
    assert estimate.exclusion_reasons == ("missing_reference_objectives",)


@pytest.mark.asyncio
async def test_non_intent_trace_changes_do_not_create_planning_delta(tmp_path):
    fixture = _fixture(reference_route="route-same")

    def factory(arm):
        async def planner(_request):
            result = _result("route-same")
            if arm == "advisor":
                result = replace(
                    result, verdict="course_correct", pinned_facts=[],
                )
            return AdvisorPlannerResult(result=result)
        return planner

    await run_advisor_case(
        fixture, case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    estimate = build_case_estimate(fixture, tmp_path)
    assert estimate.assessment_verdict == "unchanged"
    assert estimate.assessment_reason == "no_planning_delta"


def test_missing_trace_estimate_is_incomplete_and_all_metrics_na():
    estimate = build_missing_trace_estimate(_fixture(), "sidecar_append_failed")
    assert estimate.dataset_status == "incomplete"
    assert estimate.eligible_for_quality is False
    assert estimate.reference_coverage_delta == NA
    assert estimate.wall_seconds_delta == NA
    assert estimate.input_tokens_delta == NA


@pytest.mark.asyncio
async def test_trigger_ineligible_and_missing_reference_are_excluded(tmp_path):
    ineligible = _fixture(eligible=False)
    await _write_case(tmp_path / "a", ineligible)
    estimate = build_case_estimate(ineligible, tmp_path / "a")
    assert estimate.trigger_eligible is False
    assert estimate.eligible_for_quality is False
    assert "trigger_ineligible" in estimate.exclusion_reasons

    trace_only = replace(_fixture(run="run-2"), reference_objectives=())
    await _write_case(tmp_path / "b", trace_only)
    estimate2 = build_case_estimate(trace_only, tmp_path / "b")
    assert estimate2.trace_only is True
    assert "missing_reference_objectives" in estimate2.exclusion_reasons


def _estimate(run, fixture, delta, *, wall=0.0, status="clean", eligible=True,
              verdict="unchanged"):
    return AdvisorCaseEstimate(
        fixture_id=fixture, summary_digest="summary", benchmark_run_id=run,
        challenge_id=f"challenge-{run}", source_event_seq=1,
        dataset_status=status, trigger_eligible=True, trace_only=False,
        eligible_for_quality=eligible, exclusion_reasons=(),
        assessment_verdict=verdict, assessment_reason="no_planning_delta",
        baseline_reference_coverage=0.0 if eligible else NA,
        advisor_reference_coverage=delta if eligible else NA,
        reference_coverage_delta=delta if eligible else NA,
        baseline_intent_count=1 if eligible else NA,
        advisor_intent_count=1 if eligible else NA,
        intent_jaccard=1.0 if eligible else NA,
        advisor_first_reference_count=0 if eligible else NA,
        baseline_only_reference_count=0 if eligible else NA,
        baseline_duplicate_count=0 if eligible else NA,
        advisor_duplicate_count=0 if eligible else NA,
        baseline_unsupported_citation_count=0 if eligible else NA,
        advisor_unsupported_citation_count=0 if eligible else NA,
        wall_seconds_delta=wall, input_tokens_delta=NA,
        output_tokens_delta=NA, usd_delta=NA,
    )


def test_aggregate_averages_cases_within_run_and_exposes_denominators():
    cases = (
        _estimate("run-a", "f1", 1.0, wall=2.0, verdict="accepted_reference_gain"),
        _estimate("run-a", "f2", 0.0, wall=0.0),
        _estimate("run-b", "f3", 0.0, wall=4.0),
        _estimate("run-c", "f4", 0.0, status="incomplete", eligible=False),
    )
    report = build_advisor_report(cases)
    assert report.total_cases == 4
    assert report.total_run_count == 3
    assert report.quality_eligible_cases == 3
    assert report.quality_eligible_run_count == 2
    assert report.mean_reference_coverage_delta == pytest.approx(0.25)
    assert report.mean_wall_seconds_delta == pytest.approx(2.5)
    assert report.accepted_reference_gain_rate == pytest.approx(1 / 3)
    assert report.evidence_tier == "insufficient"
    assert report.reference_coverage_delta_ci95_low == NA
    assert report.real_flag_latency_improvement == NA


def test_reportable_bootstrap_is_deterministic_and_cases_sorted():
    cases = tuple(
        _estimate(f"run-{index:02d}", f"fixture-{20-index:02d}", index / 20)
        for index in range(20)
    )
    first = build_advisor_report(cases)
    second = build_advisor_report(tuple(reversed(cases)))
    assert first.evidence_tier == "reportable"
    assert first.reference_coverage_delta_ci95_low != NA
    assert advisor_report_json(first) == advisor_report_json(second)
    assert [case.benchmark_run_id for case in first.cases] == sorted(
        case.benchmark_run_id for case in first.cases
    )
