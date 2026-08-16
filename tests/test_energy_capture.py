"""M7-0 capture tests: bounded read-only snapshot (docs/10 items 23-24, 32-35,
47, 56-57, 67-68)."""

from __future__ import annotations

import ast
import inspect
import json
import threading
import time

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm import energy_capture
from dswarm.swarm.energy_capture import (
    _phase1_read,
    capture_energy_cycle_snapshot,
)
from dswarm.swarm.energy import GraphCycleSnapshot
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _chal() -> Challenge:
    return Challenge(id="t1", name="t", category="crypto")


@pytest.fixture
def graph(tmp_path):
    g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
    return g


def seed_fact(g, *, fact, route="", ts, confidence=0.8, verified=0, actor="w1",
              artifact_id=None, witness=None, intent_id=None,
              orphan_intent_id=None):
    payload = {"fact": fact, "source": "output", "route_hash": route}
    if witness:
        payload["witness"] = witness
    if intent_id:
        payload["intent_id"] = intent_id
    if orphan_intent_id:
        payload["orphan_intent_id"] = orphan_intent_id
    with g._lock:
        g._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, "
            "artifact_id, verified, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (ts, g.challenge.id, actor, "fact_added", json.dumps(payload),
             artifact_id, verified, confidence))
        g._conn.commit()


def seed_promotion(g, fact_seq, ts, confidence=0.9, witness="trace"):
    payload = {"fact_seq": fact_seq, "confidence": confidence,
               "witness": witness, "source": "verify", "verifier": "v1"}
    with g._lock:
        g._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (ts, g.challenge.id, "v1", "fact_verified", json.dumps(payload)))
        g._conn.commit()


def seed_transition(g, fact_seq, kind, ts, actor="v1"):
    payload = {"fact_seq": fact_seq}
    with g._lock:
        g._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (ts, g.challenge.id, actor, kind, json.dumps(payload)))
        g._conn.commit()


def seed_intent(g, intent_id, created_seq=None, route_hash="route:a"):
    with g._lock:
        cur = g._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (10.0, g.challenge.id, "reason", "intent_proposed",
             json.dumps({"intent_id": intent_id})))
        seq = created_seq if created_seq is not None else int(cur.lastrowid or 0)
        g._conn.execute(
            "INSERT INTO intents (intent_id, challenge_id, goal, worker_class, "
            "route_hash, priority, priority_scale, status, created_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (intent_id, g.challenge.id, "goal", "code", route_hash, 1,
             "planner", "open", seq))
        g._conn.commit()


def seed_product(g, intent_id, fact_seq):
    with g._lock:
        g._conn.execute(
            "INSERT OR IGNORE INTO intent_products (intent_id, fact_seq) "
            "VALUES (?,?)", (intent_id, fact_seq))
        g._conn.commit()


def seed_conclusion_event(g, intent_id, result, ts, actor="w1"):
    payload = {"intent_id": intent_id, "result": result}
    with g._lock:
        cur = g._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (ts, g.challenge.id, actor, "intent_concluded", json.dumps(payload)))
        seq = cur.lastrowid
        g._conn.commit()
    return seq


def seed_applied_result(g, intent_id, result_seq):
    with g._lock:
        g._conn.execute(
            "UPDATE intents SET result_seq=?, status='done' WHERE intent_id=?",
            (result_seq, intent_id))
        g._conn.commit()


def capture(g, **kw):
    return capture_energy_cycle_snapshot(g.db_path, g.challenge.id, **kw)


# ------------------------------------------------------------- 23/24/67 cutoff

def test_23_and_24_capture_reflects_only_committed_state(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=100.0)
    snap1 = capture(graph)
    assert snap1.complete is True
    assert snap1.observed_fact_count == 1
    assert snap1.captured_fact_count == 1
    assert all(o.fact_seq <= snap1.graph_after_seq
               for o in snap1.observations)
    # promotion commits AFTER snap1 -> invisible to snap1, visible to snap2
    fact_seq = snap1.observations[0].fact_seq
    seed_promotion(graph, fact_seq, ts=200.0)
    snap2 = capture(graph)
    obs1 = {o.fact_seq: o for o in snap1.observations}[fact_seq]
    obs2 = {o.fact_seq: o for o in snap2.observations}[fact_seq]
    assert obs1.energy_origin_ts == 100.0  # fact_ts, promotion invisible
    assert obs2.energy_origin_ts == 200.0  # promotion_ts (test 68 JOIN)
    assert snap2.graph_after_seq > snap1.graph_after_seq


def test_67_future_timestamp_still_membership_by_seq(graph):
    # ts far in the future: causal membership is seq-only, so it appears.
    seed_fact(graph, fact="future", route="route:a", ts=4102444800.0)
    snap = capture(graph)
    assert snap.captured_fact_count == 1


# -------------------------------------------------------------------- 68 JOIN

def test_68_promotion_timestamp_join(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=50.0)
    snap = capture(graph)
    fact_seq = snap.observations[0].fact_seq
    seed_promotion(graph, fact_seq, ts=777.5)
    snap = capture(graph)
    obs = {o.fact_seq: o for o in snap.observations}[fact_seq]
    assert obs.energy_origin_ts == 777.5
    assert obs.fact_origin_ts == 50.0
    assert obs.base_verified is True


