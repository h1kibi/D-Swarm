from __future__ import annotations

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import (
    EV_FACT_ADDED,
    IntentRouteRef,
    RouteObservation,
    SQLiteSharedGraph,
)


def _challenge() -> Challenge:
    return Challenge(id="route-lineage", name="route lineage", category="web")


def _open(tmp_path):
    return SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())


def _propose(graph: SQLiteSharedGraph, intent_id: str, route_hash: str) -> None:
    graph.propose_intent(
        actor="reason",
        intent_id=intent_id,
        goal=f"test {intent_id}",
        payload={"route_hash": route_hash},
    )


def test_route_lineage_keeps_all_intents_when_routes_agree(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-b", "SQL Injection")
    _propose(graph, "I-a", "sqli")

    fact_seq = graph.add_evidence(
        actor="cli-a",
        source="curl",
        fact="same response body",
        verified=True,
        intent_id="I-a",
    )
    assert graph.add_evidence(
        actor="cli-a",
        source="curl",
        fact="same response body",
        verified=True,
        intent_id="I-b",
    ) == -1

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation == RouteObservation(
        fact_seq=fact_seq,
        event_ts=observation.event_ts,
        inherited_routes=(
            IntentRouteRef(intent_id="I-b", route_hash="sqli"),
            IntentRouteRef(intent_id="I-a", route_hash="sqli"),
        ),
        effective_route_hash="sqli",
        lineage="inherited",
        reason="intent_product",
        eligible_for_energy=True,
    )
    assert observation.event_ts > 0
    graph.close()


def test_route_lineage_preserves_generated_fallback_route_identity(tmp_path):
    graph = _open(tmp_path)
    generated_route = graph.normalize_route_hash("!!!")
    assert generated_route.startswith("route:")
    _propose(graph, "I-generated", "!!!")
    fact_seq = graph.add_evidence(
        actor="cli-a",
        source="curl",
        fact="generated route fact",
        verified=True,
        route_hash="!!!",
        intent_id="I-generated",
    )

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.explicit_route_hash == generated_route
    assert observation.inherited_routes == (
        IntentRouteRef(intent_id="I-generated", route_hash=generated_route),
    )
    assert observation.effective_route_hash == generated_route
    assert observation.lineage == "explicit"
    assert observation.eligible_for_energy is True
    graph.close()


def test_route_lineage_marks_different_inherited_routes_as_conflict(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-web", "SQL Injection")
    _propose(graph, "I-auth", "JWT")
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="shared endpoint", verified=True,
        intent_id="I-web",
    )
    graph.add_evidence(
        actor="cli-a", source="curl", fact="shared endpoint", verified=True,
        intent_id="I-auth",
    )

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.lineage == "inherited_conflict"
    assert observation.effective_route_hash == ""
    assert observation.inherited_routes == (
        IntentRouteRef(intent_id="I-web", route_hash="sqli"),
        IntentRouteRef(intent_id="I-auth", route_hash="jwt"),
    )
    assert observation.reason == "inherited_route_conflict"
    assert observation.eligible_for_energy is False
    graph.close()


def test_explicit_route_conflict_preserves_explicit_and_all_inherited(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-a", "JWT")
    _propose(graph, "I-b", "Command Injection")
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="explicit route fact", verified=True,
        route_hash="SQL Injection", intent_id="I-a",
    )
    graph.add_evidence(
        actor="cli-a", source="curl", fact="explicit route fact", verified=True,
        route_hash="SQL Injection", intent_id="I-b",
    )

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.explicit_route_hash == "sqli"
    assert observation.effective_route_hash == "sqli"
    assert observation.lineage == "explicit_conflict"
    assert observation.inherited_routes == (
        IntentRouteRef(intent_id="I-a", route_hash="jwt"),
        IntentRouteRef(intent_id="I-b", route_hash="cmdi"),
    )
    assert observation.reason == "explicit_inherited_conflict"
    assert observation.eligible_for_energy is False
    graph.close()


def test_explicit_route_matching_inherited_is_energy_eligible(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-a", "SQL Injection")
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="matching route fact", verified=True,
        route_hash="sqli", intent_id="I-a",
    )

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.lineage == "explicit"
    assert observation.effective_route_hash == "sqli"
    assert observation.reason == "explicit_matches_inherited"
    assert observation.eligible_for_energy is True
    graph.close()


def test_route_lineage_order_is_stable_when_product_edges_arrive_in_reverse(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-first", "JWT")
    _propose(graph, "I-second", "JWT")
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="stable ordering fact", verified=True,
        intent_id="I-second",
    )
    graph.add_evidence(
        actor="cli-a", source="curl", fact="stable ordering fact", verified=True,
        intent_id="I-first",
    )

    first = graph.route_lineage_for_fact(fact_seq)
    second = graph.route_observations([fact_seq])[fact_seq]

    assert first == second
    assert tuple(ref.intent_id for ref in first.inherited_routes) == (
        "I-first", "I-second",
    )
    graph.close()


def test_legacy_payload_intent_is_used_only_when_product_edge_is_absent(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-legacy", "JWT")
    fact_seq = graph._append(
        EV_FACT_ADDED,
        "legacy-worker",
        {
            "source": "legacy",
            "fact": "legacy payload fact",
            "source_solver": "legacy-worker",
            "intent_id": "I-legacy",
        },
        verified=True,
        dedupe_key="legacy-payload-fact",
    )

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.inherited_routes == (
        IntentRouteRef(intent_id="I-legacy", route_hash="jwt"),
    )
    assert observation.lineage == "inherited"
    assert observation.effective_route_hash == "jwt"
    assert observation.reason == "payload_intent_inherit"
    assert observation.eligible_for_energy is True
    graph.close()


def test_canonical_product_edge_takes_precedence_over_payload_intent_fallback(tmp_path):
    graph = _open(tmp_path)
    _propose(graph, "I-payload", "JWT")
    _propose(graph, "I-edge", "SQL Injection")
    fact_seq = graph._append(
        EV_FACT_ADDED,
        "legacy-worker",
        {
            "source": "legacy",
            "fact": "legacy fact with repaired product edge",
            "source_solver": "legacy-worker",
            "intent_id": "I-payload",
        },
        verified=True,
        dedupe_key="legacy-repaired-product-fact",
    )
    with graph._lock:
        graph._conn.execute(
            "INSERT INTO intent_products (intent_id, fact_seq) VALUES (?, ?)",
            ("I-edge", fact_seq),
        )
        graph._conn.commit()

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.inherited_routes == (
        IntentRouteRef(intent_id="I-edge", route_hash="sqli"),
    )
    assert observation.effective_route_hash == "sqli"
    assert observation.reason == "intent_product"
    graph.close()


def test_orphan_reference_is_audited_but_never_restores_lineage(tmp_path):
    graph = _open(tmp_path)
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="orphan fact", verified=True,
        intent_id="I-late",
    )
    _propose(graph, "I-late", "JWT")

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.lineage == "unattributed"
    assert observation.effective_route_hash == ""
    assert observation.inherited_routes == ()
    assert observation.attempted_orphan_intent_id == "I-late"
    assert observation.attempted_orphan_route_hash == "jwt"
    assert observation.reason == "orphan_intent_reference"
    assert observation.eligible_for_energy is False
    graph.close()


def test_orphan_reference_never_overrides_an_explicit_route(tmp_path):
    graph = _open(tmp_path)
    fact_seq = graph.add_evidence(
        actor="cli-a", source="curl", fact="explicit orphan fact", verified=True,
        route_hash="SQL Injection", intent_id="I-late",
    )
    _propose(graph, "I-late", "JWT")

    observation = graph.route_lineage_for_fact(fact_seq)

    assert observation.lineage == "explicit"
    assert observation.explicit_route_hash == "sqli"
    assert observation.effective_route_hash == "sqli"
    assert observation.inherited_routes == ()
    assert observation.attempted_orphan_intent_id == "I-late"
    assert observation.attempted_orphan_route_hash == "jwt"
    assert observation.eligible_for_energy is True
    graph.close()


def test_route_observations_batch_omits_missing_facts_and_single_lookup_rejects_them(tmp_path):
    graph = _open(tmp_path)

    assert graph.route_observations([999]) == {}
    with pytest.raises(ValueError, match="fact_seq 999"):
        graph.route_lineage_for_fact(999)
    graph.close()
