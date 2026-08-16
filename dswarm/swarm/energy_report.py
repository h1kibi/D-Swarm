"""M7-2 offline replay and paired run-level delta bootstrap (docs/10).

Discipline (docs/10 M7 Contract v9.2):

- "offline scheduling reorder estimate" — a static reorder analysis, NOT a
  causal ablation. Only the planner_baseline -> energy segment is attributed
  to energy; production -> planner_baseline is context.
- N/A discipline: flag latency / tokens saved / worker starts saved /
  solve-rate delta / race outcome / counterfactual cost are ALWAYS "N/A".
- Paired bootstrap: per-run deltas are resampled as whole runs (cycles within
  a run are never resampled independently), seed 20260816, 2000 resamples,
  95% percentile CI.
- Run qualification: complete datasets only; incomplete/corrupt runs go to
  coverage reporting only. runs<5 -> N/A, 5-19 -> exploratory, >=20 -> CI.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from dswarm.swarm.energy import (
    EnergyConfig,
    EnergyDecision,
    EnergyObservationSnapshot,
    DeadEndObservationSnapshot,
    GraphCycleSnapshot,
    CycleTrace,
    SCHEMA_VERSION,
    energy_order,
    planner_baseline_order,
    route_energies,
    cycle_trace_from_row,
)
from dswarm.swarm.energy_sidecar import (
    MANIFEST_NAME,
    EnergyDatasetFold,
    EnergyTraceSink,
    energy_segment_paths,
)

BOOTSTRAP_SEED = 20260816
BOOTSTRAP_RESAMPLES = 2000
CI_PERCENTILE = 95.0

_NA_FIELDS = (
    "flag_latency",
    "tokens_saved",
    "worker_starts_saved",
    "solve_rate_delta",
    "race_outcome",
    "counterfactual_cost",
)


@dataclass(frozen=True)
class CycleReplay:
    """One complete cycle replayed from the sidecar dataset."""

    trace_id: str
    decisions: tuple[EnergyDecision, ...]
    production_order: tuple[EnergyDecision, ...]
    planner_baseline_order: tuple[EnergyDecision, ...]
    energy_order: tuple[EnergyDecision, ...]
    energies: dict[str, Any]
    top_k_energy_gain: float          # top-k energy delta: energy order - baseline
    displacement_production_to_planner: int
    displacement_planner_to_energy: int
    route_churn: int                  # decisions whose route position changed
    rank_corr: float | None           # spearman(baseline ranks, energy ranks)


@dataclass
class RunEnergyEstimate:
    run_id: str
    dataset_status: str               # complete | incomplete | corrupt | missing
    exclusion_reasons: list[str] = field(default_factory=list)
    cycles_started: int = 0
    cycles_written: int = 0
    cycles_used: int = 0
    zero_change_cycles: int = 0
    mean_top_k_energy_gain: float | None = None
    mean_displacement_planner_to_energy: float | None = None
    mean_displacement_production_to_planner: float | None = None
    mean_route_churn: float | None = None
    mean_rank_corr: float | None = None
    top_k_routes_baseline: list[str] = field(default_factory=list)
    top_k_routes_energy: list[str] = field(default_factory=list)
    reorder_counts: list[int] = field(default_factory=list)
    n_a: dict[str, str] = field(default_factory=lambda: {
        name: "N/A" for name in _NA_FIELDS})


def _spearman(a: Sequence[int], b: Sequence[int]) -> float:
    """Spearman rank correlation with average ranks for ties."""
    n = len(a)
    if n < 2:
        return 0.0

    def ranks(values: Sequence[int]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(list(a)), ranks(list(b))
    mean_a = (n - 1) / 2.0
    cov = sum((ra[i] - mean_a) * (rb[i] - mean_a) for i in range(n))
    var = sum((ra[i] - mean_a) ** 2 for i in range(n))
    if var == 0:
        return 0.0
    return cov / var


def replay_dataset(run_root: str | Path, *, run_id: str,
                   config: EnergyConfig, top_k: int = 3) -> tuple[
        list[CycleReplay], EnergyDatasetFold]:
    """Replay every complete cycle_trace record in physical append order.

    Reading is strictly side-effect free: the fold uses the read-only path
    (no resume guard, no manifest mutation)."""
    fold = EnergyTraceSink.readonly_fold(run_root, run_id=run_id)
    segments = energy_segment_paths(run_root)
    replays: list[CycleReplay] = []
    for segment in segments:
        raw = segment.read_bytes()
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except Exception:
                continue  # fold already classifies; replay skips malformed
            if record.get("kind") != "cycle_trace":
                continue
            if not record.get("complete"):
                continue  # excluded stubs never enter the estimate
            try:
                trace = cycle_trace_from_row(record)
            except Exception:
                continue
            production = tuple(sorted(trace.decisions,
                                      key=lambda d: d.original_index))
            baseline = tuple(planner_baseline_order(production))
            energies = route_energies(
                trace.snapshot.observations, trace.snapshot.dead_ends,
                config, as_of_ts=trace.decision_ts)
            ordered = tuple(energy_order(production, energies))

            def gain(order: Sequence[EnergyDecision]) -> float:
                total = 0.0
                for d in list(order)[:top_k]:
                    route = energies.get(d.route_hash)
                    total += route.energy if route is not None else 0.0
                return total

            def displacement(a: Sequence[EnergyDecision],
                             b: Sequence[EnergyDecision]) -> int:
                pos = {d.decision_id: i for i, d in enumerate(a)}
                return sum(1 for i, d in enumerate(b)
                           if pos[d.decision_id] != i)

            churn = sum(
                1 for i, (d1, d2) in enumerate(zip(baseline, ordered))
                if d1.route_hash != d2.route_hash)
            base_ranks = [i for i, d in sorted(
                enumerate(baseline), key=lambda p: p[1].decision_id)]
            energy_ranks = [i for i, d in sorted(
                enumerate(ordered), key=lambda p: p[1].decision_id)]
            rank_corr = _spearman(base_ranks, energy_ranks) if baseline else None
            replays.append(CycleReplay(
                trace_id=trace.trace_id,
                decisions=production,
                production_order=production,
                planner_baseline_order=baseline,
                energy_order=ordered,
                energies=energies,
                top_k_energy_gain=gain(ordered) - gain(baseline),
                displacement_production_to_planner=displacement(
                    production, baseline),
                displacement_planner_to_energy=displacement(baseline, ordered),
                route_churn=churn,
                rank_corr=rank_corr,
            ))
    return replays, fold


def build_run_estimate(run_root: str | Path, *, run_id: str,
                       config: EnergyConfig, top_k: int = 3) -> RunEnergyEstimate:
    """One run's offline reorder estimate with dataset qualification."""
    qualification = EnergyTraceSink.readonly_qualification(
        run_root, run_id=run_id
    )
    fold = qualification.fold
    replays: list[CycleReplay] = []
    if qualification.status == "complete":
        replays, fold = replay_dataset(
            run_root, run_id=run_id, config=config, top_k=top_k
        )
    estimate = RunEnergyEstimate(
        run_id=run_id,
        dataset_status=qualification.status,
        exclusion_reasons=list(dict.fromkeys([
            *qualification.reasons, *fold.reasons,
            *(f"orphan:{t}" for t in fold.orphan_started),
        ])),
        cycles_started=fold.cycles_started,
        cycles_written=fold.cycles_written,
        cycles_used=len(replays),
        n_a={field_name: "N/A" for field_name in _NA_FIELDS},
    )
    if not replays:
        return estimate
    estimate.zero_change_cycles = sum(
        1 for r in replays if r.displacement_planner_to_energy == 0
        and r.route_churn == 0)
    estimate.mean_top_k_energy_gain = sum(
        r.top_k_energy_gain for r in replays) / len(replays)
    estimate.mean_displacement_planner_to_energy = sum(
        r.displacement_planner_to_energy for r in replays) / len(replays)
    estimate.mean_displacement_production_to_planner = sum(
        r.displacement_production_to_planner for r in replays) / len(replays)
    estimate.mean_route_churn = sum(r.route_churn for r in replays) / len(replays)
    rank_corrs = [r.rank_corr for r in replays if r.rank_corr is not None]
    estimate.mean_rank_corr = (sum(rank_corrs) / len(rank_corrs)
                               if rank_corrs else None)
    estimate.reorder_counts = [r.displacement_planner_to_energy
                               for r in replays]

    def top_routes(order_key: str) -> list[str]:
        seen: list[str] = []
        for r in replays:
            for d in getattr(r, order_key)[:top_k]:
                if d.route_hash not in seen:
                    seen.append(d.route_hash)
        return seen[:top_k]

    estimate.top_k_routes_baseline = top_routes("planner_baseline_order")
    estimate.top_k_routes_energy = top_routes("energy_order")
    return estimate


