"""Isolated paired planner runner for the offline M8 Advisor experiment."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from hashlib import blake2b
import math
import time
from typing import Awaitable, Callable, Literal

from dswarm.solver.reason import ReasonResult
from dswarm.swarm.advisor_experiment import (
    AdvisorFixture,
    AdvisorReasonTrace,
    AdvisorSensitiveOutput,
    IntentComparison,
    build_experimental_summary,
    compare_intent_traces,
    flag_scout_trigger,
    safe_reason_trace,
)
from dswarm.swarm.advisor_sidecar import (
    AdvisorTraceCorrupt,
    AdvisorTraceSink,
    reason_trace_payload,
)

ArmName = Literal["baseline", "advisor"]


class AdvisorIsolationFailure(RuntimeError):
    """Planner execution can no longer be proven isolated."""


@dataclass(frozen=True, kw_only=True)
class AdvisorUsage:
    usage_status: Literal["measured", "estimated", "unknown"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None

    def __post_init__(self) -> None:
        if self.usage_status not in {"measured", "estimated", "unknown"}:
            raise ValueError("invalid usage_status")
        numeric = (self.input_tokens, self.output_tokens, self.usd)
        if self.usage_status == "unknown":
            if any(value is not None for value in numeric):
                raise ValueError("unknown usage must not contain numeric values")
            return
        if all(value is None for value in numeric):
            raise ValueError("measured or estimated usage requires a numeric value")
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid token usage")
        if self.usd is not None:
            if isinstance(self.usd, bool) or not isinstance(self.usd, (int, float)):
                raise ValueError("invalid usd usage")
            if not math.isfinite(float(self.usd)) or self.usd < 0:
                raise ValueError("invalid usd usage")


@dataclass(frozen=True, kw_only=True)
class AdvisorPlannerRequest:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    arm: ArmName
    graph_summary: str
    fact_index: str
    max_intents: int
    goal: str
    mode: str


@dataclass(frozen=True, kw_only=True)
class AdvisorPlannerResult:
    result: ReasonResult
    usage: AdvisorUsage = field(
        default_factory=lambda: AdvisorUsage(usage_status="unknown")
    )

    def __post_init__(self) -> None:
        if not isinstance(self.usage, AdvisorUsage):
            raise ValueError("invalid planner usage")


@dataclass(frozen=True, kw_only=True)
class AdvisorArmOutcome:
    arm: ArmName
    call_outcome: Literal[
        "succeeded", "planner_error", "timeout", "setup_error", "not_run",
    ]
    started_ts: float | None
    finished_ts: float | None
    wall_seconds: float | None
    result: AdvisorReasonTrace | None
    usage: AdvisorUsage
    error_code: Literal[
        "", "planner_factory_failed", "planner_call_failed",
        "invalid_planner_output", "sensitive_output_redacted",
        "planner_timeout", "sidecar_append_failed",
    ] = ""


@dataclass(frozen=True, kw_only=True)
class AdvisorCaseOutcome:
    fixture_id: str
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    trigger_reason: str
    suggestion_id: str
    baseline: AdvisorArmOutcome
    advisor: AdvisorArmOutcome
    comparison: IntentComparison | None
    failure_code: str = ""


PlannerCallable = Callable[[AdvisorPlannerRequest], Awaitable[AdvisorPlannerResult]]
PlannerFactory = Callable[[ArmName], PlannerCallable]


def arm_order_for(fixture_id: str) -> tuple[ArmName, ArmName]:
    byte = blake2b(str(fixture_id).encode("utf-8"), digest_size=1).digest()[0]
    return ("baseline", "advisor") if byte % 2 == 0 else ("advisor", "baseline")


def _validate_deadline(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"invalid {name}")
    return number


def _unknown_usage() -> AdvisorUsage:
    return AdvisorUsage(usage_status="unknown")


def _not_run(arm: ArmName) -> AdvisorArmOutcome:
    return AdvisorArmOutcome(
        arm=arm, call_outcome="not_run", started_ts=None, finished_ts=None,
        wall_seconds=None, result=None, usage=_unknown_usage(),
    )


def _usage_payload(usage: AdvisorUsage) -> dict[str, object]:
    return {
        "usage_status": usage.usage_status,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "usd": usage.usd,
    }


def _request_for(fixture: AdvisorFixture, arm: ArmName, summary: str) -> AdvisorPlannerRequest:
    return AdvisorPlannerRequest(
        fixture_id=fixture.fixture_id,
        summary_digest=fixture.summary_digest,
        benchmark_run_id=fixture.benchmark_run_id,
        challenge_id=fixture.challenge_id,
        arm=arm,
        graph_summary=summary,
        fact_index=fixture.fact_index,
        max_intents=fixture.max_intents,
        goal=fixture.goal,
        mode=fixture.challenge_mode,
    )


class _SidecarAppendFailed(RuntimeError):
    pass


async def _bounded_cancel(task: asyncio.Task[object], timeout_s: float) -> bool:
    if not task.done():
        task.cancel()
    waiter = asyncio.create_task(asyncio.wait({task}, timeout=timeout_s))
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            done, _ = await asyncio.shield(waiter)
            return task in done
        except asyncio.CancelledError:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            continue


def _failure_payload(*, arm: ArmName, arm_index: int, call_outcome: str,
                     failure_stage: str, error_code: str, started_ts: float,
                     finished_ts: float, wall_seconds: float,
                     usage: AdvisorUsage) -> dict[str, object]:
    return {
        "arm": arm, "arm_index": arm_index, "call_outcome": call_outcome,
        "failure_stage": failure_stage, "error_code": error_code,
        "started_ts": started_ts, "finished_ts": finished_ts,
        "wall_seconds": wall_seconds, "usage": _usage_payload(usage),
    }


def _completed_payload(*, arm: ArmName, arm_index: int,
                       started_ts: float, finished_ts: float,
                       wall_seconds: float, result: AdvisorReasonTrace,
                       usage: AdvisorUsage) -> dict[str, object]:
    return {
        "arm": arm, "arm_index": arm_index, "call_outcome": "succeeded",
        "started_ts": started_ts, "finished_ts": finished_ts,
        "wall_seconds": wall_seconds,
        "safe_reason_trace": reason_trace_payload(result),
        "usage": _usage_payload(usage),
    }


def _append_or_fail(sink: AdvisorTraceSink, **kwargs: object) -> None:
    try:
        sink.append(**kwargs)  # type: ignore[arg-type]
    except (OSError, AdvisorTraceCorrupt) as exc:
        raise _SidecarAppendFailed("sidecar_append_failed") from exc


async def _execute_arm(
    *, fixture: AdvisorFixture, arm: ArmName, arm_index: int,
    summary: str, planner_factory: PlannerFactory,
    seen_planners: list[PlannerCallable], sink: AdvisorTraceSink,
    suggestion_id: str, timeout_s: float, cleanup_timeout_s: float,
    lifecycle: dict[str, object],
) -> AdvisorArmOutcome:
    started_ts = time.time()
    started_mono = time.monotonic()
    _append_or_fail(
        sink, kind=f"{arm}_started", identity=f"{arm}:{arm_index}",
        payload={"arm": arm, "arm_index": arm_index, "stage": "setup"},
        ts=started_ts,
    )
    lifecycle["arm_started"] = arm
    request = _request_for(fixture, arm, summary)
    try:
        planner = planner_factory(arm)
        if (not callable(planner)
                or any(planner is prior for prior in seen_planners)):
            raise ValueError("planner_instance_not_fresh")
        # Retain the actual callable until both arms are constructed.  Retaining
        # only id(planner) permits CPython to recycle the first arm's id.
        seen_planners.append(planner)
        coroutine = planner(request)
        if not asyncio.iscoroutine(coroutine):
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise ValueError("planner_not_cold_coroutine")
    except Exception:
        finished_ts = time.time()
        wall = max(0.0, time.monotonic() - started_mono)
        usage = _unknown_usage()
        _append_or_fail(
            sink, kind=f"{arm}_failed", identity=f"{arm}:{arm_index}",
            payload=_failure_payload(
                arm=arm, arm_index=arm_index, call_outcome="setup_error",
                failure_stage="pre_submit", error_code="planner_factory_failed",
                started_ts=started_ts, finished_ts=finished_ts,
                wall_seconds=wall, usage=usage,
            ), ts=finished_ts,
        )
        if arm == "advisor":
            _append_or_fail(
                sink, kind="suggestion_rejected", identity=suggestion_id,
                payload={
                    "suggestion_id": suggestion_id,
                    "reason_code": "planner_setup_failed_before_submit",
                }, ts=finished_ts,
            )
            lifecycle["suggestion_terminal"] = "rejected"
        return AdvisorArmOutcome(
            arm=arm, call_outcome="setup_error", started_ts=started_ts,
            finished_ts=finished_ts, wall_seconds=wall, result=None,
            usage=usage, error_code="planner_factory_failed",
        )

    task: asyncio.Task[AdvisorPlannerResult] = asyncio.create_task(coroutine)
    lifecycle["owned_task"] = task
    if arm == "advisor":
        try:
            _append_or_fail(
                sink, kind="suggestion_consumed", identity=suggestion_id,
                payload={"suggestion_id": suggestion_id, "arm": "advisor"},
                ts=time.time(),
            )
            lifecycle["suggestion_terminal"] = "consumed"
        except BaseException:
            cleaned = await _bounded_cancel(task, cleanup_timeout_s)
            lifecycle["owned_task"] = None
            if not cleaned:
                raise AdvisorIsolationFailure("advisor_admission_cleanup_failed")
            raise

    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_s)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        cleaned = await _bounded_cancel(task, cleanup_timeout_s)
        lifecycle["owned_task"] = None
        if not cleaned:
            lifecycle["isolation_compromised"] = True
        raise

    if task not in done:
        cleaned = await _bounded_cancel(task, cleanup_timeout_s)
        lifecycle["owned_task"] = None
        if not cleaned:
            lifecycle["isolation_compromised"] = True
            try:
                sink.append(
                    kind="case_interrupted", identity="case", payload={
                        "interruption_code": "timeout_cleanup_failed",
                        "lifecycle_stage": f"{arm}_task",
                    }, ts=time.time(),
                )
            except BaseException:
                pass
            raise AdvisorIsolationFailure("timeout_cleanup_failed")
        finished_ts = time.time()
        wall = max(0.0, time.monotonic() - started_mono)
        usage = _unknown_usage()
        _append_or_fail(
            sink, kind=f"{arm}_failed", identity=f"{arm}:{arm_index}",
            payload=_failure_payload(
                arm=arm, arm_index=arm_index, call_outcome="timeout",
                failure_stage="post_submit", error_code="planner_timeout",
                started_ts=started_ts, finished_ts=finished_ts,
                wall_seconds=wall, usage=usage,
            ), ts=finished_ts,
        )
        return AdvisorArmOutcome(
            arm=arm, call_outcome="timeout", started_ts=started_ts,
            finished_ts=finished_ts, wall_seconds=wall, result=None,
            usage=usage, error_code="planner_timeout",
        )

    lifecycle["owned_task"] = None
    finished_ts = time.time()
    wall = max(0.0, time.monotonic() - started_mono)
    try:
        planner_result = task.result()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        usage = _unknown_usage()
        _append_or_fail(
            sink, kind=f"{arm}_failed", identity=f"{arm}:{arm_index}",
            payload=_failure_payload(
                arm=arm, arm_index=arm_index, call_outcome="planner_error",
                failure_stage="post_submit", error_code="planner_call_failed",
                started_ts=started_ts, finished_ts=finished_ts,
                wall_seconds=wall, usage=usage,
            ), ts=finished_ts,
        )
        return AdvisorArmOutcome(
            arm=arm, call_outcome="planner_error", started_ts=started_ts,
            finished_ts=finished_ts, wall_seconds=wall, result=None,
            usage=usage, error_code="planner_call_failed",
        )

    if not isinstance(planner_result, AdvisorPlannerResult):
        planner_result = None
    try:
        if planner_result is None:
            raise AdvisorSensitiveOutput("invalid_planner_output", "reason_result")
        safe = safe_reason_trace(
            planner_result.result,
            available_fact_seqs=fixture.available_fact_seqs,
            forbidden_fragments=(fixture.graph_summary,),
        )
        usage = planner_result.usage
    except AdvisorSensitiveOutput as exc:
        code = (
            "sensitive_output_redacted"
            if exc.code == "sensitive_output_redacted"
            else "invalid_planner_output"
        )
        usage = _unknown_usage()
        _append_or_fail(
            sink, kind=f"{arm}_failed", identity=f"{arm}:{arm_index}",
            payload=_failure_payload(
                arm=arm, arm_index=arm_index, call_outcome="planner_error",
                failure_stage="post_submit", error_code=code,
                started_ts=started_ts, finished_ts=finished_ts,
                wall_seconds=wall, usage=usage,
            ), ts=finished_ts,
        )
        return AdvisorArmOutcome(
            arm=arm, call_outcome="planner_error", started_ts=started_ts,
            finished_ts=finished_ts, wall_seconds=wall, result=None,
            usage=usage, error_code=code,  # type: ignore[arg-type]
        )

    _append_or_fail(
        sink, kind=f"{arm}_completed", identity=f"{arm}:{arm_index}",
        payload=_completed_payload(
            arm=arm, arm_index=arm_index, started_ts=started_ts,
            finished_ts=finished_ts, wall_seconds=wall, result=safe,
            usage=usage,
        ), ts=finished_ts,
    )
    return AdvisorArmOutcome(
        arm=arm, call_outcome="succeeded", started_ts=started_ts,
        finished_ts=finished_ts, wall_seconds=wall, result=safe, usage=usage,
    )


async def run_advisor_case(
    fixture: AdvisorFixture, *, case_root: str,
    planner_factory: PlannerFactory, timeout_s: float = 180.0,
    cleanup_timeout_s: float = 5.0,
) -> AdvisorCaseOutcome:
    """Run one offline paired case without touching production swarm state."""

    if not isinstance(fixture, AdvisorFixture):
        raise ValueError("invalid fixture")
    timeout = _validate_deadline(timeout_s, "timeout_s")
    cleanup_timeout = _validate_deadline(cleanup_timeout_s, "cleanup_timeout_s")
    loop = asyncio.get_running_loop()
    if loop.get_task_factory() is not None:
        raise AdvisorIsolationFailure("custom_task_factory")

    trigger = flag_scout_trigger(fixture)
    suggestion = trigger.suggestion
    order: tuple[ArmName, ...] = (
        arm_order_for(fixture.fixture_id) if trigger.eligible else ("baseline",)
    )
    suggestion_id = suggestion.suggestion_id if suggestion is not None else ""
    baseline = _not_run("baseline")
    advisor = _not_run("advisor")
    lifecycle: dict[str, object] = {
        "owned_task": None,
        "suggestion_created": False,
        "suggestion_terminal": "",
        "isolation_compromised": False,
    }

    try:
        sink = AdvisorTraceSink(
            case_root,
            fixture_id=fixture.fixture_id,
            summary_digest=fixture.summary_digest,
            benchmark_run_id=fixture.benchmark_run_id,
        )
    except Exception:
        return AdvisorCaseOutcome(
            fixture_id=fixture.fixture_id, dataset_status="incomplete",
            trigger_reason=trigger.reason, suggestion_id=suggestion_id,
            baseline=baseline, advisor=advisor, comparison=None,
            failure_code="sidecar_append_failed",
        )

    with sink:
        try:
            _append_or_fail(
                sink, kind="case_started", identity="case", payload={
                    "fixture_id": fixture.fixture_id,
                    "summary_digest": fixture.summary_digest,
                    "benchmark_run_id": fixture.benchmark_run_id,
                    "challenge_id": fixture.challenge_id,
                    "source_kind": fixture.source_kind,
                    "source_event_seq": fixture.source_event_seq,
                    "source_intent_id": fixture.source_intent_id,
                    "source_route_hash": fixture.source_route_hash,
                    "eligible": trigger.eligible,
                    "trigger_reason": trigger.reason,
                    "arm_order": list(order),
                    "available_fact_seqs": list(fixture.available_fact_seqs),
                }, ts=time.time(),
            )
            if suggestion is not None:
                _append_or_fail(
                    sink, kind="suggestion_created",
                    identity=suggestion.suggestion_id,
                    payload={
                        "suggestion_id": suggestion.suggestion_id,
                        "source_event_seq": suggestion.source_event_seq,
                        "route_attribution": suggestion.route_attribution,
                    }, ts=time.time(),
                )
                lifecycle["suggestion_created"] = True

            summaries = {"baseline": fixture.graph_summary}
            if suggestion is not None:
                summaries["advisor"] = build_experimental_summary(fixture, suggestion)
            seen_planners: list[PlannerCallable] = []
            for index, arm in enumerate(order):
                arm_outcome = await _execute_arm(
                    fixture=fixture, arm=arm, arm_index=index,
                    summary=summaries[arm], planner_factory=planner_factory,
                    seen_planners=seen_planners, sink=sink,
                    suggestion_id=suggestion_id, timeout_s=timeout,
                    cleanup_timeout_s=cleanup_timeout, lifecycle=lifecycle,
                )
                if arm == "baseline":
                    baseline = arm_outcome
                else:
                    advisor = arm_outcome
                lifecycle["arm_started"] = ""
                if arm_outcome.error_code == "sensitive_output_redacted":
                    try:
                        sink.append(
                            kind="case_interrupted", identity="case", payload={
                                "interruption_code": "sensitive_output_redacted",
                                "lifecycle_stage": f"{arm}_terminal",
                            }, ts=time.time(),
                        )
                    except BaseException:
                        pass
                    return AdvisorCaseOutcome(
                        fixture_id=fixture.fixture_id,
                        dataset_status="incomplete",
                        trigger_reason=trigger.reason,
                        suggestion_id=suggestion_id,
                        baseline=baseline, advisor=advisor, comparison=None,
                        failure_code="sensitive_output_redacted",
                    )

            comparison = None
            if baseline.result is not None and advisor.result is not None:
                comparison = compare_intent_traces(
                    baseline.result, advisor.result,
                    available_fact_seqs=fixture.available_fact_seqs,
                )
            trace_digest, comparison_digest = sink.current_digests()
            _append_or_fail(
                sink, kind="case_completed", identity="case", payload={
                    "fixture_id": fixture.fixture_id,
                    "summary_digest": fixture.summary_digest,
                    "benchmark_run_id": fixture.benchmark_run_id,
                    "trace_result_digest": trace_digest,
                    "comparison_digest": comparison_digest,
                    "terminal_status": "clean",
                }, ts=time.time(),
            )
            return AdvisorCaseOutcome(
                fixture_id=fixture.fixture_id, dataset_status="clean",
                trigger_reason=trigger.reason, suggestion_id=suggestion_id,
                baseline=baseline, advisor=advisor, comparison=comparison,
            )
        except _SidecarAppendFailed:
            return AdvisorCaseOutcome(
                fixture_id=fixture.fixture_id, dataset_status="incomplete",
                trigger_reason=trigger.reason, suggestion_id=suggestion_id,
                baseline=baseline, advisor=advisor, comparison=None,
                failure_code="sidecar_append_failed",
            )
        except AdvisorIsolationFailure:
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as original:
            task = lifecycle.get("owned_task")
            cleanup_failed = False
            if isinstance(task, asyncio.Task) and not task.done():
                cleanup_failed = not await _bounded_cancel(task, cleanup_timeout)
            if (lifecycle.get("suggestion_created")
                    and not lifecycle.get("suggestion_terminal")):
                try:
                    sink.append(
                        kind="suggestion_rejected", identity=suggestion_id,
                        payload={
                            "suggestion_id": suggestion_id,
                            "reason_code": "runner_cancelled_before_submit",
                        }, ts=time.time(),
                    )
                except BaseException:
                    pass
            try:
                sink.append(
                    kind="case_interrupted", identity="case", payload={
                        "interruption_code": (
                            "external_interrupt_cleanup_failed" if cleanup_failed
                            else "task_cancelled" if isinstance(original, asyncio.CancelledError)
                            else "process_interrupted"
                        ),
                        "lifecycle_stage": str(lifecycle.get("arm_started") or "case"),
                    }, ts=time.time(),
                )
            except BaseException:
                pass
            raise


__all__ = [
    "AdvisorArmOutcome", "AdvisorCaseOutcome", "AdvisorIsolationFailure",
    "AdvisorPlannerRequest", "AdvisorPlannerResult", "AdvisorUsage",
    "PlannerCallable", "PlannerFactory", "arm_order_for", "run_advisor_case",
]
