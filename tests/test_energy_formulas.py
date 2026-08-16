"""M7-1 formula and ordering tests (docs/10 items 1-22, 69-73)."""

from __future__ import annotations

import math

import pytest

from dswarm.swarm import energy
from dswarm.swarm.energy import (
    EnergyConfig,
    EnergyDecision,
    EnergyObservationSnapshot,
    DeadEndObservationSnapshot,
    decision_id_for,
    planner_baseline_order,
    energy_order,
    reorder_decisions,
    route_energies,
)


def _obs(fact_seq=1, route="route:a", confidence=0.8, verified=True,
         witness="trace", artifact_id="", state="verified", retired=False,
         energy_origin_ts=1000.0, fact_origin_ts=1000.0,
         correlation_basis_hash="basis-1", eligible=True):
    return EnergyObservationSnapshot(
        fact_seq=fact_seq, fact_origin_ts=fact_origin_ts,
        energy_origin_ts=energy_origin_ts, route_hash=route,
        lineage="unattributed", lineage_reason="no_producer",
        inherited_intent_ids=(), state=state, retired=retired,
        verified=verified, base_verified=verified, confidence=confidence,
        witness=witness, artifact_id=artifact_id, source="out", actor="w1",
        correlation_kind="artifact" if artifact_id else "fallback",
        correlation_basis_hash=correlation_basis_hash,
        eligible_for_energy=eligible, exclusion_reason="")


def _dead(intent_id="i1", route="route:a", result_seq=5, concluded_ts=1000.0,
          result="dead_end", eligible=True):
    return DeadEndObservationSnapshot(
        intent_id=intent_id, route_hash=route, result_seq=result_seq,
        concluded_ts=concluded_ts, result=result, genuine_giveup=True,
        eligible_for_energy=eligible, exclusion_reason="",
        conclusion_event_count=1, ignored_stale_conclusion_count=0)


def _decision(trace_id="t", index=0, intent_id=None, route="route:a",
              lane="ordinary", priority=0.5, scale="planner"):
    intent_id = intent_id if intent_id is not None else f"i{index}"
    return EnergyDecision(
        decision_id=decision_id_for("r1", trace_id, index, intent_id, "reason"),
        trace_id=trace_id, reason_cycle_id="reason-1", intent_id=intent_id,
        route_hash=route, worker_lane=lane, priority=float(priority),
        normalized_priority=float(priority), priority_scale=scale,
        original_index=index, decision_source="reason")


CFG = EnergyConfig({"verified_witness": 1.0, "verified": 0.8,
                    "candidate": 0.5})


# ---------------------------------------------------------------- 1-5 config

def test_1_config_copies_and_proxies():
    raw = {"verified": 1.0, "candidate": 0.5, "verified_witness": 1.0}
    cfg = EnergyConfig(raw)
    raw["verified"] = 0.0
    assert cfg.weights["verified"] == 1.0
    with pytest.raises(TypeError):
        cfg.weights["verified"] = 0.0


def test_2_unknown_weight_key_rejected():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5,
                      "verified_witness": 1.0, "extra": 1.0})


def test_3_missing_weight_key_rejected():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5})


def test_4_non_finite_weight_rejected():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": float("nan"), "candidate": 0.5,
                      "verified_witness": 1.0})
    with pytest.raises(ValueError):
        EnergyConfig({"verified": float("inf"), "candidate": 0.5,
                      "verified_witness": 1.0})


def test_5_domain_validation():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5,
                      "verified_witness": 1.0}, tau=0.0)
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5,
                      "verified_witness": 1.0}, dead_penalty=1.5)
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5,
                      "verified_witness": 1.0}, dead_tau=-1.0)


# ---------------------------------------------------------------- 6-9 filters

def test_6_confidence_clamped_to_unit():
    out = route_energies([_obs(confidence=7.0)], [], CFG, as_of_ts=1000.0)
    assert out["route:a"].positive <= 1.0
    assert out["route:a"].positive > 0.9


