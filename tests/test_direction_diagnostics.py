"""M4 direction diagnostics: deterministic registry, fallback, and persistence."""

from __future__ import annotations

import json

import pytest

from types import SimpleNamespace
from dswarm.core.event_bus import EventBus
from dswarm.core.events import EventType

from dswarm.models.solve_graph import Challenge
from dswarm.solver.direction_rules import DirectionRegistry, sanitize_raw_direction
from dswarm.solver.reason import Intent, ReasonResult, parse_reason_reply
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _challenge(**overrides) -> Challenge:
    values = {
        "id": "m4-direction",
        "name": "direction test",
        "category": "web",
        "points": 50,
        "description": "solve me",
        "flag_format": r"flag\{[^}]+\}",
        "target": "https://example.test/",
    }
    values.update(overrides)
    return Challenge(**values)


def test_direction_registry_resolution_is_structured_and_stable():
    registry = DirectionRegistry()

    assert registry.canonicalize("") == ("", "empty")
    assert registry.canonicalize("  auto ") == ("", "explicit_auto")
    assert registry.canonicalize("REV") == ("rev", "explicit_canonical")
    assert registry.canonicalize("reverse") == ("rev", "recognized_alias")
    assert registry.canonicalize("reversing") == ("", "invalid")

    assert registry.suggest("extract RSA key from the challenge", "") == (
        "crypto", "mechanical_fallback"
    )
    assert registry.suggest("nothing domain-specific", "") is None


def test_raw_direction_is_sanitized_at_parse_boundary():
    raw = "  rev\x00\x1b " + ("x" * 80)
    cleaned = sanitize_raw_direction(raw)
    assert "\x00" not in cleaned and "\x1b" not in cleaned
    assert len(cleaned) <= 40


def test_parse_reason_reply_preserves_per_intent_direction_diagnostics():
    result = parse_reason_reply(
        json.dumps(
            {
                "intents": [
                    {"id": "I1", "goal": "web probe", "direction": "crypto"},
                    {"id": "I2", "goal": "reverse binary", "direction": "reverse"},
                    {"id": "I3", "goal": "generic follow-up", "direction": ""},
                    {"id": "I4", "goal": "bad direction", "direction": "reversing"},
                ]
            }
        )
    )

    by_id = {intent.intent_id: intent for intent in result.intents}
    assert (by_id["I1"].raw_direction, by_id["I1"].direction,
            by_id["I1"].direction_resolution) == ("crypto", "crypto", "explicit_canonical")
    assert (by_id["I2"].raw_direction, by_id["I2"].direction,
            by_id["I2"].direction_resolution) == ("reverse", "rev", "recognized_alias")
    assert (by_id["I3"].raw_direction, by_id["I3"].direction,
            by_id["I3"].direction_resolution) == ("", "", "empty")
    assert (by_id["I4"].raw_direction, by_id["I4"].direction,
            by_id["I4"].direction_resolution) == ("reversing", "", "invalid")


def test_scheduler_fallback_never_overrides_valid_model_direction():
    swarm = ReasonSwarm(_challenge())
    result = ReasonResult(
        goal_met=False,
        intents=[
            Intent(
                intent_id="valid",
                goal="extract RSA key from binary",
                direction="rev",
                raw_direction="rev",
                direction_resolution="explicit_canonical",
            ),
            Intent(
                intent_id="invalid",
                goal="extract RSA key from binary",
                direction="",
                raw_direction="reversing",
                direction_resolution="invalid",
            ),
            Intent(
                intent_id="empty",
                goal="generic follow-up",
                direction="",
                raw_direction="",
                direction_resolution="empty",
            ),
        ],
        audit_notes=[],
    )

    decisions = {d.intent_id: d for d in swarm._decisions_from_reason(result)}
    assert (decisions["valid"].direction, decisions["valid"].profile,
            decisions["valid"].direction_resolution) == ("rev", "pi-rev", "explicit_canonical")
    assert (decisions["invalid"].direction, decisions["invalid"].profile,
            decisions["invalid"].direction_resolution) == ("crypto", "pi-crypto", "mechanical_fallback")
    assert (decisions["empty"].direction, decisions["empty"].profile,
            decisions["empty"].direction_resolution) == ("web", "pi-web", "category_fallback")
    assert decisions["invalid"].raw_direction == "reversing"


