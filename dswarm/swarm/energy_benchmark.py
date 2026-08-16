"""Independent M7 offline scheduling-reorder benchmark harness.

This module deliberately stays outside the Web/UI and online dispatch paths.  A
benchmark case owns a run-scoped :class:`EnergyTraceSink`; its factory must
inject that sink into a ``ReasonSwarm`` configured with energy tracing enabled.
The resulting sidecar is finalized and replayed through the normal M7 report
pipeline, so benchmark output never becomes evidence or affects dispatch.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from dswarm.swarm.energy import EnergyConfig
from dswarm.swarm.energy_report import (
    EnergyReport,
    RunEnergyEstimate,
    build_report,
    build_run_estimate,
)
from dswarm.swarm.energy_sidecar import EnergyTraceSink

BENCHMARK_KIND = "m7_offline_scheduling_reorder_estimate"


@dataclass(frozen=True)
class EnergyBenchmarkCase:
    """One independently runnable offline benchmark case."""

    run_id: str
    challenge_id: str
    run_root: str | Path
    swarm_factory: Callable[[EnergyTraceSink], Any]


@dataclass(frozen=True)
class EnergyBenchmarkSuite:
    """Factory return value consumed by the standalone benchmark CLI."""

    cases: Sequence[EnergyBenchmarkCase]
    config: EnergyConfig
    top_k: int = 3


@dataclass
class EnergyBenchmarkCaseResult:
    """Execution and replay outcome for one benchmark case."""

    run_id: str
    challenge_id: str
    execution_status: str
    error: str
    finalized: bool
    estimate: RunEnergyEstimate


@dataclass
class EnergyBenchmarkResult:
    """All case outcomes plus the aggregate offline M7 report."""

    kind: str
    cases: list[EnergyBenchmarkCaseResult]
    report: EnergyReport


def _exclude_estimate(estimate: RunEnergyEstimate, reason: str) -> None:
    """Make an execution-level failure ineligible for aggregate statistics."""

    if estimate.dataset_status != "corrupt":
        estimate.dataset_status = "incomplete"
    if reason not in estimate.exclusion_reasons:
        estimate.exclusion_reasons.append(reason)


async def run_energy_benchmark(
    cases: Sequence[EnergyBenchmarkCase],
    *,
    config: EnergyConfig,
    top_k: int = 3,
) -> EnergyBenchmarkResult:
    """Run cases sequentially, finalize each sidecar, and build one report.

    Ordinary case failures are recorded and do not stop later cases.  Base
    exceptions such as cancellation and ``SystemExit`` still propagate, while
    the ``finally`` block gives the current sidecar its normal finalization
    attempt first.
    """

    results: list[EnergyBenchmarkCaseResult] = []
    for case in cases:
        sink = EnergyTraceSink(
            case.run_root,
            run_id=case.run_id,
            challenge_id=case.challenge_id,
            enabled=True,
        )
        execution_status = "completed"
        error = ""
        finalized = False
        try:
            swarm = case.swarm_factory(sink)
            await swarm.run()
        except Exception as exc:
            execution_status = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            finalized = sink.finalize()

        estimate = build_run_estimate(
            case.run_root,
            run_id=case.run_id,
            config=config,
            top_k=top_k,
        )
        if execution_status == "error":
            _exclude_estimate(estimate, "execution_error")
        if not finalized:
            _exclude_estimate(estimate, "finalize_failed")
        results.append(EnergyBenchmarkCaseResult(
            run_id=case.run_id,
            challenge_id=case.challenge_id,
            execution_status=execution_status,
            error=error,
            finalized=finalized,
            estimate=estimate,
        ))

    report = build_report([case.estimate for case in results])
    return EnergyBenchmarkResult(
        kind=BENCHMARK_KIND,
        cases=results,
        report=report,
    )


def benchmark_result_json(result: EnergyBenchmarkResult) -> str:
    """Return deterministic, UTF-8-safe JSON for storage or stdout."""

    return json.dumps(
        asdict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
