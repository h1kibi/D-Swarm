"""⚠️ EXPERIMENTAL OFFLINE RESEARCH FRAMEWORK ONLY ⚠️

Operator-local harness for the offline-only M8 Advisor experiment.

WARNING: This module is NOT wired into production. Production Advisor remains
permanently No-Go per docs/00-architecture-spec.md §4.6 (contamination risk).

FOR RESEARCH USE ONLY. See docs/10 §M8 for experiment protocol.

---

The harness validates every path and fixture identity before creating local
artifacts. It never loads production graph/session state and never serializes
fixtures, planner requests, planner output, hidden references, or exceptions.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from hashlib import blake2b
import json
import math
from pathlib import Path
from typing import Any, Literal, Sequence

from dswarm.swarm.advisor_experiment import AdvisorFixture
from dswarm.swarm.advisor_report import (
    AdvisorAggregateReport,
    AdvisorCaseEstimate,
    build_advisor_report,
    build_case_estimate,
    build_missing_trace_estimate,
)
from dswarm.swarm.advisor_runner import (
    AdvisorIsolationFailure,
    PlannerFactory,
    run_advisor_case,
)
from dswarm.swarm.advisor_sidecar import advisor_trace_path, fold_advisor_trace


_BOOTSTRAP_SEED = 20260816
_BOOTSTRAP_SAMPLES = 2000
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CASE_LOCAL_CODES = {
    "sidecar_unavailable",
    "advisor_writer_busy",
    "existing_trace_incomplete",
    "existing_trace_corrupt",
    "existing_trace_partial",
    "existing_trace_identity_mismatch",
}


class AdvisorSuiteConstructionError(ValueError):
    """Fixed-code suite validation failure with no sensitive payload."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkCase:
    case_root: str | Path
    fixture: AdvisorFixture
    planner_factory: PlannerFactory
    timeout_s: float = 180.0
    cleanup_timeout_s: float = 5.0


@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkSuite:
    artifact_root: str | Path
    cases: Sequence[AdvisorBenchmarkCase]
    bootstrap_seed: int = _BOOTSTRAP_SEED
    bootstrap_samples: int = _BOOTSTRAP_SAMPLES

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))


@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkCaseResult:
    fixture_id: str
    benchmark_run_id: str
    status: Literal["reported", "case_local_failure"]
    failure_code: str
    estimate: AdvisorCaseEstimate

    def __post_init__(self) -> None:
        if self.status not in {"reported", "case_local_failure"}:
            raise ValueError("invalid case status")
        if (self.status == "reported") != (self.failure_code == ""):
            raise ValueError("invalid case failure code")
        if self.failure_code and self.failure_code not in _CASE_LOCAL_CODES:
            raise ValueError("invalid case failure code")
        if not isinstance(self.estimate, AdvisorCaseEstimate):
            raise ValueError("invalid case estimate")


@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkResult:
    kind: Literal["m8_advisor_benchmark_result"]
    declared_case_count: int
    reported_case_count: int
    case_local_failure_count: int
    case_results: tuple[AdvisorBenchmarkCaseResult, ...]
    report: AdvisorAggregateReport

    def __post_init__(self) -> None:
        if self.kind != "m8_advisor_benchmark_result":
            raise ValueError("invalid benchmark result kind")
        results = tuple(self.case_results)
        object.__setattr__(self, "case_results", results)
        reported = sum(item.status == "reported" for item in results)
        failed = sum(item.status == "case_local_failure" for item in results)
        if (
            self.declared_case_count != len(results)
            or self.reported_case_count != reported
            or self.case_local_failure_count != failed
            or reported + failed != len(results)
            or self.report.total_cases != len(results)
        ):
            raise ValueError("invalid benchmark result counts")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _summary_digest(summary: str) -> str:
    encoded = _canonical_json(["m8-summary", str(summary)])
    return f"m8-summary::{blake2b(encoded, digest_size=16).hexdigest()}"