# ------------------------------------------------------------------ 35 folding

def test_35_m3_folding_semantics(graph):
    seed_fact(graph, fact="c1", route="route:a", ts=10.0)
    snap = capture(graph)
    fact_seq = snap.observations[0].fact_seq
    seed_transition(graph, fact_seq, "fact_challenged", ts=20.0)
    snap = capture(graph)
    obs = {o.fact_seq: o for o in snap.observations}[fact_seq]
    assert obs.state == "challenged"
    assert obs.verified is False
    assert obs.confidence == 0.4
    seed_transition(graph, fact_seq, "fact_rejected", ts=30.0)
    snap = capture(graph)
    obs = {o.fact_seq: o for o in snap.observations}[fact_seq]
    assert obs.retired is True
    assert obs.verified is False
    assert obs.confidence == 0.0


# ------------------------------------------------------------- 47/56 dead-end

def test_47_and_56_dead_end_reads_applied_result_seq(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=10.0)
    snap = capture(graph)
    fact_seq = snap.observations[0].fact_seq
    seed_intent(graph, "intent-1", created_seq=snap.graph_after_seq + 1)
    seed_product(graph, "intent-1", fact_seq)
    # stale (earlier) conclusion first, applied (later) second 鈥?result_seq
    # points at the EARLIER seq so MAX(seq) would pick the stale one.
    stale_seq = seed_conclusion_event(
        graph, "intent-1", "dead_end", ts=20.0)
    applied_seq = seed_conclusion_event(
        graph, "intent-1", "dead_end", ts=30.0)
    seed_applied_result(graph, "intent-1", stale_seq)  # owner fence applied stale
    snap = capture(graph)
    dead = [d for d in snap.dead_ends if d.intent_id == "intent-1"]
    assert len(dead) == 1
    d = dead[0]
    assert d.result_seq == stale_seq  # intents.result_seq, NOT MAX(seq)
    assert d.result_seq != applied_seq
    assert d.conclusion_event_count == 2
    assert d.ignored_stale_conclusion_count == 1
    assert d.genuine_giveup is True
    assert d.eligible_for_energy is True


def test_47_dead_end_not_genuine_giveup_is_ineligible(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=10.0)
    snap = capture(graph)
    seed_intent(graph, "intent-2", created_seq=snap.graph_after_seq + 1)
    seq = seed_conclusion_event(graph, "intent-2", "explored", ts=20.0)
    seed_applied_result(graph, "intent-2", seq)
    snap = capture(graph)
    dead = [d for d in snap.dead_ends if d.intent_id == "intent-2"]
    assert len(dead) == 1
    assert dead[0].eligible_for_energy is False
    assert dead[0].exclusion_reason == "not_genuine_giveup"


# ------------------------------------------------------------ lineage capture

def test_lineage_from_intent_products(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=10.0)
    snap = capture(graph)
    fact_seq = snap.observations[0].fact_seq
    seed_intent(graph, "intent-9", created_seq=snap.graph_after_seq + 1)
    seed_product(graph, "intent-9", fact_seq)
    snap = capture(graph)
    obs = {o.fact_seq: o for o in snap.observations}[fact_seq]
    assert obs.lineage == "explicit"
    assert obs.lineage_reason == "explicit_matches_inherited"
    assert obs.inherited_intent_ids == ("intent-9",)


def test_capture_inherits_unique_route_from_product_edge(graph):
    seed_fact(graph, fact="inherited", ts=10.0)
    fact_seq = capture(graph).observations[0].fact_seq
    seed_intent(graph, "intent-a", route_hash="JWT")
    seed_product(graph, "intent-a", fact_seq)

    obs = capture(graph).observations[0]
    assert obs.route_hash == "jwt"
    assert obs.lineage == "inherited"
    assert obs.lineage_reason == "intent_product"
    assert obs.inherited_intent_ids == ("intent-a",)
    assert obs.eligible_for_energy is True


def test_capture_preserves_same_route_multi_intent_lineage(graph):
    seed_fact(graph, fact="shared", ts=10.0)
    fact_seq = capture(graph).observations[0].fact_seq
    seed_intent(graph, "intent-b", route_hash="JWT")
    seed_intent(graph, "intent-a", route_hash="jwt")
    seed_product(graph, "intent-b", fact_seq)
    seed_product(graph, "intent-a", fact_seq)

    obs = capture(graph).observations[0]
    assert obs.route_hash == "jwt"
    assert obs.lineage == "inherited"
    assert obs.inherited_intent_ids == ("intent-b", "intent-a")
    assert obs.eligible_for_energy is True


