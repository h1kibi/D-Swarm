from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import weakref

import pytest

from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_experiment import (
    AdvisorReferenceObjective,
    make_advisor_fixture,
)
from dswarm.swarm.advisor_runner import (
    AdvisorIsolationFailure,
    AdvisorPlannerResult,
    AdvisorUsage,
    arm_order_for,
    run_advisor_case,
)
from dswarm.swarm.advisor_sidecar import fold_advisor_trace


def _fixture(*, eligible: bool = True, summary: str = "OPAQUE RAWFLAG{sentinel}"):
    return make_advisor_fixture(
        benchmark_run_id="run-1", challenge_id="challenge-1",
        challenge_mode="ctf", expected_flags=3 if eligible else 1,
        captured_flags_before_source=0, source_event_seq=42,
        source_event_ts=1.0, source_intent_id="intent-source",
        source_route_hash="route-source", next_cycle_id="reason-2",
        graph_summary=summary, fact_index="[10] evidence", available_fact_seqs=(10,),
        max_intents=4, goal="find remaining flags",
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="HIDDEN-LABEL", route_hash="route-hidden",
        ),),
    )


def _result(route: str = "route-next", *, goal: str = "inspect next"):
    return ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id=f"intent-{route}", goal=goal, route_hash=route,
            worker_class="code", direction="web", priority=0.5,
            from_facts=[10],
        )],
        audit_notes=[], pinned_facts=[10], dispatches=[],
    )


class Planner:
    def __init__(self, name, calls, *, result=None, delay=0.0, error=None):
        self.name = name
        self.calls = calls
        self.result = result or _result(f"route-{name}")
        self.delay = delay
        self.error = error
        self.started = False

    async def __call__(self, request):
        self.started = True
        self.calls.append((self.name, request))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return AdvisorPlannerResult(
            result=self.result,
            usage=AdvisorUsage(
                usage_status="measured", input_tokens=10,
                output_tokens=2, usd=0.01,
            ),
        )


class Factory:
    def __init__(self, **planner_kwargs):
        self.calls = []
        self.instances = []
        self.planner_kwargs = planner_kwargs

    def __call__(self, arm):
        planner = Planner(arm, self.calls, **self.planner_kwargs.get(arm, {}))
        self.instances.append(planner)
        return planner


def test_usage_status_validation_and_dimension_preservation():
    assert AdvisorUsage(usage_status="measured", input_tokens=0).input_tokens == 0
    assert AdvisorUsage(usage_status="estimated", usd=0.0).usd == 0.0
    assert AdvisorUsage(usage_status="unknown") == AdvisorUsage(usage_status="unknown")
    with pytest.raises(ValueError):
        AdvisorUsage(usage_status="unknown", input_tokens=0)
    with pytest.raises(ValueError):
        AdvisorUsage(usage_status="measured")
    with pytest.raises(ValueError):
        AdvisorUsage(usage_status="measured", input_tokens=True)
    with pytest.raises(ValueError):
        AdvisorUsage(usage_status="estimated", usd=float("nan"))


def test_arm_order_is_stable_and_has_both_parities():
    assert arm_order_for("fixture-a") == arm_order_for("fixture-a")
    orders = {arm_order_for(f"fixture-{index}") for index in range(64)}
    assert orders == {("baseline", "advisor"), ("advisor", "baseline")}