def _fail(code: str) -> None:
    raise AdvisorSuiteConstructionError(code)


def _strict_descendant(candidate: Path, root: Path) -> bool:
    return candidate != root and candidate.is_relative_to(root)


def _resolve(value: str | Path, code: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        _fail(code)
    raise AssertionError("unreachable")


def _validate_positive_finite(value: object, code: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        _fail(code)


def _validate_suite(
    suite: AdvisorBenchmarkSuite,
) -> tuple[Path, tuple[tuple[AdvisorBenchmarkCase, Path], ...]]:
    if not isinstance(suite, AdvisorBenchmarkSuite):
        _fail("invalid_suite_type")
    cases = tuple(suite.cases)
    if type(suite.bootstrap_seed) is not int or type(suite.bootstrap_samples) is not int:
        _fail("invalid_bootstrap_settings")
    if (
        suite.bootstrap_seed != _BOOTSTRAP_SEED
        or suite.bootstrap_samples != _BOOTSTRAP_SAMPLES
    ):
        _fail("invalid_bootstrap_settings")

    artifact_root = _resolve(suite.artifact_root, "artifact_root_not_allowed")
    allowed_roots = (
        (_REPO_ROOT / "eval_runs" / "m8-advisor").resolve(strict=False),
        (_REPO_ROOT / "sessions").resolve(strict=False),
    )
    if not any(_strict_descendant(artifact_root, root) for root in allowed_roots):
        _fail("artifact_root_not_allowed")

    fixture_ids: set[str] = set()
    source_keys: set[tuple[str, str, int]] = set()
    case_roots: set[Path] = set()
    validated: list[tuple[AdvisorBenchmarkCase, Path]] = []
    for case in cases:
        if not isinstance(case, AdvisorBenchmarkCase):
            _fail("invalid_case_type")
        fixture = case.fixture
        if not isinstance(fixture, AdvisorFixture):
            _fail("invalid_fixture_type")
        if fixture.fixture_id in fixture_ids:
            _fail("duplicate_fixture_id")
        fixture_ids.add(fixture.fixture_id)

        source_key = (
            fixture.benchmark_run_id,
            fixture.challenge_id,
            fixture.source_event_seq,
        )
        if source_key in source_keys:
            _fail("source_identity_conflict")
        source_keys.add(source_key)
        if fixture.summary_digest != _summary_digest(fixture.graph_summary):
            _fail("invalid_summary_digest")
        _validate_positive_finite(case.timeout_s, "invalid_timeout")
        _validate_positive_finite(case.cleanup_timeout_s, "invalid_cleanup_timeout")
        if not callable(case.planner_factory):
            _fail("invalid_planner_factory")

        case_root = _resolve(case.case_root, "case_root_not_allowed")
        if not _strict_descendant(case_root, artifact_root):
            _fail("case_root_not_allowed")
        if case_root in case_roots:
            _fail("duplicate_case_root")
        case_roots.add(case_root)
        validated.append((case, case_root))
    return artifact_root, tuple(validated)


def _lock_path(case_root: Path) -> Path:
    return advisor_trace_path(case_root).parent / "advisor-experiment.writer.lock"


def _synthetic_case_result(
    fixture: AdvisorFixture,
    code: str,
    *, corrupt: bool = False,
) -> AdvisorBenchmarkCaseResult:
    estimate = build_missing_trace_estimate(fixture, code)
    if corrupt:
        estimate = replace(estimate, dataset_status="corrupt")
    return AdvisorBenchmarkCaseResult(
        fixture_id=fixture.fixture_id,
        benchmark_run_id=fixture.benchmark_run_id,
        status="case_local_failure",
        failure_code=code,
        estimate=estimate,
    )


def _classify_existing(fixture: AdvisorFixture, case_root: Path):
    fold = fold_advisor_trace(case_root)
    if "partial_tail" in fold.reasons:
        return _synthetic_case_result(fixture, "existing_trace_partial")
    identity_match = (
        fold.fixture_id == fixture.fixture_id
        and fold.summary_digest == fixture.summary_digest
        and fold.benchmark_run_id == fixture.benchmark_run_id
    )
    if fold.events and not identity_match:
        return _synthetic_case_result(
            fixture, "existing_trace_identity_mismatch", corrupt=True,
        )
    if fold.dataset_status == "corrupt":
        return _synthetic_case_result(fixture, "existing_trace_corrupt", corrupt=True)
    if not fold.complete:
        return _synthetic_case_result(fixture, "existing_trace_incomplete")
    estimate = build_case_estimate(fixture, case_root)
    if estimate.dataset_status == "corrupt" and "identity_mismatch" in estimate.exclusion_reasons:
        return _synthetic_case_result(
            fixture, "existing_trace_identity_mismatch", corrupt=True,
        )
    if estimate.dataset_status != "clean":
        code = (
            "existing_trace_corrupt"
            if estimate.dataset_status == "corrupt"
            else "existing_trace_incomplete"
        )
        return _synthetic_case_result(
            fixture, code, corrupt=estimate.dataset_status == "corrupt",
        )
    return AdvisorBenchmarkCaseResult(
        fixture_id=fixture.fixture_id,
        benchmark_run_id=fixture.benchmark_run_id,
        status="reported",
        failure_code="",
        estimate=estimate,
    )


async def run_advisor_benchmark(
    suite: AdvisorBenchmarkSuite,
) -> AdvisorBenchmarkResult:
    """Validate and run a sequential, offline Advisor benchmark suite."""

    _artifact_root, validated = _validate_suite(suite)
    results: list[AdvisorBenchmarkCaseResult] = []
    for case, case_root in validated:
        fixture = case.fixture
        if _lock_path(case_root).exists():
            results.append(_synthetic_case_result(fixture, "advisor_writer_busy"))
            continue

        trace_path = advisor_trace_path(case_root)
        if trace_path.exists() and trace_path.stat().st_size:
            results.append(_classify_existing(fixture, case_root))
            continue

        outcome = await run_advisor_case(
            fixture,
            case_root=str(case_root),
            planner_factory=case.planner_factory,
            timeout_s=case.timeout_s,
            cleanup_timeout_s=case.cleanup_timeout_s,
        )
        if not trace_path.exists() or not trace_path.stat().st_size:
            results.append(_synthetic_case_result(fixture, "sidecar_unavailable"))
            continue

        if outcome.failure_code == "sensitive_output_redacted":
            estimate = build_case_estimate(fixture, case_root)
            results.append(AdvisorBenchmarkCaseResult(
                fixture_id=fixture.fixture_id,
                benchmark_run_id=fixture.benchmark_run_id,
                status="reported",
                failure_code="",
                estimate=estimate,
            ))
            continue
        results.append(_classify_existing(fixture, case_root))

    estimates = tuple(item.estimate for item in results)
    report = build_advisor_report(estimates)
    reported = sum(item.status == "reported" for item in results)
    return AdvisorBenchmarkResult(
        kind="m8_advisor_benchmark_result",
        declared_case_count=len(results),
        reported_case_count=reported,
        case_local_failure_count=len(results) - reported,
        case_results=tuple(results),
        report=report,
    )


def benchmark_result_json(result: AdvisorBenchmarkResult) -> str:
    if not isinstance(result, AdvisorBenchmarkResult):
        raise ValueError("invalid benchmark result")
    return json.dumps(
        asdict(result),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "AdvisorBenchmarkCase",
    "AdvisorBenchmarkCaseResult",
    "AdvisorBenchmarkResult",
    "AdvisorBenchmarkSuite",
    "AdvisorIsolationFailure",
    "AdvisorSuiteConstructionError",
    "benchmark_result_json",
    "run_advisor_benchmark",
]