def test_capture_excludes_inherited_and_explicit_conflicts(graph):
    seed_fact(graph, fact="multi-conflict", ts=10.0)
    first_seq = capture(graph).observations[0].fact_seq
    seed_intent(graph, "intent-a", route_hash="jwt")
    seed_intent(graph, "intent-b", route_hash="sqli")
    seed_product(graph, "intent-a", first_seq)
    seed_product(graph, "intent-b", first_seq)

    seed_fact(graph, fact="explicit-conflict", route="rev", ts=11.0)
    second_seq = max(o.fact_seq for o in capture(graph).observations)
    seed_product(graph, "intent-a", second_seq)

    by_seq = {o.fact_seq: o for o in capture(graph).observations}
    inherited = by_seq[first_seq]
    assert inherited.route_hash == ""
    assert inherited.lineage == "inherited_conflict"
    assert inherited.lineage_reason == "inherited_route_conflict"
    assert inherited.eligible_for_energy is False
    assert inherited.exclusion_reason == "lineage_unresolved"
    explicit = by_seq[second_seq]
    assert explicit.route_hash == "rev"
    assert explicit.lineage == "explicit_conflict"
    assert explicit.lineage_reason == "explicit_inherited_conflict"
    assert explicit.eligible_for_energy is False
    assert explicit.exclusion_reason == "lineage_unresolved"


def test_capture_uses_payload_fallback_but_never_orphan_reference(graph):
    seed_intent(graph, "intent-payload", route_hash="jwt")
    seed_intent(graph, "intent-orphan", route_hash="sqli")
    seed_fact(graph, fact="legacy", ts=10.0, intent_id="intent-payload")
    seed_fact(graph, fact="orphan", ts=11.0, orphan_intent_id="intent-orphan")

    by_fact = {o.fact_seq: o for o in capture(graph).observations}
    ordered = [by_fact[key] for key in sorted(by_fact)]
    payload, orphan = ordered
    assert payload.route_hash == "jwt"
    assert payload.lineage == "inherited"
    assert payload.lineage_reason == "payload_intent_inherit"
    assert payload.inherited_intent_ids == ("intent-payload",)
    assert payload.eligible_for_energy is True
    assert orphan.route_hash == ""
    assert orphan.lineage == "unattributed"
    assert orphan.lineage_reason == "orphan_intent_reference"
    assert orphan.inherited_intent_ids == ()
    assert orphan.eligible_for_energy is False


def test_invalid_fact_row_marks_snapshot_incomplete(graph):
    seed_fact(graph, fact="bad-confidence", route="jwt", ts=10.0,
              confidence="bad")
    snap = capture(graph)
    assert snap.complete is False
    assert snap.exclusion_reason == "snapshot_invalid_rows"
    assert snap.observed_fact_count == 1
    assert snap.captured_fact_count == 0
    assert snap.stored_fact_count == 0


# ------------------------------------------------------------------ 32/33/57

def test_33_capture_failure_degrades_to_unavailable(tmp_path):
    snap = capture_energy_cycle_snapshot(
        str(tmp_path / "missing.db"), "t1")
    assert isinstance(snap, GraphCycleSnapshot)
    assert snap.complete is False
    assert snap.exclusion_reason == "snapshot_unavailable"
    assert snap.observations == () and snap.dead_ends == ()


def test_57_progress_handler_deadline_aborts_and_releases(graph):
    seed_fact(graph, fact="f1", route="route:a", ts=10.0)
    snap = capture(graph, deadline_s=0.0)
    assert snap.complete is False
    assert snap.exclusion_reason == "snapshot_unavailable"
    # connection was closed: a fresh capture on the same db still works and the
    # writer is not blocked afterwards.
    snap2 = capture(graph)
    assert snap2.complete is True


def test_32_capture_does_not_block_writer(graph, monkeypatch):
    seed_fact(graph, fact="f1", route="route:a", ts=10.0)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        try:
            i = 0
            while not stop.is_set():
                seed_fact(graph, fact=f"w{i}", route="route:a", ts=1000.0 + i)
                i += 1
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(40):
            snap = capture(graph)
            assert isinstance(snap, GraphCycleSnapshot)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors


# ------------------------------------------------------------------ 34/54 ast

def test_34_and_54_phase1_has_no_phase2_work():
    src = inspect.getsource(_phase1_read)
    tree = ast.parse(src)
    banned = {"json", "hashlib", "EnergyObservationSnapshot",
              "DeadEndObservationSnapshot", "GraphCycleSnapshot", "blake2b"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in banned:
                raise AssertionError(
                    f"phase 1 references phase-2 symbol: {node.attr}")
        if isinstance(node, ast.Name) and node.id in banned:
            raise AssertionError(
                f"phase 1 references phase-2 symbol: {node.id}")


def test_malformed_applied_conclusion_marks_snapshot_incomplete(graph):
    seed_intent(graph, "intent-malformed", route_hash="route:a")
    with graph._lock:
        cur = graph._conn.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload) "
            "VALUES (?,?,?,?,?)",
            (20.0, graph.challenge.id, "w1", "intent_concluded", "{bad-json"),
        )
        graph._conn.execute(
            "UPDATE intents SET result_seq=?, status='done' WHERE intent_id=?",
            (int(cur.lastrowid or 0), "intent-malformed"),
        )
        graph._conn.commit()

    snap = capture(graph)

    assert snap.complete is False
    assert snap.exclusion_reason == "snapshot_invalid_rows"
    assert snap.dead_ends == ()
