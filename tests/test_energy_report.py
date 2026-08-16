"""M7-2 offline replay + paired bootstrap tests (docs/10 items 40-44, 52,
117-118)."""

from __future__ import annotations

import json
import time

import pytest

from dswarm.swarm import energy
from dswarm.swarm.energy import (
    SCHEMA_VERSION,
    CycleTrace,
    EnergyConfig,
    EnergyDecision,
    EnergyObservationSnapshot,
    GraphCycleSnapshot,
    decision_id_for,
)
from dswarm.swarm.energy_report import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    CI_PERCENTILE,
    RunEnergyEstimate,
    build_report,
    build_run_estimate,
    paired_bootstrap,
)
from dswarm.swarm.energy_sidecar import EnergyTraceSink

CFG = EnergyConfig({"verified_witness": 1.0, "verified": 0.8,
                    "candidate": 0.5})


def _obs(fact_seq, route, confidence=0.8, basis=None, energy_origin_ts=1000.0):
    return EnergyObservationSnapshot(
        fact_seq=fact_seq, fact_origin_ts=1000.0,
        energy_origin_ts=energy_origin_ts, route_hash=route,
        lineage="unattributed", lineage_reason="no_producer",
        inherited_intent_ids=(), state="verified", retired=False,
        verified=True, base_verified=True, confidence=confidence,
        witness="trace", artifact_id="", source="out", actor="w1",
        correlation_kind="fallback",
        correlation_basis_hash=basis or f"basis-{fact_seq}",
        eligible_for_energy=True, exclusion_reason="")


def _decision(trace_id, index, route, priority=0.5):
    intent_id = f"i{index}"
    return EnergyDecision(
        decision_id=decision_id_for("r1", trace_id, index, intent_id, "reason"),
        trace_id=trace_id, reason_cycle_id="reason-1", intent_id=intent_id,
        route_hash=route, worker_lane="ordinary", priority=priority,
        normalized_priority=priority, priority_scale="planner",
        original_index=index, decision_source="reason")


def _write_cycle(sink: EnergyTraceSink, trace_id: str, routes,
                 observations=(), priorities=None):
    sink.start_cycle(trace_id, reason_cycle_id="reason-1", decision_ts=1000.0)
    priorities = priorities or [0.5] * len(routes)
    decisions = tuple(_decision(trace_id, i, route, priority=p)
                      for i, (route, p) in enumerate(zip(routes, priorities)))
    snapshot = GraphCycleSnapshot(
        graph_after_seq=0, observations=tuple(observations), dead_ends=(),
        complete=True, exclusion_reason="",
        observed_fact_count=len(observations),
        captured_fact_count=len(observations),
        stored_fact_count=len(observations))
    trace = CycleTrace(
        schema_version=SCHEMA_VERSION, trace_id=trace_id,
        reason_cycle_id="reason-1", decision_ts=1000.0,
        expected_decision_count=len(decisions), decisions=decisions,
        snapshot=snapshot, complete=True, exclusion_reason="",
        serialized_bytes=0, serialized_bytes_attempted=None)
    assert sink.write_trace(trace) is True


def _complete_run(tmp_path, sub, cycles):
    sink = EnergyTraceSink(tmp_path / sub, run_id=sub, challenge_id="t1",
                           enabled=True)
    for i, (routes, obs, prios) in enumerate(cycles):
        _write_cycle(sink, f"m7-cycle::{sub}::inst::{i}", routes,
                     observations=obs, priorities=prios)
    assert sink.finalize() is True
    assert sink.dataset_complete() is True
    return sink


# ------------------------------------------------------------------ 40/41/52

def test_40_and_52_paired_bootstrap_resamples_whole_runs():
    estimates = [
        RunEnergyEstimate(run_id=f"r{i}", dataset_status="complete",
                          mean_top_k_energy_gain=0.1 * (i + 1))
        for i in range(6)]
    result = paired_bootstrap(estimates)
    assert result["n_runs"] == 6
    assert result["resamples"] == BOOTSTRAP_RESAMPLES
    assert result["mean"] == pytest.approx(0.35)
    # CI brackets the population mean (percentile interval)
    assert result["lower"] <= result["mean"] <= result["upper"]


def test_41_bootstrap_parameters_fixed():
    assert BOOTSTRAP_SEED == 20260816
    assert BOOTSTRAP_RESAMPLES == 2000
    assert CI_PERCENTILE == 95.0
    estimates = [RunEnergyEstimate(run_id="r1", dataset_status="complete",
                                   mean_top_k_energy_gain=0.5)]
    a = paired_bootstrap(estimates)
    b = paired_bootstrap(estimates)
    assert a == b  # deterministic under the fixed seed