def test_7_age_floors_at_zero():
    # origin in the future relative to as_of -> age clamped to 0 (full weight)
    future = _obs(energy_origin_ts=2000.0)
    now = _obs(energy_origin_ts=1000.0)
    aged = _obs(energy_origin_ts=800.0)
    out_future = route_energies([future], [], CFG, as_of_ts=1000.0)
    out_now = route_energies([now], [], CFG, as_of_ts=1000.0)
    out_aged = route_energies([aged], [], CFG, as_of_ts=1000.0)
    # future origin saturates to age 0 == now origin (no negative age)
    assert out_future["route:a"].positive == out_now["route:a"].positive
    # aged decays below the saturated value
    assert out_aged["route:a"].positive < out_now["route:a"].positive


def test_8_as_of_must_be_finite_and_ts_never_membership():
    with pytest.raises(ValueError):
        route_energies([], [], CFG, as_of_ts=float("nan"))
    # a "future ts" observation is NOT excluded: membership is seq-based and
    # happens at capture; the formula treats ts only as decay input.
    obs = _obs(fact_origin_ts=1.0, energy_origin_ts=999999.0)
    out = route_energies([obs], [], CFG, as_of_ts=1000.0)
    assert "route:a" in out


def test_9_ineligible_and_empty_route_excluded():
    bad = _obs(eligible=False)
    empty = _obs(route="", eligible=True)
    good = _obs()
    out = route_energies([bad, empty, good], [], CFG, as_of_ts=1000.0)
    assert out["route:a"].raw_fact_count == 1


# ------------------------------------------------------------------ 10 tier

def test_10_tier_precedence():
    vw = _obs(fact_seq=1, verified=True, witness="trace")
    v = _obs(fact_seq=2, verified=True, witness="")
    c = _obs(fact_seq=3, verified=False, state="candidate")
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 0.5,
                        "candidate": 0.25})
    out = route_energies([vw], [], cfg, as_of_ts=1000.0)
    high = out["route:a"].positive
    out2 = route_energies([v], [], cfg, as_of_ts=1000.0)
    mid = out2["route:a"].positive
    out3 = route_energies([c], [], cfg, as_of_ts=1000.0)
    low = out3["route:a"].positive
    assert high > mid > low


# ---------------------------------------------------------------- 11-14 math

def test_11_raw_score_then_decay():
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 1.0,
                        "candidate": 1.0}, tau=100.0)
    fresh = _obs(fact_seq=1, confidence=0.5, energy_origin_ts=1000.0)
    aged = _obs(fact_seq=2, confidence=0.5, energy_origin_ts=900.0)
    out = route_energies([fresh], [], cfg, as_of_ts=1000.0)
    out_aged = route_energies([aged], [], cfg, as_of_ts=1000.0)
    expected = 0.5 * math.exp(-100.0 / 100.0)
    assert math.isclose(out_aged["route:a"].positive, expected, rel_tol=1e-9)
    assert out["route:a"].positive == pytest.approx(0.5)


def test_12_correlation_group_max_and_cross_group_product():
    # two observations same basis: max inside the group
    same = [_obs(fact_seq=1, confidence=0.2, correlation_basis_hash="b"),
            _obs(fact_seq=2, confidence=0.8, correlation_basis_hash="b")]
    out = route_energies(same, [], CFG, as_of_ts=1000.0)
    assert out["route:a"].correlation_group_count == 1
    assert math.isclose(out["route:a"].positive, 1.0 * 0.8, rel_tol=1e-9)
    # two different groups: 1 - (1-g1)(1-g2)
    two = [_obs(fact_seq=1, confidence=0.5, correlation_basis_hash="b1"),
           _obs(fact_seq=2, confidence=0.5, correlation_basis_hash="b2")]
    out2 = route_energies(two, [], CFG, as_of_ts=1000.0)
    assert out2["route:a"].correlation_group_count == 2
    assert math.isclose(out2["route:a"].positive, 1 - 0.5 * 0.5, rel_tol=1e-9)


