from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_benchmark import (
    AdvisorBenchmarkCase,
    AdvisorBenchmarkSuite,
    AdvisorIsolationFailure,
    AdvisorSuiteConstructionError,
    benchmark_result_json,
    run_advisor_benchmark,
)
from dswarm.swarm.advisor_experiment import (
    AdvisorReferenceObjective,
    make_advisor_fixture,
)
from dswarm.swarm.advisor_runner import AdvisorPlannerResult


@pytest.fixture
def artifact_root(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    root = (
        Path(__file__).resolve().parents[1]
        / "eval_runs"
        / "m8-advisor"
        / f"pytest-{tmp_path.name}"
    )
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    return root


def _fixture(*, run: str = "run-1", challenge: str = "challenge-1",
             source_seq: int = 42, summary: str = "OPAQUE RAWFLAG{secret}",
             cycle: str = "reason-2"):
    return make_advisor_fixture(
        benchmark_run_id=run,
        challenge_id=challenge,
        challenge_mode="ctf",
        expected_flags=3,
        captured_flags_before_source=0,
        source_event_seq=source_seq,
        source_event_ts=1.0,
        source_intent_id="intent-source",
        source_route_hash="route-source",
        next_cycle_id=cycle,
        graph_summary=summary,
        fact_index="[10] evidence",
        available_fact_seqs=(10,),
        max_intents=4,
        goal="find remaining flags",
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="HIDDEN-OBJECTIVE",
            route_hash="route-advisor",
            goal="hidden goal text",
        ),),
    )


def _result(route: str) -> ReasonResult:
    return ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id=f"intent-{route}",
            goal=f"inspect {route}",
            route_hash=route,
            worker_class="code",
            direction="web",
            priority=0.5,
            from_facts=[10],
        )],
        audit_notes=[],
        pinned_facts=[10],
        dispatches=[],
    )


class CountingFactory:
    def __init__(self, *, fail: bool = False) -> None:
        self.factory_calls: list[str] = []
        self.planner_calls: list[str] = []
        self.fail = fail

    def __call__(self, arm: str):
        self.factory_calls.append(arm)

        async def planner(_request):
            self.planner_calls.append(arm)
            if self.fail:
                raise RuntimeError("provider secret must not escape")
            route = "route-baseline" if arm == "baseline" else "route-advisor"
            return AdvisorPlannerResult(result=_result(route))

        return planner


def _suite(root: Path, fixture=None, factory=None, *, case_root: Path | None = None):
    fixture = fixture or _fixture()
    factory = factory or CountingFactory()
    return AdvisorBenchmarkSuite(
        artifact_root=root,
        cases=(AdvisorBenchmarkCase(
            case_root=case_root or root / "case-1",
            fixture=fixture,
            planner_factory=factory,
            timeout_s=1.0,
            cleanup_timeout_s=0.2,
        ),),
    )


@pytest.mark.asyncio
async def test_valid_suite_runs_in_declared_order_and_builds_safe_result(artifact_root):
    first = CountingFactory()
    second = CountingFactory()
    suite = AdvisorBenchmarkSuite(
        artifact_root=artifact_root,
        cases=(
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-1",
                fixture=_fixture(run="run-1", challenge="challenge-1", source_seq=41),
                planner_factory=first,
                timeout_s=1.0,
                cleanup_timeout_s=0.2,
            ),
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-2",
                fixture=_fixture(run="run-2", challenge="challenge-2", source_seq=42),
                planner_factory=second,
                timeout_s=1.0,
                cleanup_timeout_s=0.2,
            ),
        ),
    )

    result = await run_advisor_benchmark(suite)

    assert result.declared_case_count == 2
    assert result.reported_case_count == 2
    assert result.case_local_failure_count == 0
    assert [item.status for item in result.case_results] == ["reported", "reported"]
    assert first.planner_calls and second.planner_calls
    encoded = benchmark_result_json(result)
    assert encoded == benchmark_result_json(result)
    for forbidden in (
        "RAWFLAG{secret}", "HIDDEN-OBJECTIVE", "hidden goal text",
        "provider secret must not escape", str(artifact_root),
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "code"),
    (
        (lambda suite: replace(suite, cases=suite.cases + (suite.cases[0],)),
         "duplicate_fixture_id"),
        (lambda suite: replace(
            suite,
            cases=suite.cases + (replace(
                suite.cases[0],
                fixture=_fixture(run="run-2", challenge="challenge-2", source_seq=99),
            ),),
        ), "duplicate_case_root"),
        (lambda suite: replace(
            suite,
            cases=suite.cases + (replace(
                suite.cases[0],
                case_root=Path(suite.artifact_root) / "case-2",
                fixture=_fixture(cycle="reason-3", summary="different opaque summary"),
            ),),
        ), "source_identity_conflict"),
        (lambda suite: replace(
            suite,
            cases=(replace(
                suite.cases[0],
                fixture=replace(suite.cases[0].fixture, summary_digest="bad"),
            ),),
        ), "invalid_summary_digest"),
        (lambda suite: replace(
            suite, cases=(replace(suite.cases[0], timeout_s=float("nan")),),
        ), "invalid_timeout"),
        (lambda suite: replace(suite, bootstrap_samples=0),
         "invalid_bootstrap_settings"),
    ),
)
async def test_invalid_suite_fails_before_any_factory_or_directory(
    artifact_root, mutate, code,
):
    factory = CountingFactory()
    suite = mutate(_suite(artifact_root, factory=factory))

    with pytest.raises(AdvisorSuiteConstructionError) as raised:
        await run_advisor_benchmark(suite)

    assert raised.value.code == code
    assert factory.factory_calls == []
    assert not artifact_root.exists()