# ------------------------------------------------------------------ 42/43/44

def test_42_run_qualification_tiers_and_zero_change():
    complete = [RunEnergyEstimate(run_id=f"c{i}", dataset_status="complete",
                                  cycles_used=1, zero_change_cycles=1,
                                  mean_top_k_energy_gain=0.0)
                for i in range(20)]
    incomplete = [RunEnergyEstimate(run_id="bad1", dataset_status="incomplete"),
                  RunEnergyEstimate(run_id="bad2", dataset_status="corrupt")]
    report = build_report(complete + incomplete)
    assert report.qualified_runs == 20
    assert report.zero_change_runs == 20
    assert report.tier == "CI"
    assert report.bootstrap["n_runs"] == 20
    # incomplete/corrupt never enter the main CI
    assert all(e.dataset_status != "complete" for e in
               report.run_estimates[-2:])
    small = build_report(complete[:5])
    assert small.tier == "exploratory"
    tiny = build_report(complete[:4])
    assert tiny.tier == "N/A"
    assert tiny.bootstrap["n_runs"] == 4


def test_43_na_discipline_fields_are_always_na():
    estimate = RunEnergyEstimate(run_id="r1", dataset_status="complete")
    for field_name in ("flag_latency", "tokens_saved", "worker_starts_saved",
                       "solve_rate_delta", "race_outcome",
                       "counterfactual_cost"):
        assert estimate.n_a[field_name] == "N/A"


def test_44_coverage_ratios_sum_to_one():
    estimates = [RunEnergyEstimate(run_id="a", dataset_status="complete"),
                 RunEnergyEstimate(run_id="b", dataset_status="incomplete"),
                 RunEnergyEstimate(run_id="c", dataset_status="corrupt")]
    report = build_report(estimates)
    assert abs(sum(report.coverage.values()) - 1.0) < 1e-9
    assert report.coverage["status_complete"] == 1 / 3
    assert report.coverage["status_incomplete"] == 1 / 3
    assert report.coverage["status_corrupt"] == 1 / 3


# ------------------------------------------------------------------ 117/118

def test_117_report_is_deterministic(tmp_path):
    cycles = [
        (["route:a", "route:b"],
         [_obs(1, "route:a", 0.9), _obs(2, "route:b", 0.5)], None),
        (["route:a", "route:b"],
         [_obs(3, "route:a", 0.3), _obs(4, "route:b", 0.7)], None),
    ]
    _complete_run(tmp_path, "det", cycles)
    a = build_run_estimate(tmp_path / "det", run_id="det", config=CFG)
    b = build_run_estimate(tmp_path / "det", run_id="det", config=CFG)
    assert a == b
    assert a.dataset_status == "complete"
    assert a.cycles_used == 2


def test_118_energy_metrics_exclude_production_to_planner(tmp_path):
    # production order differs from planner baseline (unsorted priorities),
    # but zero energy => the energy-attributable segment must be exactly zero.
    cycles = [
        (["route:a", "route:b"], [], [0.2, 0.8]),
    ]
    _complete_run(tmp_path, "seg", cycles)
    estimate = build_run_estimate(tmp_path / "seg", run_id="seg", config=CFG)
    assert estimate.dataset_status == "complete"
    # production -> planner: a two-decision swap (both positions change)
    assert estimate.mean_displacement_production_to_planner == 2.0
    # planner -> energy: no energy available -> strictly zero
    assert estimate.mean_displacement_planner_to_energy == 0.0
    assert estimate.mean_top_k_energy_gain == 0.0
    assert estimate.zero_change_cycles == 1


def test_117_energy_reorder_shows_in_replay(tmp_path):
    # five equal-priority decisions; energy ascending a->e (0.1..0.5) reverses
    # the group. top-3 baseline {a,b,c} = 0.6; top-3 energy {e,d,c} = 1.2.
    cycles = [
        (["route:a", "route:b", "route:c", "route:d", "route:e"],
         [_obs(i + 1, f"route:{c}", 0.1 * (i + 1))
          for i, c in enumerate("abcde")], [0.5] * 5),
    ]
    _complete_run(tmp_path, "move", cycles)
    estimate = build_run_estimate(tmp_path / "move", run_id="move", config=CFG)
    assert estimate.dataset_status == "complete"
    # reversal: all but the middle element change position (c stays at 2)
    assert estimate.mean_displacement_planner_to_energy == 4.0
    assert estimate.mean_top_k_energy_gain == pytest.approx(0.6)
    assert estimate.zero_change_cycles == 0
