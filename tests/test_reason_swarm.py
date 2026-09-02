"""Deterministic tests for the Reason-centered swarm scheduler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from dswarm.core.events import EventType
from dswarm.core.llm import ModelSpec
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.solver.types import SolverConfig
from dswarm.swarm.agents import AgentRegistry, DispatchDecision
from dswarm.swarm.board import FindingKind, FindingPredicate, MemoryBoard
from dswarm.swarm.reason_scheduler import ReasonSwarm
from dswarm.swarm.swarm import Swarm
from dswarm.solver.cli_solver import CliSolver
from dswarm.solver.reason import Intent, ReasonResult


def _challenge(**overrides) -> Challenge:
    values = {
        "id": "c-reason",
        "name": "reason-test",
        "category": "web",
        "points": 50,
        "description": "solve me",
        "flag_format": r"flag\{[^}]+\}",
        "target": "https://example.test/",
    }
    values.update(overrides)
    return Challenge(**values)


def _outcome(flag: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(flag=flag, flags=[flag] if flag else [], engine="pi-worker")


async def test_reason_swarm_starts_with_single_recon():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    await swarm.run()

    assert len(calls) == 2
    assert [c.mode for c in calls] == ["recon", "bootstrap"]
    assert calls[0].profile == "pi-web"  # challenge category web → web direction


async def test_reason_goal_met_without_flag_does_not_stop_the_run():
    board = MemoryBoard("c-reason")
    bus = _Bus()
    calls: list[DispatchDecision] = []
    reason_calls = 0

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        nonlocal reason_calls
        reason_calls += 1
        return ReasonResult(goal_met=True, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        worker_factory=worker,
        reason_fn=reason_fn,
    )

    result = await swarm.run()

    assert result["solved"] is False
    assert [decision.mode for decision in calls] == ["recon", "bootstrap"]
    assert reason_calls == 2
    finished = next(
        event.payload
        for event in reversed(bus.events)
        if event.payload.get("delta_type") == "reason_loop_finished"
    )
    assert finished["stop_reason"] == "no_fresh_intents"
    assert finished["solved"] is False


async def test_reason_swarm_marks_initial_recon_as_resolve_when_requested():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome("flag{resolved}")

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        initial_runtime_operation_kind="resolve",
    )
    await swarm.run()

    assert len(calls) == 1
    assert calls[0].mode == "recon"
    assert calls[0].runtime_operation_kind == "resolve"


async def test_planner_unavailable_retries_and_does_not_collapse(monkeypatch):
    """run-3155: a transient planner outage used to collapse the WHOLE run — one
    failed reason cycle → fallback → next failed cycle → break → finished. The
    scheduler must retry instead, keep the run alive (no fallback/finish on a
    planner error), and only give up after a bounded run of consecutive failures."""
    import dswarm.swarm.reason_scheduler as rs

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(rs.asyncio, "sleep", _no_sleep)

    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []
    reason_calls = 0

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        nonlocal reason_calls
        reason_calls += 1
        raise RuntimeError("provider 503")

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    swarm._max_planner_failures = 2
    await swarm.run()

    # recon still ran, but NO fallback-bootstrap worker was spawned while the
    # planner is down (planner outage must not collapse into fallback/finish).
    assert [c.mode for c in calls] == ["recon"]
    # the in-cycle retry + consecutive-cycle budget both fired.
    assert reason_calls >= 3
    assert swarm._planner_failures >= 2


def test_reason_decisions_route_direction_to_profile():
    from dswarm.solver.reason import Intent, ReasonResult

    challenge = _challenge(category="web")
    swarm = ReasonSwarm(challenge)
    result = ReasonResult(
        goal_met=False,
        intents=[
            Intent(intent_id="I1", goal="crack the key", direction="crypto"),
            Intent(intent_id="I2", goal="fuzz the login", direction=""),
            Intent(intent_id="I3", goal="flip the binary", direction="rev"),
        ],
        audit_notes=[],
    )
    decisions = swarm._decisions_from_reason(result)
    by_id = {d.intent_id: d for d in decisions}
    assert by_id["I1"].profile == "pi-crypto"
    assert by_id["I1"].direction == "crypto"
    # empty direction → challenge category's primary direction profile
    assert by_id["I2"].profile == "pi-web"
    assert by_id["I3"].profile == "pi-rev"


async def test_initial_recon_flag_is_reported():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome("flag{initial-recon}")

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()

    assert len(calls) == 1
    assert result["solved"] is True
    assert result["flags"] == ["flag{initial-recon}"]


async def test_reason_dispatches_exploit_after_recon():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        if decision.mode == "explore":
            board.write_finding(
                challenge_id="c-reason",
                kind=FindingKind.FLAG_FOUND,
                agent_name="pi-web",
                target="flag{ok}",
                payload={"flag": "flag{ok}"},
            )
            return _outcome("flag{ok}")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(
                    intent_id="I1",
                    goal="probe login endpoint",
                    profile="exploit",
                    mode="explore",
                    from_facts=[1],
                )
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()

    assert [c.mode for c in calls] == ["recon", "explore"]
    assert result["solved"] is True
    assert result["flags"] == ["flag{ok}"]


async def test_reason_can_start_dynamic_recon_on_new_surface():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        if decision.mode == "recon" and decision.intent_id != "recon-initial":
            board.write_finding(
                challenge_id="c-reason",
                kind=FindingKind.FLAG_FOUND,
                agent_name="pi-web",
                target="flag{backend}",
                payload={"flag": "flag{backend}"},
            )
            return _outcome("flag{backend}")
        if decision.mode == "explore":
            board.write_finding(
                challenge_id="c-reason",
                kind=FindingKind.NEW_SURFACE,
                agent_name="pi-web",
                target="https://backend.example.test/admin",
                payload={"surface": "backend admin"},
            )
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        if any(
            f.target == "https://backend.example.test/admin"
            for f in board.query_findings(FindingPredicate())
        ):
            return ReasonResult(
                goal_met=False,
                intents=[
                    Intent(
                        intent_id="R2",
                        goal="recon newly exposed backend",
                        profile="recon",
                        mode="recon",
                        surface_target="https://backend.example.test/admin",
                    )
                ],
                audit_notes=[],
            )
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(
                    intent_id="I1",
                    goal="probe frontend",
                    profile="exploit",
                    mode="explore",
                )
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()

    modes = [c.mode for c in calls]
    assert modes.count("recon") == 2
    assert modes.count("explore") == 1
    assert result["flags"] == ["flag{backend}"]


async def test_reason_swarm_does_not_emit_race_events():
    board = MemoryBoard("c-reason")
    seen_kinds: list[str] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    original_write = board.write_finding

    def write_finding(**kwargs):
        seen_kinds.append(kwargs["kind"])
        return original_write(**kwargs)

    board.write_finding = write_finding
    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    await swarm.run()

    assert "RACE_STARTED" not in seen_kinds
    assert "race_started" not in seen_kinds


def test_default_agent_registry_contains_recon_and_exploit():
    registry = AgentRegistry()
    assert registry.resolve("recon").mode == "explore"
    assert registry.resolve("exploit").mode == "explore"
    assert registry.resolve("review").id == "pi-worker"


def test_agent_profile_resolves_category_profile():
    profile = AgentRegistry().resolve("exploit")
    assert profile.resolve_worker_profile("crypto") == "pi-worker"
    assert profile.resolve_worker_profile("web") == "pi-worker"


def test_structured_finding_markers_parse():
    text = (
        "FINDING_TYPE=HTTP_ENDPOINT\n"
        "FINDING_TARGET=https://app.example.test/api\n"
        "FINDING_DATA={\"status\":200}\n"
    )
    findings = CliSolver._extract_structured_findings(text)
    assert findings == [
        {
            "kind": "HTTP_ENDPOINT",
            "target": "https://app.example.test/api",
            "data": '{"status":200}',
        }
    ]


def test_recon_prompt_lives_in_build_prompt_not_engagement_goal():
    solver = CliSolver.__new__(CliSolver)
    solver.challenge = _challenge()
    solver.mode = "recon"
    solver.host_scan = False
    solver.intent_goal = "map backend surface"
    solver.kb = False
    solver._staged_files = []
    solver._target = lambda: "https://example.test/"
    solver._board_context = lambda: ""
    solver._intent_neighborhood_context = lambda: ""
    solver._workspace_protocol_block = lambda: ""
    solver._poc_prompt_block = lambda: ""
    solver._standing_block = lambda: ""
    solver._submit_gate_block = lambda: ""
    solver._team_context_block = lambda: ""
    solver._rejected_flags_block = lambda: ""
    solver._flag_hint = lambda: "flag{...}"

    prompt = solver._build_prompt()
    assert "reconnaissance specialist" in prompt
    assert "timeout 30s" in prompt
    assert "FOUND_FLAG=<flag> immediately" in prompt
    assert "do NOT run nmap" in prompt
    assert prompt.count("nmap") == 1, "only the prohibition may mention nmap in CTF recon"
    assert solver._engagement_goal() == "Solve reason-test [web]"


def test_recon_prompt_allows_host_scan_when_explicit():
    solver = CliSolver.__new__(CliSolver)
    solver.challenge = _challenge()
    solver.mode = "recon"
    solver.host_scan = True
    solver.intent_goal = "map backend surface"
    solver.kb = False
    solver._staged_files = []
    solver._target = lambda: "https://example.test/"
    solver._board_context = lambda: ""
    solver._intent_neighborhood_context = lambda: ""
    solver._workspace_protocol_block = lambda: ""
    solver._poc_prompt_block = lambda: ""
    solver._standing_block = lambda: ""
    solver._submit_gate_block = lambda: ""
    solver._team_context_block = lambda: ""
    solver._rejected_flags_block = lambda: ""
    solver._flag_hint = lambda: "flag{...}"

    prompt = solver._build_prompt()
    assert "host/port scanning is explicitly authorized" in prompt
    assert "nmap" not in prompt, "explicit host scan does not need the generic nmap hint"


def test_reason_decision_carries_host_scan():
    swarm = ReasonSwarm(_challenge())
    result = ReasonResult(
        goal_met=False,
        intents=[Intent(intent_id="I1", goal="find ports", host_scan=True)],
        audit_notes=[],
    )
    decisions = swarm._decisions_from_reason(result)
    assert decisions[0].host_scan is True


def test_reason_parse_host_scan_rejects_string_false():
    from dswarm.solver.reason import parse_reason_reply

    result = parse_reason_reply(
        '{"goal_met": false, "intents": ['
        '{"id": "I1", "goal": "map app", "host_scan": "false", '
        '"requires_recon": "true"}]}'
    )
    assert result.intents[0].host_scan is False
    assert result.intents[0].requires_recon is True


async def test_reason_scheduler_empty_health_roster_finishes_explicitly(tmp_path):
    """Reason mode must report unavailable workers instead of returning silently."""
    from dswarm.core.event_bus import EventBus

    challenge = _challenge()
    bus = EventBus()
    sw = Swarm(
        challenge,
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sbx"),
        artifacts=ArtifactStore(root=tmp_path / "arts"),
        config=SolverConfig(),
        run_id="c-reason-health-empty",
        executor="cli",
        engines=["pi"],
        bus=bus,
    )
    sw._healthy_engines_async = lambda: _empty_health()  # type: ignore[method-assign]
    finalized: list[dict] = []
    real_finalize = sw._finalize_coordinator_run

    async def tracking_finalize(**kwargs):
        finalized.append(kwargs)
        await real_finalize(**kwargs)

    sw._finalize_coordinator_run = tracking_finalize  # type: ignore[method-assign]
    outcome = await sw._run_reason_scheduler()

    assert outcome.solved is False
    assert outcome.reason == "worker_unavailable"
    assert finalized and finalized[0]["terminal_reason"] == "worker_unavailable"


async def _empty_health():
    return []


async def test_swarm_reason_path_starts_with_one_recon(tmp_path):
    challenge = _challenge()
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    swarm = Swarm(
        challenge,
        llm=None,
        sandbox=sandbox,
        artifacts=arts,
        config=SolverConfig(),
        run_id="c-reason",
        executor="cli",
        engines=["pi"],
    )

    async def fake_healthy(*args, **kwargs):
        return ["pi"]

    def fake_worker(*args, **kwargs):
        async def fake_run():
            return _outcome()

        return SimpleNamespace(run=fake_run, solver_id="cli-pi")

    swarm._healthy_engines_async = fake_healthy
    swarm._make_cli_worker = fake_worker
    outcome = await swarm._run_reason_scheduler()

    assert outcome.solved is False


async def test_reason_swarm_stop_event_halts_dispatch():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []
    stop_event = asyncio.Event()

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        if decision.intent_id == "recon-initial":
            stop_event.set()
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(
                    intent_id="I1",
                    goal="probe login",
                    profile="exploit",
                    mode="explore",
                )
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
        stop_event=stop_event,
    )
    result = await swarm.run()

    assert [c.intent_id for c in calls] == ["recon-initial"]
    assert result["solved"] is False


async def test_reason_planner_failure_is_not_fatal():
    class _BadLLM:
        async def chat(self, **kwargs):
            raise RuntimeError("Illegal header value b'Bearer '")

    swarm = ReasonSwarm(
        _challenge(),
        llm=_BadLLM(),
        reason_model="deepseek-v4-pro",
        worker_factory=lambda d, p: _outcome(),
    )
    result = await swarm._run_reason()
    assert result.intents == []
    assert result.audit_notes == ["reason planner unavailable"]


async def test_reason_swarm_salvages_flag_after_worker_crash():
    class FakeGraph:
        def snapshot(self):
            return SimpleNamespace(flags=["flag{salvaged}"])

    async def _boom(decision, profile):
        raise RuntimeError()

    swarm = ReasonSwarm(
        _challenge(),
        graph=FakeGraph(),
        worker_factory=_boom,
    )
    result = await swarm.run()
    assert result["solved"] is True
    assert result["flags"] == ["flag{salvaged}"]
    assert swarm.lane_gate.snapshot() == {
        "ordinary_active": 0, "review_active": 0,
    }


async def test_reason_swarm_fallback_bootstrap_when_reason_empty():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile):
        calls.append(decision)
        if decision.mode == "bootstrap":
            return _outcome("flag{fallback}")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()
    assert [c.mode for c in calls] == ["recon", "bootstrap"]
    assert result["solved"] is True
    assert result["flags"] == ["flag{fallback}"]


async def test_reason_swarm_pause_event_blocks_dispatch():
    """A cleared pause_event freezes the reason loop; setting it resumes."""
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []
    pause_event = asyncio.Event()
    pause_event.set()
    recon_done = asyncio.Event()

    async def worker(decision: DispatchDecision, profile):
        calls.append(decision)
        if decision.intent_id == "recon-initial":
            pause_event.clear()  # operator pauses right after recon
            recon_done.set()
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(
                    intent_id="I1",
                    goal="probe login",
                    profile="exploit",
                    mode="explore",
                )
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
        pause_event=pause_event,
    )
    task = asyncio.create_task(swarm.run())
    await asyncio.wait_for(recon_done.wait(), 2)
    await asyncio.sleep(0.2)
    assert [c.intent_id for c in calls] == ["recon-initial"]  # frozen pre-dispatch
    pause_event.set()
    result = await asyncio.wait_for(task, 5)
    assert [c.mode for c in calls] == ["recon", "explore", "bootstrap"]
    assert result["solved"] is False


async def test_reason_path_hitl_pause_resume_gates_reason_loop(tmp_path):
    """Regression: HITL pause/resume must reach the ReasonSwarm loop.

    _run_reason_scheduler used to build ReasonSwarm without pause_event, so an
    operator pause reported success (operator_paused receipt) while the reason
    loop kept dispatching workers.
    """
    challenge = _challenge()
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    hitl: asyncio.Queue = asyncio.Queue()
    swarm = Swarm(
        challenge,
        llm=None,
        sandbox=sandbox,
        artifacts=arts,
        config=SolverConfig(),
        run_id="c-reason-pause",
        executor="cli",
        engines=["pi"],
        hitl_inbox=hitl,
    )

    async def fake_healthy(*args, **kwargs):
        return ["pi"]

    recon_started = asyncio.Event()
    release_recon = asyncio.Event()

    def fake_worker(*args, **kwargs):
        async def fake_run():
            # hold the initial recon open so the run stays alive while we
            # exercise pause/resume through the HITL inbox
            recon_started.set()
            await release_recon.wait()
            return _outcome()

        return SimpleNamespace(run=fake_run, solver_id="cli-pi")

    swarm._healthy_engines_async = fake_healthy
    swarm._make_cli_worker = fake_worker

    task = asyncio.create_task(swarm._run_reason_scheduler())
    try:
        # wait for the pause gate to be wired and the run to reach recon
        await asyncio.wait_for(recon_started.wait(), 5)
        gate = getattr(swarm, "_reason_pause_gate", None)
        assert gate is not None and gate.is_set()

        await hitl.put({"action": "pause", "text": "", "target": "global"})
        for _ in range(200):
            if not gate.is_set():
                break
            await asyncio.sleep(0.01)
        assert not gate.is_set(), "HITL pause must clear the ReasonSwarm pause gate"
        assert swarm._operator_paused is True

        await hitl.put({"action": "resume", "text": "", "target": "global"})
        for _ in range(200):
            if gate.is_set():
                break
            await asyncio.sleep(0.01)
        assert gate.is_set(), "HITL resume must re-set the ReasonSwarm pause gate"
        assert swarm._operator_paused is False
    finally:
        await hitl.put({"action": "stop", "text": "", "target": "global"})
        release_recon.set()
        outcome = await asyncio.wait_for(task, 10)
    assert outcome.reason == "operator_stop"


class _Bus:
    """Capture bus for structured-observability assertions."""

    def __init__(self):
        self.events: list = []

    async def emit(self, ev):
        self.events.append(ev)


def _delta_seq(bus: _Bus) -> list[str]:
    return [e.payload.get("delta_type") for e in bus.events]


async def test_reason_swarm_emits_structured_events():
    board = MemoryBoard("c-reason")
    bus = _Bus()

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        if decision.mode == "explore":
            return _outcome("flag{ok}")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(
                    intent_id="I1",
                    goal="probe login endpoint",
                    mode="explore",
                    priority=0.9,
                    from_facts=[1],
                )
            ],
            audit_notes=["verify /login again"],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-obs-1",
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()

    assert result["solved"] is True
    seq = _delta_seq(bus)

    def idx(name: str) -> int:
        assert name in seq, f"{name} missing from {seq}"
        return seq.index(name)

    assert (
        idx("recon_started")
        < idx("recon_completed")
        < idx("reason_cycle_started")
        < idx("intent_proposed")
        < idx("dispatch_decision")
        < idx("intent_completed")
        < idx("reason_cycle_completed")
        < idx("reason_loop_finished")
    )
    assert all(e.event_type == EventType.BLACKBOARD_DELTA for e in bus.events)
    assert all(e.run_id == "run-obs-1" for e in bus.events)
    assert all(e.challenge_id == "c-reason" for e in bus.events)

    proposed = next(
        e.payload for e in bus.events if e.payload.get("delta_type") == "intent_proposed"
    )
    for field in ("intent_id", "goal", "mode", "priority", "dedupe_key", "from_facts"):
        assert field in proposed, f"intent_proposed payload missing {field}"
    assert proposed["intent_id"] == "I1"
    assert proposed["goal"] == "probe login endpoint"
    assert proposed["mode"] == "explore"
    assert proposed["priority"] == 0.9
    assert proposed["from_facts"] == [1]
    assert proposed["stage"] == "reason"
    assert proposed["reason_cycle_id"] == "reason-1"

    started = next(
        e.payload
        for e in bus.events
        if e.payload.get("delta_type") == "reason_cycle_started"
    )
    assert started["generation"] == 1
    assert started["stage"] == "reason"

    completed = next(
        e.payload
        for e in bus.events
        if e.payload.get("delta_type") == "reason_cycle_completed"
    )
    assert completed["audit_notes"] == ["verify /login again"]
    assert completed["goal_met"] is False
    assert completed["planner"] == "deepseek-v4-pro"
    assert "duration_ms" in completed

    finished = bus.events[-1].payload
    assert finished["delta_type"] == "reason_loop_finished"
    assert finished["stage"] == "finalize"
    assert finished["stop_reason"] == "goal_met"


async def test_reason_swarm_emits_intent_skipped_for_duplicates():
    board = MemoryBoard("c-reason")
    bus = _Bus()

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(intent_id="I1", goal="probe login", mode="explore")
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-obs-2",
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    await swarm.run()

    skipped = [
        e.payload for e in bus.events if e.payload.get("delta_type") == "intent_skipped"
    ]
    assert skipped, f"no intent_skipped in {_delta_seq(bus)}"
    assert skipped[0]["skip_reason"] == "duplicate"
    assert skipped[0]["dedupe_key"]
    assert skipped[0]["intent_id"] == "I1"


async def test_reason_swarm_emits_fallback_dispatch():
    board = MemoryBoard("c-reason")
    bus = _Bus()

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        if decision.mode == "bootstrap":
            return _outcome("flag{fallback}")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-obs-3",
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()

    assert result["solved"] is True
    fallbacks = [
        e.payload
        for e in bus.events
        if e.payload.get("delta_type") == "fallback_dispatch"
    ]
    assert len(fallbacks) == 1
    assert fallbacks[0]["intent_id"] == "fallback-bootstrap"
    assert fallbacks[0]["reason"] == "no fresh intents"
    assert fallbacks[0]["stage"] == "dispatch"


async def test_reason_swarm_emits_intent_failed_on_worker_crash():
    board = MemoryBoard("c-reason")
    bus = _Bus()

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        if decision.mode == "explore":
            raise RuntimeError("worker exploded")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[Intent(intent_id="I1", goal="probe login", mode="explore")],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-obs-4",
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    await swarm.run()

    failed = [
        e.payload for e in bus.events if e.payload.get("delta_type") == "intent_failed"
    ]
    assert failed, f"no intent_failed in {_delta_seq(bus)}"
    assert failed[0]["intent_id"] == "I1"
    assert failed[0]["stage"] == "execute"
    assert "worker exploded" in failed[0]["error"]


async def test_reason_swarm_bus_failure_does_not_break_scheduling():
    class _BadBus:
        async def emit(self, ev):
            raise RuntimeError("bus down")

    board = MemoryBoard("c-reason")

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        if decision.mode == "explore":
            return _outcome("flag{ok}")
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[Intent(intent_id="I1", goal="probe login", mode="explore")],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=_BadBus(),
        worker_factory=worker,
        reason_fn=reason_fn,
    )
    result = await swarm.run()
    assert result["solved"] is True
    assert result["flags"] == ["flag{ok}"]


async def test_reason_intent_registration_failure_is_surfaced_once_without_blocking():
    class _FailingGraph:
        def propose_intent(self, **kwargs):
            raise RuntimeError("sqlite secret=/tmp/private.db")

    bus = _Bus()
    swarm = ReasonSwarm(_challenge(), graph=_FailingGraph(), bus=bus)
    decision = DispatchDecision(
        intent_id="I-db-fail", profile="pi-worker", direction="web",
        goal="probe the endpoint", mode="explore",
    )

    swarm._register_decision(decision)
    swarm._register_decision(decision)
    await asyncio.sleep(0)

    failures = [
        e.payload for e in bus.events
        if e.payload.get("delta_type") == "intent_db_write_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["intent_id"] == "I-db-fail"
    assert failures[0]["op"] == "propose"
    assert failures[0]["reason"] == "RuntimeError"
    assert "sqlite secret" not in repr(failures[0])
    assert len(failures[0]["reason"]) <= 160
    assert "probe the endpoint" not in failures[0]["reason"]


async def test_reason_registers_intents_before_worker_dispatch(tmp_path):
    """Reason decisions must exist in SQLite before workers attach products to them."""
    import sqlite3

    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_challenge())
    board = MemoryBoard("c-reason")
    calls: list[str] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision.intent_id)
        with sqlite3.connect(graph.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id=?", (decision.intent_id,)
            ).fetchone()
        assert row is not None, "intent row must precede worker execution"
        assert graph.claim_intent(worker="cli-pi", intent_id=decision.intent_id)
        if decision.intent_id == "I1":
            graph.flag_found(
                actor="cli-pi", flag="flag{registered}", intent_id=decision.intent_id
            )
            graph.conclude_intent(
                actor="cli-pi", intent_id=decision.intent_id, result="solved"
            )
            return SimpleNamespace(
                flag="flag{registered}", flags=["flag{registered}"],
                engine="pi", session="session-1",
            )
        graph.conclude_intent(
            actor="cli-pi", intent_id=decision.intent_id, result="explored"
        )
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[Intent(
                intent_id="I1", goal="probe the confirmed endpoint",
                mode="explore", from_facts=[],
            )],
            audit_notes=[],
        )

    reason_swarm = ReasonSwarm(
        _challenge(), board=board, graph=graph,
        worker_factory=worker, reason_fn=reason_fn,
    )
    result = await reason_swarm.run()

    assert calls == ["recon-initial", "I1"]
    assert result["winner_outcome"].session == "session-1"
    with sqlite3.connect(graph.db_path) as conn:
        rows = dict(conn.execute(
            "SELECT intent_id, status FROM intents "
            "WHERE intent_id IN ('recon-initial','I1')"
        ))
    assert rows == {"recon-initial": "done", "I1": "done"}
    graph.close()


async def test_reason_path_finalizes_board_and_persists_real_winner(tmp_path):
    """Regression for run-1803: Reason finalization must not bypass board/winner."""
    import json
    import sqlite3

    challenge = _challenge()
    workspace = tmp_path / "workspace"
    graph_dir = workspace / "graph"
    worker_root = workspace / "workers"
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=workspace / "arts")
    swarm = Swarm(
        challenge,
        llm=None,
        sandbox=sandbox,
        artifacts=arts,
        config=SolverConfig(),
        run_id="c-reason-finalize",
        executor="cli",
        engines=["pi"],
        graph_dir=graph_dir,
        worker_root=worker_root,
    )

    async def fake_healthy(*args, **kwargs):
        return ["pi"]

    def fake_worker(*args, **kwargs):
        intent_id = kwargs["intent_id"]

        async def fake_run():
            assert swarm.shared_graph is not None
            assert swarm.shared_graph.claim_intent(
                worker="cli-pi-2", intent_id=intent_id, lease_s=1000.0
            )
            fact_seq = swarm.shared_graph.add_evidence(
                actor="cli-pi-2", source="curl",
                fact="server output contains flag{finalized}",
                artifact_id="artifact-1", verified=True, intent_id=intent_id,
            )
            swarm.shared_graph.flag_found(
                actor="cli-pi-2", flag="flag{finalized}",
                artifact_id="artifact-1", intent_id=intent_id,
            )
            swarm.shared_graph.conclude_intent(
                actor="cli-pi-2", intent_id=intent_id,
                result="solved", to_fact_seq=fact_seq,
            )
            return SimpleNamespace(
                solved=True, flag="flag{finalized}", flags=["flag{finalized}"],
                engine="pi", session="session-real", workdir="", reason="solved",
            )

        return SimpleNamespace(run=fake_run, solver_id="cli-pi-2")

    swarm._healthy_engines_async = fake_healthy
    swarm._make_cli_worker = fake_worker
    outcome = await swarm._run_reason_scheduler()

    assert outcome.solved is True
    assert outcome.winner == "pi"
    board_text = (workspace / ".dswarm_board.md").read_text(encoding="utf-8")
    assert "flag{finalized}" in board_text
    winner = json.loads((workspace / "winner.json").read_text(encoding="utf-8"))
    assert winner["session"] == "session-real"
    assert winner["engine"] == "pi"
    assert winner["flag"] == "flag{finalized}"
    with sqlite3.connect(graph_dir / "shared_graph.db") as conn:
        row = conn.execute(
            "SELECT status, dispatch_state FROM intents WHERE intent_id='recon-initial'"
        ).fetchone()
    assert row == ("done", "closed")


async def test_operator_hint_intent_dispatched_before_fallback(tmp_path):
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    graph = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_challenge())
    info = graph.add_operator_directive(action="hint", text="CVE-2022-29078")
    op_id = f"I-{info['directive_id']}"
    graph.propose_intent(
        actor="operator", intent_id=op_id, goal="CVE-2022-29078",
        payload={"source": "operator_directive", "action": "hint",
                 "directive_id": info["directive_id"],
                 "worker_class": "shell_agent", "direction": "web",
                 "priority": "operator"},
    )

    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        return _outcome()

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    swarm = ReasonSwarm(
        _challenge(), board=board, worker_factory=worker,
        reason_fn=reason_fn, graph=graph,
    )
    await swarm.run()

    # dry reason: the open operator hint intent is dispatched as a focused
    # explore worker (pi-web), not orphaned in favour of the generic fallback.
    assert calls[0].mode == "recon"
    assert calls[1].intent_id == op_id
    assert calls[1].mode == "explore"
    assert calls[1].profile == "pi-web"
    graph.close()


async def test_reason_swarm_retries_retryable_provider_runtime_failure():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []
    reason_calls = {"n": 0}

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        if decision.mode == "recon":
            return _outcome()
        if len([c for c in calls if c.intent_id == "I1"]) == 1:
            return SimpleNamespace(
                flag=None,
                flags=[],
                engine="pi-worker",
                reason="pi CLI: runtime failure",
                provider_error={
                    "category": "transient_network",
                    "retryable": True,
                    "should_pause_dispatch": False,
                    "raw_message": "connection reset by peer",
                    "provider": "deepseek-main",
                    "account_id": "acct-primary",
                    "worker_id": "cli-pi#1",
                },
            )
        return _outcome("flag{recovered_after_retry}")

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        reason_calls["n"] += 1
        if reason_calls["n"] <= 2:
            return ReasonResult(
                goal_met=False,
                intents=[Intent(intent_id="I1", goal="probe login", mode="explore")],
                audit_notes=[],
            )
        return ReasonResult(goal_met=True, intents=[], audit_notes=[])

    class _Bus:
        def __init__(self):
            self.events = []
        async def emit(self, ev):
            self.events.append(ev)

    bus = _Bus()
    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-retry-provider",
        worker_factory=worker,
        reason_fn=reason_fn,
    )

    result = await swarm.run()

    i1_calls = [c for c in calls if c.intent_id == "I1"]
    assert len(i1_calls) == 2, "retryable provider runtime failure should free intent for redispatch"
    assert i1_calls[0].runtime_operation_kind == ""
    assert i1_calls[1].runtime_operation_kind == "recovery"
    assert result["solved"] is True
    assert result["flags"] == ["flag{recovered_after_retry}"]
    deltas = [e.payload for e in bus.events if e.event_type is EventType.BLACKBOARD_DELTA]
    assert any(d.get("kind") == "intent_failed" and d.get("intent_id") == "I1" for d in deltas)
    assert any(d.get("kind") == "worker_recovery_scheduled" and d.get("intent_id") == "I1" for d in deltas)


async def test_reason_swarm_emits_provider_batch_alert_for_many_fatal_errors():
    board = MemoryBoard("c-reason")
    calls: list[DispatchDecision] = []

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        calls.append(decision)
        if decision.mode == "recon":
            return _outcome()
        return SimpleNamespace(
            flag=None,
            flags=[],
            engine="pi-worker",
            reason="pi CLI: runtime failure",
            provider_error={
                "category": "insufficient_quota",
                "severity": "fatal",
                "retryable": False,
                "should_pause_dispatch": True,
                "raw_message": "402 insufficient balance",
                "provider": "deepseek-main",
                "account_id": "acct-primary",
                "worker_id": decision.intent_id,
                "user_message": "LLM 提供商返回余额/额度不足，继续派发会批量失败。",
                "suggested_action": "请检查账号余额、套餐额度或切换可用账号/模型后再恢复。",
            },
        )

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(intent_id="I1", goal="probe login", mode="explore", priority=1.0, direction="web"),
                Intent(intent_id="I2", goal="probe upload", mode="explore", priority=0.9, direction="misc"),
                Intent(intent_id="I3", goal="probe token", mode="explore", priority=0.8, direction="pwn"),
            ],
            audit_notes=[],
        )

    class _Bus:
        def __init__(self):
            self.events = []
        async def emit(self, ev):
            self.events.append(ev)

    bus = _Bus()
    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        bus=bus,
        run_id="run-provider-batch",
        worker_factory=worker,
        reason_fn=reason_fn,
        max_intents_per_reason=3,
        max_workers=3,
    )

    await swarm.run()

    alerts = [e for e in bus.events if e.event_type is EventType.PROVIDER_BATCH_ALERT]
    assert alerts, "three fatal provider errors in one window must alert the operator"
    payload = alerts[-1].payload
    assert payload["category"] == "insufficient_quota"
    assert payload["count"] >= 3
    assert payload["affected_workers"] >= 3
    assert payload["should_pause_dispatch"] is True
    assert "余额" in payload["user_message"]
    board_alerts = [e.payload for e in bus.events
                    if e.event_type is EventType.BLACKBOARD_DELTA
                    and e.payload.get("kind") == "provider_batch_alert"]
    assert board_alerts, "batch alert should also be visible on the blackboard timeline"

async def test_reason_swarm_caps_fresh_ordinary_workers_at_max_workers():
    board = MemoryBoard("c-reason")
    active = 0
    peak = 0
    first_pair_started = asyncio.Event()
    release = asyncio.Event()
    reason_calls = 0

    async def worker(decision: DispatchDecision, profile) -> SimpleNamespace:
        nonlocal active, peak
        if decision.mode == "recon":
            return _outcome()
        active += 1
        peak = max(peak, active)
        if active == 2:
            first_pair_started.set()
        try:
            await release.wait()
            return _outcome()
        finally:
            active -= 1

    async def reason_fn(summary: str, challenge_id: str) -> ReasonResult:
        nonlocal reason_calls
        reason_calls += 1
        if reason_calls > 1:
            return ReasonResult(goal_met=True, intents=[], audit_notes=[])
        return ReasonResult(
            goal_met=False,
            intents=[
                Intent(intent_id=f"I{i}", goal=f"probe {i}", mode="explore")
                for i in range(4)
            ],
            audit_notes=[],
        )

    swarm = ReasonSwarm(
        _challenge(),
        board=board,
        worker_factory=worker,
        reason_fn=reason_fn,
        max_workers=2,
        max_intents_per_reason=4,
    )
    task = asyncio.create_task(swarm.run())
    await asyncio.wait_for(first_pair_started.wait(), timeout=1.0)
    await asyncio.sleep(0)

    assert peak == 2
    assert active == 2

    release.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert peak == 2
    assert swarm.lane_gate.snapshot() == {
        "ordinary_active": 0,
        "review_active": 0,
    }


def test_reason_decision_preserves_worker_class_for_lane_routing():
    swarm = ReasonSwarm(_challenge())
    result = ReasonResult(
        goal_met=False,
        intents=[
            Intent(
                intent_id="I-verify",
                goal="reproduce candidate flag",
                worker_class="verifier",
                mode="explore",
            )
        ],
        audit_notes=[],
    )

    decision = swarm._decisions_from_reason(result)[0]

    assert decision.worker_class == "verifier"
    assert swarm.lane_gate.lane_for(
        mode=decision.mode,
        worker_class=decision.worker_class,
    ) == "review"

async def test_swarm_injects_its_worker_lane_gate_into_reason_scheduler(
    tmp_path, monkeypatch,
):
    challenge = _challenge()
    swarm = Swarm(
        challenge,
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sbx"),
        artifacts=ArtifactStore(root=tmp_path / "arts"),
        config=SolverConfig(),
        executor="cli",
        engines=["pi"],
        max_workers=3,
        review_policy={"enabled": True, "max_concurrent": 2},
    )
    captured = {}

    async def fake_healthy(*args, **kwargs):
        return ["pi"]

    class FakeReasonSwarm:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return {"solved": False, "flags": [], "winner_outcome": None}

    monkeypatch.setattr(swarm, "_healthy_engines_async", fake_healthy)
    monkeypatch.setattr("dswarm.swarm.swarm.ReasonSwarm", FakeReasonSwarm)

    await swarm._run_reason_scheduler()

    assert captured["lane_gate"] is swarm._worker_lane_gate
    assert swarm._worker_lane_gate.ordinary_limit == 3
    assert swarm._worker_lane_gate.review_limit == 2


def test_swarm_preserves_zero_review_lane_capacity(tmp_path):
    swarm = Swarm(
        _challenge(),
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sbx"),
        artifacts=ArtifactStore(root=tmp_path / "arts"),
        config=SolverConfig(),
        executor="cli",
        engines=["pi"],
        review_policy={"enabled": True, "max_concurrent": 0},
    )

    assert swarm.review_policy["max_concurrent"] == 0
    assert swarm._worker_lane_gate.review_limit == 0
    assert swarm._review_capacity_available() is False
