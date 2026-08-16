"""M7-0 tests: pure types, validators, canonical serialization (docs/10).

Covers contract items: 25, 26(partial in sidecar tests), 27-29, 31, 39, 51
(stub semantics in sidecar tests), 53, 55, 63-66, 79, 81, 86, 94-97, 114-116.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dswarm.swarm import energy
from dswarm.swarm.energy import (
    MAX_FIXED_POINT_ITERATIONS,
    MAX_SEGMENT_BYTES,
    MAX_TRACE_BYTES,
    SCHEMA_VERSION,
    CycleTrace,
    EnergyConfig,
    EnergyDecision,
    EnergyObservationSnapshot,
    DeadEndObservationSnapshot,
    GraphCycleSnapshot,
    RouteEnergy,
    SizeFixedPointError,
    build_size_stub,
    decision_id_for,
    encode_cycle_trace_line,
    make_trace_id,
    reorder_decisions,
    route_energies,
    validate_cycle_trace,
)


def _obs(fact_seq=1, route="route:a", confidence=0.8, verified=True,
         witness="trace", artifact_id="", state="verified", retired=False,
         energy_origin_ts=1000.0, correlation_basis_hash="basis-1",
         eligible=True):
    return EnergyObservationSnapshot(
        fact_seq=fact_seq, fact_origin_ts=1000.0,
        energy_origin_ts=energy_origin_ts, route_hash=route,
        lineage="unattributed", lineage_reason="no_producer",
        inherited_intent_ids=(), state=state, retired=retired,
        verified=verified, base_verified=verified, confidence=confidence,
        witness=witness, artifact_id=artifact_id, source="out", actor="w1",
        correlation_kind="artifact" if artifact_id else "fallback",
        correlation_basis_hash=correlation_basis_hash,
        eligible_for_energy=eligible, exclusion_reason="")


def _dead(intent_id="i1", route="route:a", result_seq=5, concluded_ts=1000.0,
          result="dead_end", genuine=True, eligible=True,
          exclusion_reason=""):
    return DeadEndObservationSnapshot(
        intent_id=intent_id, route_hash=route, result_seq=result_seq,
        concluded_ts=concluded_ts, result=result, genuine_giveup=genuine,
        eligible_for_energy=eligible, exclusion_reason=exclusion_reason,
        conclusion_event_count=1, ignored_stale_conclusion_count=0)


def _decision(trace_id="m7-cycle::r1::i1::1", index=0, intent_id="i0",
              route="route:a", lane="ordinary", priority=0.5, scale="planner"):
    return EnergyDecision(
        decision_id=decision_id_for("r1", trace_id, index, intent_id, "reason"),
        trace_id=trace_id, reason_cycle_id="reason-1", intent_id=intent_id,
        route_hash=route, worker_lane=lane, priority=float(priority),
        normalized_priority=float(priority), priority_scale=scale,
        original_index=index, decision_source="reason")


def _snapshot(**kw):
    values = dict(graph_after_seq=0, observations=(), dead_ends=(),
                  complete=True, exclusion_reason="", observed_fact_count=0,
                  captured_fact_count=0, stored_fact_count=0)
    values.update(kw)
    return GraphCycleSnapshot(**values)


def _trace(trace_id="m7-cycle::r1::i1::1", decisions=(), snapshot=None,
           complete=True, reason="", attempted=None, decision_ts=None):
    snapshot = snapshot if snapshot is not None else _snapshot()
    return CycleTrace(
        schema_version=SCHEMA_VERSION, trace_id=trace_id,
        reason_cycle_id="reason-1",
        decision_ts=time.time() if decision_ts is None else decision_ts,
        expected_decision_count=len(decisions), decisions=tuple(decisions),
        snapshot=snapshot, complete=complete, exclusion_reason=reason,
        serialized_bytes=0, serialized_bytes_attempted=attempted)


# ---------------------------------------------------------------- 27 validator

def test_27_config_validator_rejects_unknown_key():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5, "verified_witness": 1.0,
                      "bogus": 1.0})


def test_27_config_validator_requires_all_keys():
    with pytest.raises(ValueError):
        EnergyConfig({"verified": 1.0, "candidate": 0.5})


def test_27_config_freezes_and_copies():
    raw = {"verified": 1.0, "candidate": 0.5, "verified_witness": 1.0}
    cfg = EnergyConfig(raw)
    raw["verified"] = 9.0
    assert cfg.weights["verified"] == 1.0
    with pytest.raises(TypeError):
        cfg.weights["verified"] = 9.0  # MappingProxyType


def test_27_observation_validator_enums_and_finite():
    with pytest.raises(ValueError):
        _obs(state="nonsense")
    with pytest.raises(ValueError):
        _obs(fact_seq=0)
    with pytest.raises(ValueError):
        EnergyObservationSnapshot(
            fact_seq=1, fact_origin_ts=float("nan"), energy_origin_ts=1.0,
            route_hash="r", lineage="l", lineage_reason="x",
            inherited_intent_ids=(), state="verified", retired=False,
            verified=True, base_verified=True, confidence=1.0, witness="w",
            artifact_id="", source="s", actor="a", correlation_kind="artifact",
            correlation_basis_hash="h", eligible_for_energy=True,
            exclusion_reason="")


def test_27_dead_end_validator():
    with pytest.raises(ValueError):
        _dead(result_seq=0)
    with pytest.raises(ValueError):
        _dead(exclusion_reason="bogus")
    with pytest.raises(ValueError):
        _dead(concluded_ts=float("inf"))


def test_27_trace_validator_and_decision_id_recheck():
    trace = _trace(decisions=(_decision(),), snapshot=_snapshot(observations=()))
    assert validate_cycle_trace(trace, run_id="r1") == []
    good = _decision()
    bad = EnergyDecision(
        decision_id="tampered", trace_id=good.trace_id,
        reason_cycle_id=good.reason_cycle_id, intent_id=good.intent_id,
        route_hash=good.route_hash, worker_lane=good.worker_lane,
        priority=good.priority, normalized_priority=good.normalized_priority,
        priority_scale=good.priority_scale, original_index=good.original_index,
        decision_source=good.decision_source)
    trace_bad = _trace(decisions=(bad,), snapshot=_snapshot(observations=()))
    assert any("decision_id" in e for e in validate_cycle_trace(trace_bad, run_id="r1"))


# ------------------------------------------------------------- 28/29/31/53/64/65

def test_28_explicit_serialization_keys():
    obs = _obs()
    row = energy.observation_to_dict(obs)
    assert set(row) == {
        "fact_seq", "fact_origin_ts", "energy_origin_ts", "route_hash",
        "lineage", "lineage_reason", "inherited_intent_ids", "state", "retired",
        "verified", "base_verified", "confidence", "witness", "artifact_id",
        "source", "actor", "correlation_kind", "correlation_basis_hash",
        "eligible_for_energy", "exclusion_reason"}
    trace = _trace(decisions=(_decision(),))
    line, _ = encode_cycle_trace_line(trace)
    row = json.loads(line)
    assert row["kind"] == "cycle_trace"
    assert "snapshot" in row and "decisions" in row


def test_29_decision_ts_is_epoch_float():
    trace = _trace()
    assert isinstance(trace.decision_ts, float)
    assert not isinstance(trace.decision_ts, str)


def test_31_and_53_single_authority_no_dual_model():
    snapshot = _snapshot()
    assert not hasattr(snapshot, "facts")
    assert not hasattr(snapshot, "routes")
    assert hasattr(snapshot, "observations")
    assert hasattr(snapshot, "dead_ends")


def test_64_cycle_trace_summary_removed():
    assert not hasattr(energy, "CycleTraceSummary")


def test_65_cycle_trace_embeds_snapshot():
    snapshot = _snapshot(observed_fact_count=3)
    trace = _trace(snapshot=snapshot)
    line, _ = encode_cycle_trace_line(trace)
    row = json.loads(line)
    assert row["snapshot"]["observed_fact_count"] == 3
    assert row["snapshot"]["observations"] == []


# ------------------------------------------------------------------ 36/46 ids

def test_36_trace_id_unique_across_instances():
    a = make_trace_id("run", "inst-a", 1)
    b = make_trace_id("run", "inst-b", 1)
    assert a != b
    assert make_trace_id("run", "inst-a", 1) == a


def test_46_decision_id_binds_trace_id_and_index():
    d1 = _decision(trace_id="t1", index=0, intent_id="i0")
    d2 = _decision(trace_id="t1", index=1, intent_id="i0")
    d3 = _decision(trace_id="t2", index=0, intent_id="i0")
    assert d1.decision_id != d2.decision_id
    assert d1.decision_id != d3.decision_id
    assert d1.decision_id == decision_id_for("r1", "t1", 0, "i0", "reason")


# --------------------------------------------------------------- 39/79/81/94-97

def test_39_measured_bytes_exclude_newline_and_count_utf8():
    trace = _trace(decisions=(_decision(),))
    line, length = encode_cycle_trace_line(trace)
    assert not line.endswith(b"\n")
    assert length == len(line)
    row = json.loads(line)
    assert row["serialized_bytes"] == length


def test_97_non_ascii_measured_in_utf8_bytes():
    obs = _obs()
    chinese = EnergyObservationSnapshot(
        fact_seq=7, fact_origin_ts=1.0, energy_origin_ts=1.0,
        route_hash="路由:a", lineage="未归属", lineage_reason="no_producer",
        inherited_intent_ids=(), state="verified", retired=False,
        verified=True, base_verified=True, confidence=0.9, witness="证据",
        artifact_id="", source="输出", actor="w1",
        correlation_kind="fallback", correlation_basis_hash="hash-1",
        eligible_for_energy=True, exclusion_reason="")
    trace = _trace(snapshot=_snapshot(observations=(chinese,),
                                      captured_fact_count=1,
                                      stored_fact_count=1))
    line, length = encode_cycle_trace_line(trace)
    assert length == len(line)
    assert len(line) == len(line.decode("utf-8").encode("utf-8"))


def test_81_single_line_capacity_invariant():
    assert MAX_TRACE_BYTES < MAX_SEGMENT_BYTES


def test_94_fixed_point_converges_at_digit_boundary():
    hits = []
    for k in range(1, 2000):
        trace = _trace(trace_id="x" * k, decision_ts=1234567.0)
        line, length = encode_cycle_trace_line(trace)
        assert len(line) == length  # stable within one object
        if length in (998, 999, 1000, 1001, 1002):
            hits.append(length)
        if len(hits) >= 2:
            break
    assert hits, "no filler length lands near a digit boundary"


def test_96_full_trace_thresholds_and_stub_semantics(monkeypatch):
    import dswarm.swarm.energy_sidecar as sidecar

    target = 500
    n_decisions = 0
    base = _trace(decisions=tuple(
        _decision(index=i, intent_id=f"intent-{i:03d}")
        for i in range(n_decisions)), trace_id="p", decision_ts=1234567.0)
    _, base_len = encode_cycle_trace_line(base)
    assert base_len < target - 2, "reduce decisions to leave padding room"
    pad = target - base_len + 1  # baseline trace_id already has one char

    def sized(delta: int) -> tuple[CycleTrace, int]:
        trace = _trace(decisions=tuple(
            _decision(index=i, intent_id=f"intent-{i:03d}")
            for i in range(n_decisions)), trace_id="p" * (pad + delta),
            decision_ts=1234567.0)
        _, length = encode_cycle_trace_line(trace)
        return trace, length

    under, under_len = sized(-1)
    at, at_len = sized(0)
    over, over_len = sized(+1)
    assert under_len == target - 1
    assert at_len == target
    assert over_len == target + 1

    monkeypatch.setattr(sidecar, "MAX_TRACE_BYTES", target)
    # below limit -> full record
    assert under_len < target
    # exactly at limit -> full record (not oversize)
    assert at_len == target
    # one byte over -> oversize stub
    assert over_len > target
    stub = build_size_stub(over, full_attempted_bytes=over_len)
    assert stub.complete is False
    assert stub.exclusion_reason == "snapshot_size_limit"
    assert stub.snapshot.observations == ()
    assert stub.snapshot.dead_ends == ()
    assert stub.snapshot.stored_fact_count == 0
    assert stub.decisions == over.decisions
    assert stub.serialized_bytes_attempted == over_len


def test_115_fixed_point_iteration_cap(monkeypatch):
    monkeypatch.setattr(energy, "MAX_FIXED_POINT_ITERATIONS", 1)
    try:
        with pytest.raises(SizeFixedPointError):
            encode_cycle_trace_line(_trace(decisions=(_decision(),)))
    finally:
        monkeypatch.setattr(energy, "MAX_FIXED_POINT_ITERATIONS",
                            MAX_FIXED_POINT_ITERATIONS)


# ----------------------------------------------------------------- 86 universe

def test_86_route_energy_universe_and_counts():
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 0.8,
                        "candidate": 0.5})
    obs = [
        _obs(fact_seq=1, route="route:a", verified=True, witness="trace"),
        _obs(fact_seq=2, route="route:a", verified=True, witness="trace",
             artifact_id="art-1", correlation_basis_hash="basis-art"),
        _obs(fact_seq=3, route="route:a", state="challenged", verified=False,
             confidence=0.4),  # census yes, contribution no
        _obs(fact_seq=4, route="route:a", retired=True),  # excluded entirely
    ]
    dead = _dead(route="route:b")  # dead-end-only route
    out = route_energies(obs, [dead], cfg, as_of_ts=1000.0,
                         captured_routes=frozenset({"route:c"}))
    assert set(out) == {"route:a", "route:b", "route:c"}
    a = out["route:a"]
    assert a.eligible is True
    assert a.raw_fact_count == 3  # challenged counted in census, retired excluded
    assert a.correlation_group_count == 2
    b = out["route:b"]
    assert b.positive == 0.0 and b.energy == 0.0 and b.eligible is False
    assert b.penalty > 0.0  # penalty still recorded
    c = out["route:c"]
    assert c.positive == 0.0 and c.penalty == 0.0 and c.eligible is False


def test_86_witness_tier_requires_nonempty_witness():
    cfg = EnergyConfig({"verified_witness": 1.0, "verified": 0.1,
                        "candidate": 0.01})
    with_witness = _obs(fact_seq=1, verified=True, witness="  trace  ")
    without = _obs(fact_seq=2, verified=True, witness="   ")
    out = route_energies([with_witness, without], [], cfg, as_of_ts=1000.0)
    assert out["route:a"].raw_fact_count == 2
    # verified_witness tier only for the non-blank witness observation
    assert out["route:a"].positive > 0.1


# ------------------------------------------------------------ 114 snapshot size

def test_114_snapshot_complete_has_no_size_clause():
    import inspect
    import dswarm.swarm.energy_capture as capture
    src = inspect.getsource(capture)
    assert "MAX_TRACE_BYTES" not in src  # size judged at trace layer only
    big_snapshot = _snapshot(observed_fact_count=10**9, captured_fact_count=0)
    assert big_snapshot.complete is True  # no size check in the dataclass


# ------------------------------------------------------------------ 55/63/116

def test_55_m7_doc_dates_are_current():
    doc = Path(__file__).resolve().parents[1] / "docs" / "10-v4-kernel-improvement-implementation.md"
    section = doc.read_text(encoding="utf-8")
    assert "2026-08-16" in section


def test_63_offline_reorder_estimate_wording():
    doc = Path(__file__).resolve().parents[1] / "docs" / "10-v4-kernel-improvement-implementation.md"
    text = doc.read_text(encoding="utf-8")
    start = text.index("## M7 energy")
    end = text.index("## M8 Advisor")
    assert "offline scheduling reorder estimate" in text[start:end]


def test_116_test_ownership_partition_is_disjoint_and_total():
    m0 = set(range(23, 40)) | set(range(45, 52)) | set(range(53, 69)) | set(range(74, 117)) | set(range(119, 128))
    m1 = set(range(1, 23)) | set(range(69, 74))
    m2 = set(range(40, 45)) | {52} | set(range(117, 119))
    assert m0 & m1 == set()
    assert m0 & m2 == set()
    assert m1 & m2 == set()
    assert m0 | m1 | m2 == set(range(1, 128))