@dataclass
class EnergyReport:
    """Aggregate over runs: coverage, qualification, paired bootstrap CI."""

    run_estimates: list[RunEnergyEstimate]
    coverage: dict[str, float] = field(default_factory=dict)
    qualified_runs: int = 0
    zero_change_runs: int = 0
    tier: str = "N/A"                     # N/A | exploratory | CI
    bootstrap: dict[str, Any] = field(default_factory=dict)


def paired_bootstrap(run_estimates: Sequence[RunEnergyEstimate], *,
                     seed: int = BOOTSTRAP_SEED,
                     resamples: int = BOOTSTRAP_RESAMPLES,
                     percentile: float = CI_PERCENTILE) -> dict[str, Any]:
    """Run-level paired delta bootstrap: whole runs are the resampling unit."""
    qualified = [e for e in run_estimates
                 if e.dataset_status == "complete"
                 and e.mean_top_k_energy_gain is not None]
    deltas = [e.mean_top_k_energy_gain for e in qualified]
    n_runs = len(deltas)
    if n_runs < 5:
        return {"n_runs": n_runs, "lower": None, "upper": None, "mean": None,
                "resamples": 0, "seed": seed, "percentile": percentile}
    if n_runs < 20:
        return {"n_runs": n_runs, "lower": None, "upper": None,
                "mean": sum(deltas) / n_runs, "resamples": 0,
                "seed": seed, "percentile": percentile}
    rng = random.Random(seed)
    sample_means: list[float] = []
    for _ in range(resamples):
        picks = [deltas[rng.randrange(len(deltas))] for _ in deltas]
        sample_means.append(sum(picks) / len(picks))
    sample_means.sort()
    low_pct = (100.0 - percentile) / 2.0
    high_pct = 100.0 - low_pct
    lower = sample_means[max(0, int(resamples * low_pct / 100.0) - 1)]
    upper = sample_means[min(resamples - 1, int(resamples * high_pct / 100.0))]
    return {"n_runs": n_runs, "lower": lower, "upper": upper,
            "mean": sum(deltas) / len(deltas), "resamples": resamples,
            "seed": seed, "percentile": percentile}