@pytest.mark.asyncio
async def test_artifact_and_case_paths_must_be_strict_allowed_descendants(
    artifact_root, tmp_path,
):
    fixture = _fixture()
    factory = CountingFactory()
    repo = Path(__file__).resolve().parents[1]
    invalid_suites = (
        _suite(repo / "eval_runs" / "m8-advisor", fixture, factory),
        _suite(tmp_path / "outside", fixture, factory),
        _suite(artifact_root, fixture, factory, case_root=artifact_root),
        _suite(artifact_root, fixture, factory, case_root=artifact_root / ".." / "escape"),
    )

    for suite in invalid_suites:
        with pytest.raises(AdvisorSuiteConstructionError):
            await run_advisor_benchmark(suite)

    assert factory.factory_calls == []


@pytest.mark.asyncio
async def test_existing_clean_trace_is_reused_without_planner_calls(artifact_root):
    fixture = _fixture()
    first = CountingFactory()
    initial = await run_advisor_benchmark(_suite(artifact_root, fixture, first))
    assert initial.reported_case_count == 1

    forbidden = CountingFactory()
    reused = await run_advisor_benchmark(_suite(artifact_root, fixture, forbidden))

    assert reused.case_results[0].status == "reported"
    assert forbidden.factory_calls == []
    assert forbidden.planner_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_code"),
    (
        (b'{"partial":true}', "existing_trace_partial"),
        (b'{}\n', "existing_trace_corrupt"),
    ),
)
async def test_existing_bad_trace_is_case_local_and_never_rerun(
    artifact_root, payload, expected_code,
):
    case_root = artifact_root / "case-1"
    trace = case_root / "metrics" / "advisor-experiment.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_bytes(payload)
    factory = CountingFactory()

    result = await run_advisor_benchmark(_suite(artifact_root, factory=factory))

    item = result.case_results[0]
    assert item.status == "case_local_failure"
    assert item.failure_code == expected_code
    assert item.estimate.dataset_status in {"incomplete", "corrupt"}
    assert factory.factory_calls == []
    assert trace.read_bytes() == payload


@pytest.mark.asyncio
async def test_present_writer_lock_is_busy_without_fold_or_factory(artifact_root):
    case_root = artifact_root / "case-1"
    lock = case_root / "metrics" / "advisor-experiment.writer.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("stale owner", encoding="utf-8")
    factory = CountingFactory()

    result = await run_advisor_benchmark(_suite(artifact_root, factory=factory))

    assert result.case_results[0].failure_code == "advisor_writer_busy"
    assert factory.factory_calls == []
    assert lock.read_text(encoding="utf-8") == "stale owner"


@pytest.mark.asyncio
async def test_durable_planner_failure_is_reported_and_later_case_continues(artifact_root):
    failing = CountingFactory(fail=True)
    succeeding = CountingFactory()
    suite = AdvisorBenchmarkSuite(
        artifact_root=artifact_root,
        cases=(
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-1",
                fixture=_fixture(run="run-1", challenge="challenge-1", source_seq=1),
                planner_factory=failing,
                timeout_s=1.0,
                cleanup_timeout_s=0.2,
            ),
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-2",
                fixture=_fixture(run="run-2", challenge="challenge-2", source_seq=2),
                planner_factory=succeeding,
                timeout_s=1.0,
                cleanup_timeout_s=0.2,
            ),
        ),
    )

    result = await run_advisor_benchmark(suite)

    assert [item.status for item in result.case_results] == ["reported", "reported"]
    assert result.case_results[0].estimate.assessment_verdict == "indeterminate_planner_error"
    assert succeeding.planner_calls


@pytest.mark.asyncio
async def test_isolation_failure_propagates_and_stops_later_cases(
    artifact_root, monkeypatch,
):
    calls: list[str] = []

    async def fail_isolation(fixture, **_kwargs):
        calls.append(fixture.benchmark_run_id)
        raise AdvisorIsolationFailure("owned_task_still_running")

    monkeypatch.setattr(
        "dswarm.swarm.advisor_benchmark.run_advisor_case", fail_isolation,
    )
    suite = AdvisorBenchmarkSuite(
        artifact_root=artifact_root,
        cases=(
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-1", fixture=_fixture(run="run-1"),
                planner_factory=CountingFactory(),
            ),
            AdvisorBenchmarkCase(
                case_root=artifact_root / "case-2",
                fixture=_fixture(run="run-2", challenge="challenge-2", source_seq=43),
                planner_factory=CountingFactory(),
            ),
        ),
    )

    with pytest.raises(AdvisorIsolationFailure):
        await run_advisor_benchmark(suite)

    assert calls == ["run-1"]


@pytest.mark.asyncio
async def test_external_cancellation_propagates(artifact_root, monkeypatch):
    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("dswarm.swarm.advisor_benchmark.run_advisor_case", cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_advisor_benchmark(_suite(artifact_root))
