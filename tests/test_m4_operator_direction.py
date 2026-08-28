from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dswarm.core.event_bus import EventBus
from dswarm.core.events import EventType
from dswarm.models.solve_graph import Challenge
from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def challenge(**overrides) -> Challenge:
    values = {
        "id": "m41",
        "name": "M4.1",
        "category": "web",
        "description": "composite challenge",
        "target": "https://example.test",
    }
    values.update(overrides)
    return Challenge(**values)


def test_challenge_stores_optional_operator_direction():
    assert Challenge(id="x", name="x", category="web").direction == ""
    assert challenge(direction="pwn").direction == "pwn"


def test_operator_direction_wins_over_keyword_for_empty_or_invalid_model_direction():
    swarm = ReasonSwarm(challenge(direction="pwn"))
    result = ReasonResult(
        goal_met=False,
        intents=[
            Intent(intent_id="empty", goal="extract RSA key", direction="",
                   raw_direction="", direction_resolution="empty"),
            Intent(intent_id="invalid", goal="extract RSA key", direction="",
                   raw_direction="banana", direction_resolution="invalid"),
        ],
        audit_notes=[],
    )

    decisions = {item.intent_id: item for item in swarm._decisions_from_reason(result)}

    assert decisions["empty"].direction == "pwn"
    assert decisions["empty"].profile == "pi-pwn"
    assert decisions["empty"].direction_source == "operator"
    assert decisions["empty"].direction_resolution == "empty"
    assert decisions["invalid"].direction == "pwn"
    assert decisions["invalid"].direction_source == "operator"
    assert decisions["invalid"].direction_resolution == "invalid"


def test_valid_model_alias_beats_operator_direction():
    swarm = ReasonSwarm(challenge(direction="pwn"))
    result = ReasonResult(
        goal_met=False,
        intents=[Intent(
            intent_id="model", goal="reverse engineer binary", direction="reverse",
            raw_direction="reverse", direction_resolution="recognized_alias",
        )],
        audit_notes=[],
    )

    decision = swarm._decisions_from_reason(result)[0]
    assert (decision.direction, decision.profile, decision.direction_source) == (
        "rev", "pi-rev", "model"
    )


@pytest.mark.asyncio
async def test_initial_recon_uses_operator_direction_without_keyword_fallback():
    seen = []

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=True, intents=[], audit_notes=[])

    async def worker(decision, profile):
        seen.append((decision.intent_id, decision.direction, decision.direction_source, profile.id))
        return SimpleNamespace(flag=None, flags=[], engine=profile.id)

    swarm = ReasonSwarm(
        challenge(direction="pwn", description="RSA crypto challenge"),
        reason_fn=reason_fn, worker_factory=worker,
    )

    await swarm.run()
    assert seen[0] == ("recon-initial", "pwn", "operator", "pi-pwn")


def test_direction_source_is_persisted_in_intent_projection(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "m41.db", challenge=challenge())
    graph.propose_intent(
        actor="reason", intent_id="I-source", goal="operator route",
        payload={
            "worker_class": "shell_agent", "direction": "pwn",
            "canonical_direction": "", "raw_direction": "",
            "direction_resolution": "empty", "direction_source": "operator",
        },
    )

    row = next(item for item in graph.dispatchable_intents() if item["intent_id"] == "I-source")
    assert row["direction_source"] == "operator"
    graph.close()


def test_operator_direction_normalization_accepts_alias_and_rejects_dirty_value():
    from apps.web.drivers import normalize_operator_direction

    assert normalize_operator_direction("reverse")[0] == "rev"
    assert normalize_operator_direction("ai-security")[0] == "aisec"
    assert normalize_operator_direction("not-a-direction")[0] == ""


def test_operator_direction_has_one_shared_public_implementation():
    from apps.web import drivers
    from dswarm.solver.direction_rules import normalize_operator_direction

    assert drivers.normalize_operator_direction is normalize_operator_direction


def test_graph_key_normalizers_delegate_to_dependency_free_leaf():
    from dswarm.core import normalization

    assert SQLiteSharedGraph.normalize_route_hash("sqli on /login") == normalization.normalize_route_hash("sqli on /login")
    assert SQLiteSharedGraph.normalize_lane_key("destructive:https:443@example.test") == normalization.normalize_lane_key("destructive:https:443@example.test")
    assert SQLiteSharedGraph.normalize_resource_key(" target / 443 ") == normalization.normalize_resource_key(" target / 443 ")


def test_resolve_direction_merge_distinguishes_missing_from_explicit_auto():
    from apps.web.run_manager import merge_resolve_dispatch

    saved = {"challenge": {"direction": "pwn", "category": "web"}}
    historical = {"direction": "rev", "category": "reverse"}
    assert merge_resolve_dispatch(
        saved, {}, historical_challenge=historical
    )["challenge"]["direction"] == "pwn"
    assert merge_resolve_dispatch(
        saved, {"challenge": {"direction": ""}}, historical_challenge=historical
    )["challenge"]["direction"] == ""
    assert merge_resolve_dispatch(
        saved, {"challenge": {"direction": "crypto"}}, historical_challenge=historical
    )["challenge"]["direction"] == "crypto"


@pytest.mark.asyncio
async def test_web_driver_threads_canonical_operator_direction_into_ctf_challenge(monkeypatch):
    from apps.web import drivers
    import dswarm.swarm.swarm as swarm_module

    captured = {}

    class FakeSwarm:
        def __init__(self, challenge, **kwargs):
            captured["challenge"] = challenge

        async def run(self):
            return SimpleNamespace(flag=None, flags=[], solved=False, winner=None)

    monkeypatch.setattr(swarm_module, "Swarm", FakeSwarm)
    driver = drivers._swarm_driver({
        "kind": "swarm",
        "worker_backend": "local",
        "challenge": {
            "name": "ctf",
            "category": "web",
            "direction": "reverse",
            "description": "reverse a binary",
        },
    })

    class FakeBus:
        async def emit(self, *args, **kwargs):
            pass

        async def close(self):
            pass

        def add_sink(self, *args):
            pass

    class FakeRun:
        run_id = "m41-driver"
        bus = FakeBus()
        hitl = None
        worker_cmds = None
        cost = None
        flag = None

    await driver(FakeRun())
    assert captured["challenge"].direction == "rev"
    assert captured["challenge"].mode == "ctf"


@pytest.mark.asyncio
async def test_web_driver_ignores_operator_direction_in_pentest(monkeypatch):
    from apps.web import drivers
    import dswarm.swarm.swarm as swarm_module

    captured = {}

    class FakeSwarm:
        def __init__(self, challenge, **kwargs):
            captured["challenge"] = challenge

        async def run(self):
            return SimpleNamespace(flag=None, flags=[], solved=False, winner=None)

    monkeypatch.setattr(swarm_module, "Swarm", FakeSwarm)
    driver = drivers._swarm_driver({
        "kind": "swarm",
        "worker_backend": "local",
        "challenge": {
            "name": "pentest",
            "category": "web",
            "direction": "pwn",
            "mode": "pentest",
            "description": "audit the target",
        },
    })

    class FakeBus:
        async def emit(self, *args, **kwargs):
            pass

        async def close(self):
            pass

        def add_sink(self, *args):
            pass

    class FakeRun:
        run_id = "m41-pentest"
        bus = FakeBus()
        hitl = None
        worker_cmds = None
        cost = None
        flag = None

    await driver(FakeRun())
    assert captured["challenge"].direction == ""
    assert captured["challenge"].mode == "pentest"