def test_13_dead_penalty_max_merge_never_sums():
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 1.0,
                        "candidate": 1.0}, dead_penalty=0.5, dead_tau=100.0)
    obs = [_obs(fact_seq=1, confidence=1.0)]
    one = route_energies(obs, [_dead(concluded_ts=1000.0)], cfg,
                         as_of_ts=1000.0)
    two = route_energies(
        obs,
        [_dead(concluded_ts=1000.0), _dead(concluded_ts=950.0, result_seq=6)],
        cfg, as_of_ts=1000.0)
    assert one["route:a"].penalty == pytest.approx(0.5)
    # second (weaker, older) dead-end must not increase the penalty
    assert two["route:a"].penalty == pytest.approx(0.5)


def test_14_energy_clamped_non_negative():
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 1.0,
                        "candidate": 1.0}, dead_penalty=1.0, dead_tau=100.0)
    out = route_energies([_obs(fact_seq=1, confidence=0.1)],
                         [_dead(concluded_ts=1000.0)], cfg, as_of_ts=1000.0)
    assert out["route:a"].energy == 0.0
    assert out["route:a"].energy >= 0.0


# ---------------------------------------------------------------- 15/16 rules

def test_15_flag_captured_is_label_only():
    out = route_energies([_obs()], [], CFG, as_of_ts=1000.0)
    assert out["route:a"].flag_captured is False


def test_16_participation_rules():
    retired = _obs(fact_seq=1, retired=True)
    challenged = _obs(fact_seq=2, state="challenged", verified=False,
                      confidence=0.4)
    good = _obs(fact_seq=3)
    out = route_energies([retired, challenged, good], [], CFG,
                         as_of_ts=1000.0)
    assert out["route:a"].raw_fact_count == 2  # challenged in census
    # challenged contributes nothing: positive comes only from `good`
    assert out["route:a"].positive == pytest.approx(0.8)


def test_16_standalone_dead_end_is_audit_only():
    out = route_energies([], [_dead(route="route:z")], CFG, as_of_ts=1000.0)
    assert out["route:z"].eligible is False
    assert out["route:z"].energy == 0.0


# ---------------------------------------------------------------- 17-22 sort

def test_17_exact_equal_group_uses_normalized_priority():
    a = _decision(index=0, priority=0.5)
    b = _decision(index=1, priority=0.5, route="route:b")
    energies = {"route:b": type("E", (), {"energy": 0.9})()}
    result = reorder_decisions([a, b], enabled=True,
                               energy_supplier=lambda: energies)
    assert result[0].original_index == 1  # higher energy route first in group


def test_18_and_71_three_orderings():
    decisions = [
        _decision(index=0, priority=0.3, route="route:a"),
        _decision(index=1, priority=0.9, route="route:b"),
        _decision(index=2, priority=0.5, route="route:c"),
    ]
    production = list(decisions)
    baseline = planner_baseline_order(decisions)
    assert [d.original_index for d in baseline] == [1, 2, 0]  # -priority DESC
    energies = {"route:a": type("E", (), {"energy": 0.5})(),
                "route:b": type("E", (), {"energy": 0.2})(),
                "route:c": type("E", (), {"energy": 0.7})()}
    ordered = energy_order(decisions, energies)
    assert ordered == baseline  # different priorities: energy cannot reorder
    # energy only within exact-equal groups
    same_prio = [
        _decision(index=0, priority=0.5, route="route:a"),
        _decision(index=1, priority=0.5, route="route:b"),
    ]
    same_energies = {"route:a": type("E", (), {"energy": 0.2})(),
                     "route:b": type("E", (), {"energy": 0.7})()}
    ordered2 = energy_order(same_prio, same_energies)
    assert [d.original_index for d in ordered2] == [1, 0]