def test_direction_diagnostics_persist_in_event_and_dispatch_projection(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    graph.propose_intent(
        actor="reason",
        intent_id="I-diag",
        goal="extract RSA key",
        payload={
            "worker_class": "shell_agent",
            "direction": "crypto",
            "raw_direction": "reversing",
            "direction_resolution": "mechanical_fallback",
        },
    )

    rows = graph.dispatchable_intents()
    row = next(item for item in rows if item["intent_id"] == "I-diag")
    assert row["direction"] == "crypto"
    assert row["raw_direction"] == "reversing"
    assert row["direction_resolution"] == "mechanical_fallback"

    with graph._lock:
        payload = graph._conn.execute(
            "SELECT payload FROM events WHERE kind='intent_proposed' "
            "AND json_extract(payload, '$.intent_id')='I-diag'"
        ).fetchone()[0]
    event = json.loads(payload)
    assert event["raw_direction"] == "reversing"
    assert event["direction_resolution"] == "mechanical_fallback"
    graph.close()


@pytest.mark.asyncio
async def test_scheduler_telemetry_contains_direction_diagnostics():
    events = []
    bus = EventBus()

    async def capture(event):
        events.append(event)

    bus.add_sink(capture)
    calls = 0

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ReasonResult(
                goal_met=False,
                intents=[Intent(
                    intent_id="I-telemetry",
                    goal="extract RSA key",
                    direction="",
                    raw_direction="reversing",
                    direction_resolution="invalid",
                )],
                audit_notes=[],
            )
        return ReasonResult(goal_met=True, intents=[], audit_notes=[])

    async def worker(decision, profile):
        return SimpleNamespace(flag=None, flags=[], engine=profile.id)

    swarm = ReasonSwarm(
        _challenge(),
        bus=bus,
        reason_fn=reason_fn,
        worker_factory=worker,
    )
    await swarm.run()

    deltas = [event.payload for event in events if event.event_type is EventType.BLACKBOARD_DELTA]
    proposed = next(item for item in deltas if item.get("kind") == "intent_proposed")
    override = next(item for item in deltas if item.get("kind") == "direction_override")
    assert proposed["raw_direction"] == "reversing"
    assert proposed["canonical_direction"] == "crypto"
    assert proposed["direction_resolution"] == "mechanical_fallback"
    assert override["intent_id"] == "I-telemetry"
    assert override["raw_direction"] == "reversing"


def test_operator_decision_rejects_invalid_stored_direction(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "invalid-operator.db", challenge=_challenge())
    directive = graph.add_operator_directive(action="hint", text="use a safe route")
    graph.propose_intent(
        actor="coordinator",
        intent_id="I-invalid-operator",
        goal="follow the operator hint",
        payload={
            "directive_id": directive["directive_id"],
            "worker_class": "shell_agent",
            "direction": "reversing",
            "raw_direction": "reversing",
            "direction_resolution": "invalid",
            "direction_source": "operator",
            "priority": 0.8,
        },
    )

    decision = ReasonSwarm(_challenge(), graph=graph)._open_operator_decision()

    assert decision is not None
    assert decision.direction == "web"
    assert decision.profile == "pi-web"
    assert decision.direction_source == "category"
    assert decision.direction != "reversing"
    graph.close()


def test_operator_decision_marks_category_fallback_as_category(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "category-operator.db", challenge=_challenge())
    directive = graph.add_operator_directive(action="hint", text="use the challenge category")
    graph.propose_intent(
        actor="coordinator",
        intent_id="I-category-operator",
        goal="follow the category",
        payload={
            "directive_id": directive["directive_id"],
            "worker_class": "shell_agent",
            "direction": "",
            "raw_direction": "",
            "direction_resolution": "empty",
            "direction_source": "operator",
            "priority": 0.8,
        },
    )

    decision = ReasonSwarm(_challenge(), graph=graph)._open_operator_decision()

    assert decision is not None
    assert decision.direction == "web"
    assert decision.direction_source == "category"
    graph.close()


def test_operator_decision_preserves_direction_diagnostics(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "operator.db", challenge=_challenge())
    directive = graph.add_operator_directive(action="hint", text="use reverse engineering")
    graph.propose_intent(
        actor="coordinator",
        intent_id="I-operator",
        goal="use reverse engineering",
        payload={
            "directive_id": directive["directive_id"],
            "worker_class": "shell_agent",
            "direction": "rev",
            "canonical_direction": "rev",
            "raw_direction": "reverse",
            "direction_resolution": "recognized_alias",
            "priority": 0.8,
        },
    )

    decision = ReasonSwarm(_challenge(), graph=graph)._open_operator_decision()
    assert decision is not None
    assert (decision.direction, decision.canonical_direction,
            decision.raw_direction, decision.direction_resolution) == (
                "rev", "rev", "reverse", "recognized_alias"
            )
    graph.close()





def test_scheduler_canonicalizes_programmatic_alias_intents():
    swarm = ReasonSwarm(_challenge())
    result = ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id="legacy-alias",
            goal="reverse engineer the binary",
            direction="reverse",
        )],
        audit_notes=[],
    )

    decision = swarm._decisions_from_reason(result)[0]
    assert (decision.direction, decision.canonical_direction,
            decision.direction_resolution, decision.profile) == (
                "rev", "rev", "recognized_alias", "pi-rev"
            )