def build_report(run_estimates: Sequence[RunEnergyEstimate], *,
                 seed: int = BOOTSTRAP_SEED,
                 resamples: int = BOOTSTRAP_RESAMPLES) -> EnergyReport:
    """Coverage split + run qualification tiers + bootstrap CI."""
    total = len(run_estimates)
    statuses = {"complete": 0, "incomplete": 0, "corrupt": 0, "missing": 0}
    for e in run_estimates:
        statuses[e.dataset_status] = statuses.get(e.dataset_status, 0) + 1
    coverage = {
        f"status_{key}": (value / total) if total else 0.0
        for key, value in statuses.items()}
    qualified = [e for e in run_estimates if e.dataset_status == "complete"]
    zero_change = sum(1 for e in qualified
                      if e.cycles_used > 0 and e.zero_change_cycles == e.cycles_used)
    tier = "N/A"
    if len(qualified) >= 20:
        tier = "CI"
    elif len(qualified) >= 5:
        tier = "exploratory"
    return EnergyReport(
        run_estimates=list(run_estimates),
        coverage=coverage,
        qualified_runs=len(qualified),
        zero_change_runs=zero_change,
        tier=tier,
        bootstrap=paired_bootstrap(run_estimates, seed=seed,
                                   resamples=resamples),
    )