def test_19_energy_key_shape():
    decisions = [
        _decision(index=0, priority=0.5, route="route:a"),
        _decision(index=1, priority=0.5, route="route:b"),
    ]
    energies = {"route:a": type("E", (), {"energy": 0.1})(),
                "route:b": type("E", (), {"energy": 0.9})()}
    out = energy_order(decisions, energies)
    assert [d.route_hash for d in out] == ["route:b", "route:a"]


def test_20_unobserved_route_and_cold_start_fall_back_to_baseline():
    decisions = [_decision(index=0, priority=0.5, route="route:x"),
                 _decision(index=1, priority=0.5, route="route:y")]
    baseline = planner_baseline_order(decisions)
    out = reorder_decisions(decisions, enabled=True,
                            energy_supplier=lambda: {})
    assert out == baseline  # cold start: full-zero energies
    energies = {"route:x": type("E", (), {"energy": 0.9})()}
    out2 = energy_order(decisions, energies)
    # route:y has no energy -> 0 -> x first
    assert [d.route_hash for d in out2] == ["route:x", "route:y"]


def test_21_lane_and_scale_ranks_hold_globally():
    decisions = [
        _decision(index=0, lane="ordinary", priority=1.0),
        _decision(index=1, lane="review", priority=0.0),
    ]
    baseline = planner_baseline_order(decisions)
    assert [d.original_index for d in baseline] == [0, 1]  # ordinary first


def test_22_and_73_disabled_returns_production_order_no_supplier():
    decisions = [_decision(index=1, priority=0.9),
                 _decision(index=0, priority=0.1)]
    called = []
    out = reorder_decisions(decisions, enabled=False,
                            energy_supplier=lambda: called.append(1) or {})
    assert called == []  # supplier strictly zero calls
    assert [d.original_index for d in out] == [1, 0]  # input order preserved


# ---------------------------------------------------------------- 69/70 norms

def test_69_normalized_priority_frozen_at_construction():
    from dswarm.swarm.priority import normalize_priority
    assert normalize_priority(None) == 0.0
    assert normalize_priority(True) == 0.0
    assert normalize_priority("bogus") == 0.0
    assert normalize_priority(float("nan")) == 0.0
    assert normalize_priority(float("inf")) == 0.0
    assert normalize_priority("high") == 50.0
    assert normalize_priority(0.25) == 0.25
    # EnergyDecision carries the frozen value; energy.py reuses the helper
    d = _decision(priority=0.5)
    assert d.normalized_priority == normalize_priority(0.5)


def test_70_exact_equal_is_ieee_float_and_no_second_norm():
    import inspect
    src = inspect.getsource(energy)
    assert "def normalize_priority" not in src  # no re-implementation
    a = _decision(index=0, priority=0.5)
    b = _decision(index=1, priority=0.5, route="route:b")
    assert a.normalized_priority == b.normalized_priority  # IEEE float ==


def test_72_attribution_split_between_two_segments():
    # production -> planner_baseline is context; planner_baseline -> energy is
    # the attributable segment. Compute both displacements separately.
    decisions = [
        _decision(index=0, priority=0.2, route="route:a"),
        _decision(index=1, priority=0.8, route="route:b"),
    ]
    production = [d.original_index for d in decisions]
    baseline = [d.original_index for d in planner_baseline_order(decisions)]
    energies = {"route:a": type("E", (), {"energy": 0.9})(),
                "route:b": type("E", (), {"energy": 0.1})()}
    ordered = [d.original_index for d in energy_order(decisions, energies)]
    assert production != baseline  # production->planner segment moved
    assert baseline == ordered      # planner->energy segment: no change
    # the energy-attributable displacement is zero in this case
    energy_displacement = sum(
        1 for i, j in zip(baseline, ordered) if i != j)
    assert energy_displacement == 0