@pytest.mark.asyncio
async def test_eligible_case_uses_distinct_planners_and_safe_requests(tmp_path):
    fixture = _fixture()
    factory = Factory()
    outcome = await run_advisor_case(
        fixture, case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    assert outcome.dataset_status == "clean"
    assert len(factory.instances) == 2
    assert factory.instances[0] is not factory.instances[1]
    requests = {arm: request for arm, request in factory.calls}
    assert requests["baseline"].graph_summary == fixture.graph_summary
    assert requests["advisor"].graph_summary.startswith(fixture.graph_summary + "\n\n")
    suffix = requests["advisor"].graph_summary[len(fixture.graph_summary):]
    assert "RAWFLAG{sentinel}" not in suffix
    trace_bytes = (tmp_path / "metrics" / "advisor-experiment.jsonl").read_text()
    assert "RAWFLAG{sentinel}" not in trace_bytes
    assert "HIDDEN-LABEL" not in trace_bytes
    assert fold_advisor_trace(tmp_path).complete is True


@pytest.mark.asyncio
async def test_ineligible_case_runs_only_baseline(tmp_path):
    factory = Factory()
    outcome = await run_advisor_case(
        _fixture(eligible=False), case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    assert [arm for arm, _ in factory.calls] == ["baseline"]
    assert outcome.advisor.call_outcome == "not_run"
    assert fold_advisor_trace(tmp_path).complete is True


@pytest.mark.asyncio
async def test_ordinary_timeout_is_terminal_and_other_arm_still_runs(tmp_path):
    factory = Factory(baseline={"delay": 0.2})
    outcome = await run_advisor_case(
        _fixture(), case_root=tmp_path, planner_factory=factory,
        timeout_s=0.01, cleanup_timeout_s=0.2,
    )
    assert outcome.baseline.call_outcome == "timeout"
    assert outcome.advisor.call_outcome == "succeeded"
    assert fold_advisor_trace(tmp_path).complete is True


@pytest.mark.asyncio
async def test_timeout_discards_late_result_after_cooperative_cleanup(tmp_path):
    calls = []

    class SwallowOnce:
        async def __call__(self, request):
            calls.append(request.arm)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return AdvisorPlannerResult(result=_result("late"))

    def factory(arm):
        return SwallowOnce() if arm == "baseline" else Planner(arm, calls)

    outcome = await run_advisor_case(
        _fixture(), case_root=tmp_path, planner_factory=factory,
        timeout_s=0.01, cleanup_timeout_s=0.2,
    )
    assert outcome.baseline.call_outcome == "timeout"
    assert outcome.baseline.result is None


@pytest.mark.asyncio
async def test_noncooperative_timeout_raises_isolation_failure_and_stops_second_arm(tmp_path):
    release = asyncio.Event()
    calls = []

    class NonCooperative:
        async def __call__(self, request):
            calls.append(request.arm)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await release.wait()
                return AdvisorPlannerResult(result=_result("late"))

    def factory(arm):
        return NonCooperative() if arm == arm_order_for(_fixture().fixture_id)[0] else Planner(arm, calls)

    with pytest.raises(AdvisorIsolationFailure):
        await run_advisor_case(
            _fixture(), case_root=tmp_path, planner_factory=factory,
            timeout_s=0.01, cleanup_timeout_s=0.01,
        )
    assert len(calls) == 1
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_first_planner_instance_remains_alive_until_second_is_created(tmp_path):
    first_ref = None

    def factory(arm):
        nonlocal first_ref

        async def planner(_request):
            return AdvisorPlannerResult(result=_result(f"route-{arm}"))

        if first_ref is None:
            first_ref = weakref.ref(planner)
        else:
            assert first_ref() is not None
        return planner

    outcome = await run_advisor_case(
        _fixture(), case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    assert outcome.baseline.call_outcome == "succeeded"
    assert outcome.advisor.call_outcome == "succeeded"


@pytest.mark.asyncio
async def test_same_planner_object_is_rejected_without_cross_arm_reuse(tmp_path):
    calls = []
    planner = Planner("shared", calls)

    def factory(_arm):
        return planner

    outcome = await run_advisor_case(
        _fixture(), case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    assert {outcome.baseline.call_outcome, outcome.advisor.call_outcome} == {
        "succeeded", "setup_error",
    }


@pytest.mark.asyncio
async def test_custom_event_loop_task_factory_is_rejected_before_trace(tmp_path):
    loop = asyncio.get_running_loop()
    old = loop.get_task_factory()
    loop.set_task_factory(lambda loop, coro: asyncio.tasks.Task(coro, loop=loop))
    try:
        with pytest.raises(AdvisorIsolationFailure):
            await run_advisor_case(
                _fixture(), case_root=tmp_path, planner_factory=Factory(),
            )
    finally:
        loop.set_task_factory(old)
    assert not (tmp_path / "metrics" / "advisor-experiment.jsonl").exists()


@pytest.mark.asyncio
async def test_sensitive_planner_echo_is_redacted_and_case_incomplete(tmp_path):
    factory = Factory(baseline={"result": _result(goal="OPAQUE RAWFLAG{sentinel}")})
    outcome = await run_advisor_case(
        _fixture(), case_root=tmp_path, planner_factory=factory,
        timeout_s=1.0, cleanup_timeout_s=0.2,
    )
    assert outcome.dataset_status == "incomplete"
    data = (tmp_path / "metrics" / "advisor-experiment.jsonl").read_text()
    assert "RAWFLAG{sentinel}" not in data
    assert "sensitive_output_redacted" in data


@pytest.mark.asyncio
async def test_external_cancellation_writes_interruption_and_propagates(tmp_path):
    entered = asyncio.Event()

    class Blocking:
        async def __call__(self, request):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(run_advisor_case(
        _fixture(eligible=False), case_root=tmp_path,
        planner_factory=lambda arm: Blocking(), timeout_s=20,
        cleanup_timeout_s=0.2,
    ))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fold = fold_advisor_trace(tmp_path)
    assert fold.dataset_status == "incomplete"
    assert fold.case_interrupted is not None


def test_runner_modules_are_offline_isolated_and_avoid_generic_dumping():
    root = Path(__file__).resolve().parents[1]
    targets = [
        root / "dswarm/swarm/advisor_experiment.py",
        root / "dswarm/swarm/advisor_sidecar.py",
        root / "dswarm/swarm/advisor_runner.py",
    ]
    forbidden = (
        "shared_graph", "reason_scheduler", "EventBus", "apps.web", "apps.tui",
        "solver.gate", "asdict(", "model_dump(", "vars(", ".__dict__",
        "Intent.to_payload",
    )
    for target in targets:
        text = target.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), target
