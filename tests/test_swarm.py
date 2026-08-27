"""Swarm + Insight Bus mechanics.

The execution layer is now CLI-only (the code-driven scripted-LLM path was
retired), so these tests exercise the swarm's coordination machinery directly:
the InsightBus fan-out, the CLI race lineup + degrade logic, and the coordinator's
plan / dispatch loop — all with the CLI subprocess stubbed out (no real engine).
"""

import asyncio
import hashlib
import time
from pathlib import Path

import pytest

from dswarm.core.llm import ModelSpec
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.swarm.insight_bus import InsightBus, InsightKind
from dswarm.swarm.swarm import Swarm


def test_worker_runtime_mixin_alloc_workdir_uses_workspace_initializer(tmp_path, monkeypatch):
    from dswarm.swarm.worker_runtime_mixin import WorkerRuntimeMixin

    called = {"n": 0}

    def fake_ensure_workspace(root, *, runtime):
        called["n"] += 1
        assert runtime == {
            "backend": "local",
            "run_id": "alloc-run",
        }
        (root / "workspace").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "dswarm.swarm.worker_runtime_mixin.ensure_workspace",
        fake_ensure_workspace,
    )

    class Probe(WorkerRuntimeMixin):
        pass

    probe = Probe()
    probe.worker_root = tmp_path / "workers"
    probe.workspace_root = tmp_path / "workspace"
    probe.worker_backend = "local"
    probe.run_id = "alloc-run"
    probe._worker_seq = 0

    workdir = probe._alloc_workdir("pi")

    assert called["n"] == 1
    assert workdir == str(tmp_path / "workers" / "cli-pi-1")


@pytest.fixture
def challenge() -> Challenge:
    return Challenge(id="c-swarm", name="swarm-test", category="web", points=50,
                     description="solve me", flag_format=r"flag\{[^}]+\}")


@pytest.fixture(autouse=True)
def _reset_health_probe_cache():
    """The health-probe cache is process-wide (so sibling runs share verdicts in
    production). In tests that means a stubbed roster could leak across cases — clear
    it before AND after each test so verdicts never bleed."""
    from dswarm.swarm import swarm as _swarm_mod
    _swarm_mod._health_cache_clear()
    yield
    _swarm_mod._health_cache_clear()




def test_unverified_flag_claim_queues_review(challenge, tmp_path: Path):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        graph_dir=tmp_path / "graph",
        stage_policy={"coordinator": {"review": {"enabled": True}}},
    )
    assert sw.shared_graph is not None
    sw.shared_graph.flag_unverified(
        actor="cli-pi-1", flag="flag{claimed}", reason="no command output witness")

    assert sw._queue_unverified_flag_review() is True
    assert sw._queued_review_requests
    assert sw._queued_review_requests[-1]["trigger"] == "unverified_flag"
    assert "FLAG_AUDIT" in sw._queued_review_requests[-1]["directive"]


def test_flag_unverified_graph_event_bridges_to_blackboard(challenge, tmp_path: Path):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        graph_dir=tmp_path / "graph",
        stage_policy={"coordinator": {"review": {"enabled": True}}},
    )
    assert sw.shared_graph is not None
    seq = sw.shared_graph.flag_unverified(actor="cli-pi-1", flag="flag{claimed}", reason="no witness")
    ev = sw.shared_graph.events_since(0, kinds=["flag_unverified"])[0]

    bridged = sw._graph_event_to_bb(ev)
    assert bridged == [("flag_unverified", {
        "flag": "flag{claimed}",
        "claim_state": "unverified",
        "status": "unverified",
        "reason": "no witness",
        "seq": seq,
        "claim_seq": seq,
        "source_actor": "cli-pi-1",
        "artifact_id": "",
    })]


def test_poc_reproduction_graph_event_bridge_redacts_indicator_and_command(challenge, tmp_path: Path):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        graph_dir=tmp_path / "graph",
    )
    ev = {
        "seq": 9,
        "kind": "poc_reproduction_registered",
        "actor": "cli-pi-1",
        "payload": {
            "poc_id": "poc-1",
            "reproduction_id": "poc-repro::1",
            "artifact_id": "artifact-1",
            "command": "python3 poc.py --token=do-not-leak",
            "indicator": "sensitive-observable",
        },
    }

    bridged = sw._graph_event_to_bb(ev)

    assert bridged == [("poc_reproduction_registered", {
        "seq": 9,
        "poc_id": "poc-1",
        "reproduction_id": "poc-repro::1",
        "status": "registered",
        "indicator_digest": hashlib.sha256(b"sensitive-observable").hexdigest(),
        "indicator_length": len("sensitive-observable"),
    })]
    assert "sensitive-observable" not in repr(bridged)
    assert "do-not-leak" not in repr(bridged)


def test_runtime_infra_fact_graph_event_does_not_bridge_to_blackboard(challenge, tmp_path: Path):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        graph_dir=tmp_path / "graph",
    )
    assert sw.shared_graph is not None
    sw.shared_graph.add_evidence(
        actor="cli-pi",
        source="worker",
        fact='[pi] Error: Unknown provider "dswarm-worker". Use --list-models to see available providers/models.',
        verified=False,
        confidence=0.2,
    )
    ev = sw.shared_graph.events_since(0, kinds=["fact_added"])[0]

    assert sw._graph_event_to_bb(ev) == []

    sw.shared_graph.add_evidence(
        actor="cli-pi", source="worker", fact="/robots.txt exposes /admin", verified=False
    )
    normal = sw.shared_graph.events_since(ev["seq"], kinds=["fact_added"])[0]
    bridged = sw._graph_event_to_bb(normal)
    assert bridged[0][0] == "fact_added"
    assert bridged[0][1]["fact"] == "/robots.txt exposes /admin"


# ── InsightBus: cross-solver fact/dead-end sharing ───────────────────────────

async def test_insight_bus_fan_out_excludes_producer() -> None:
    bus = InsightBus("c1")
    qa = bus.subscribe("A")
    qb = bus.subscribe("B")
    await bus.fact("A", "service is nginx 1.18")
    # B receives it, A does not get its own
    assert qb.get_nowait().text == "service is nginx 1.18"
    assert qa.empty()


async def test_insight_bus_backlog_for_late_subscriber() -> None:
    bus = InsightBus("c1")
    bus.subscribe("A")
    await bus.fact("A", "leaked cred admin:hunter2")
    await bus.dead_end("A", "SQLi on /login is patched")
    # C joins late -> gets the backlog (both, since neither was produced by C)
    qc = bus.subscribe("C")
    got = [qc.get_nowait() for _ in range(2)]
    kinds = {g.kind for g in got}
    assert InsightKind.FACT in kinds and InsightKind.DEAD_END in kinds


# ── CLI executor: race lineup + degrade (no real subprocess) ─────────────────





















def test_engines_roster_deduped(challenge, tmp_path: Path) -> None:
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               engines=["pi", "pi", "pi", "pi", "pi"])
    assert sw.engines == ["pi"]


# ── single-engine multi-instance: N same-engine profiles, distinct models ─────
# (P1 §10: "drag out 3 codex, each pinned to a different model; race them
# concurrently; dispatch later phases by priority".)

def _pi_trio_profiles():
    """Three pi profiles (same base engine), each a distinct model, with
    priorities deliberately OUT of declaration order so an order-preserving
    roster would NOT be priority-sorted. id == name (normalize copies one to the
    other)."""
    mk = lambda pid, model, prio: {
        "id": pid, "name": pid, "engine": "pi", "runtime": "local",
        "credential_account": "pi-main", "auth": "subscription",
        "roles": ["race", "bootstrap", "explore", "review"],
        "race": True, "max_running": 1, "priority": prio, "model": model,
        "enabled": True,
    }
    # declaration order a,b,c but priorities 30,10,20 → priority order is b,c,a
    return [mk("pi-a", "deepseek-reasoner", 30),
            mk("pi-b", "deepseek-v4-pro", 10),
            mk("pi-c", "deepseek-v4-flash", 20)]








def test_pick_engine_honors_priority_order(challenge, tmp_path: Path) -> None:
    # The priority sort only matters if the DISPATCHER actually honors it. This
    # proves _pick_engine returns the priority-TOP instance when all are idle.
    # NOTE the real semantics (§10.8 "语义澄清"): _running_count_for_candidate
    # aggregates by BASE ENGINE, so once ANY codex instance runs, the remaining
    # same-engine instances are NOT priority-distinguished (heterogeneity-first
    # treats them as "the codex slot, already covered"). Priority is authoritative
    # exactly when the contenders are idle — which is the all-idle pick below and
    # the classic-race lineup order (test_engines_roster_sorted_by_priority).
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               worker_profiles=_pi_trio_profiles())
    healthy = ["pi-a", "pi-b", "pi-c"]
    # _pick_engine(running_engines, healthy, *, role)
    # all idle → top priority (pi-b, prio 10) picked first
    assert sw._pick_engine([], healthy, role="bootstrap") == "pi-b"
    # all-idle pick is deterministic regardless of declaration order: priority wins
    assert sw._pick_engine([], list(reversed(healthy)), role="bootstrap") == "pi-b"


def test_priority_zero_is_highest_not_demoted(challenge, tmp_path: Path) -> None:
    # REGRESSION (GPT-5.5 §10 impl review): `int(priority or 100)` turned a legal
    # priority 0 (highest precedence, reachable via hand-edited JSON / API import —
    # coerce_nonneg_int keeps 0) into 100, sinking the top-priority profile. The
    # fix uses coerce_nonneg_int so 0 stays 0 and sorts FIRST.
    mk = lambda pid, prio: {
        "id": pid, "name": pid, "engine": "pi", "runtime": "local",
        "credential_account": "pi-main", "auth": "subscription",
        "roles": ["race", "bootstrap"], "race": True, "max_running": 1,
        "priority": prio, "model": "gpt-5.5", "enabled": True,
    }
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    # zero-prio declared LAST; must still sort first (not demoted to 100).
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               worker_profiles=[mk("pi-ten", 10), mk("pi-zero", 0)])
    assert sw.engines == ["pi-zero", "pi-ten"], sw.engines


def test_engines_priority_sort_only_when_profiles(challenge, tmp_path: Path) -> None:
    # GUARD: the priority sort is scoped to worker_profiles. A bare engine-name
    # roster (no profiles) must keep its given order untouched — this is what
    # test_engines_roster_deduped relies on and what the historical race lineup
    # expects.
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               engines=["pi", "pi"])
    assert sw.engines == ["pi"]




# ── _healthy_engines: silent-degrade NO LONGER silent ────────────────────────
# An engine dropped from the roster by a dispatch-time health-check failure now
# emits an `engine_degraded` blackboard delta (with the failure REASON) so the
# operator sees WHY it never showed up — instead of it vanishing from the panel.

def _bus_health_swarm(challenge, tmp_path, *, healthy: dict[str, bool]):
    """Coordinator swarm wired to a real EventBus, with each engine's
    health_detail() stubbed from `healthy` (name -> ok). Returns (swarm, events)
    where events is a live-appended list of every emitted Event."""
    import dswarm.solver.cli_driver as cd
    from dswarm.core.event_bus import EventBus
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    bus = EventBus()
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        bus=bus,
        engines=["pi"],
    )
    # these tests exercise probe → state-change → RE-PROBE semantics (degrade then
    # recover when a stubbed driver flips health). Disable the health-probe cache so
    # each _healthy_engines() call genuinely re-probes the (swapped) drivers.
    sw._health_probe_ttl = 0
    events: list = []
    bus.add_sink(lambda ev: events.append(ev) or _noop())  # sink must be awaitable
    orig = {n: cd.DRIVERS[n].health_detail for n in cd.DRIVERS}
    for n, drv in cd.DRIVERS.items():
        ok = healthy.get(n, True)
        # accept *a/**k — the probe path now calls health_detail(env=...)
        drv.health_detail = (  # type: ignore[method-assign]
            lambda *a, ok=ok, n=n, **k: (True, "") if ok
            else (False, f"Authentication required ({n})"))
    sw._restore_health = lambda: [setattr(cd.DRIVERS[n], "health_detail", orig[n]) for n in orig]
    return sw, events


async def _noop() -> None:
    return None


def _degrade_events(events):
    from dswarm.core.events import EventType
    return [e for e in events
            if e.event_type is EventType.BLACKBOARD_DELTA
            and (e.payload or {}).get("kind") == "engine_degraded"]


async def test_healthy_engines_emits_degrade_with_reason(challenge, tmp_path: Path) -> None:
    sw, events = _bus_health_swarm(challenge, tmp_path, healthy={"pi": False})
    try:
        roster = sw._healthy_engines()
        await asyncio.sleep(0)  # let the fire-and-forget emit tasks run
    finally:
        sw._restore_health()
    # pi dropped from the roster (the fallback keeps the swarm running)
    assert roster == ["pi"]
    degr = _degrade_events(events)
    assert len(degr) == 1
    p = degr[0].payload
    assert p["engine"] == "pi" and p["status"] == "degraded"
    assert "Authentication required" in p["reason"]


async def test_healthy_engines_degrade_deduped(challenge, tmp_path: Path) -> None:
    sw, events = _bus_health_swarm(challenge, tmp_path, healthy={"pi": False})
    try:
        sw._healthy_engines()
        sw._healthy_engines()  # same failure twice → still ONE event (no spam)
        await asyncio.sleep(0)
    finally:
        sw._restore_health()
    assert len(_degrade_events(events)) == 1


async def test_healthy_engines_recovery_event(challenge, tmp_path: Path) -> None:
    sw, events = _bus_health_swarm(challenge, tmp_path, healthy={"pi": False})
    try:
        sw._healthy_engines()              # pi down → degraded
        await asyncio.sleep(0)
        # pi logs back in
        import dswarm.solver.cli_driver as cd
        cd.DRIVERS["pi"].health_detail = lambda *a, **k: (True, "")  # type: ignore[method-assign]
        roster = sw._healthy_engines()     # pi back → recovered event
        await asyncio.sleep(0)
    finally:
        sw._restore_health()
    assert "pi" in roster
    degr = _degrade_events(events)
    statuses = [e.payload["status"] for e in degr]
    assert statuses == ["degraded", "recovered"]


def test_container_backend_health_probe_defers_to_worker_container(
        challenge, tmp_path: Path, monkeypatch) -> None:
    """Container workers must not be health-checked by a host-side CLI probe."""
    import dswarm.solver.cli_driver as cd

    sw = _coordinator_swarm(challenge, tmp_path, worker_backend="container")
    called = {"driver_for": False}

    def fail_driver_for(*args, **kwargs):
        called["driver_for"] = True
        raise AssertionError("host CLI health probe must not run for container workers")

    monkeypatch.setattr(cd, "driver_for", fail_driver_for)
    ok, detail = sw._probe_engine_health("pi", "bootstrap")

    assert ok is True
    assert detail == "deferred to worker container"
    assert called["driver_for"] is False




async def _empty_health():
    return []


async def test_healthy_engines_async_does_not_block_event_loop(
        challenge, tmp_path: Path, monkeypatch) -> None:
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        engines=["pi"],
    )

    def slow_probe():
        time.sleep(0.15)
        return ["pi"]

    monkeypatch.setattr(sw, "_healthy_engines", slow_probe)
    ticks = 0
    done = False

    async def ticker():
        nonlocal ticks
        while not done:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        assert await sw._healthy_engines_async() == ["pi"]
    finally:
        done = True
        await task

    assert ticks > 0


# ── health-probe latency fix: parallel probes + short-TTL cache ──────────────
# The "dispatch freezes for ~a minute" symptom: _healthy_engines shelled a real
# one-turn CLI hello per engine SERIALLY (60–150s timeout each) on the critical
# path before the first worker spawned. These cover the two fixes.

def _probe_swarm(challenge, tmp_path, engines):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    return Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
        engines=list(engines),
    )


def test_healthy_engines_probes_run_in_parallel(challenge, tmp_path: Path,
                                                 monkeypatch) -> None:
    # three profiles, each probe sleeps 0.3s. SERIAL → ~0.9s; PARALLEL → ~0.3s.
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               worker_profiles=_pi_trio_profiles())

    def slow_probe(name, role):
        time.sleep(0.3)
        return True, ""

    monkeypatch.setattr(sw, "_probe_engine_health", slow_probe)
    t0 = time.monotonic()
    roster = sw._healthy_engines()
    elapsed = time.monotonic() - t0
    assert sorted(roster) == ["pi-a", "pi-b", "pi-c"]
    # generous bound: parallel must finish well under the 0.9s serial cost.
    assert elapsed < 0.7, f"probes look serial: {elapsed:.2f}s for 3×0.3s"


def test_healthy_engines_caches_verdicts_within_ttl(challenge, tmp_path: Path,
                                                    monkeypatch) -> None:
    # a SECOND dispatch within the TTL must reuse verdicts, not re-probe.
    sw = _probe_swarm(challenge, tmp_path, ["pi"])
    calls: list[str] = []

    def counting_probe(name, role):
        calls.append(name)
        return True, ""

    monkeypatch.setattr(sw, "_probe_engine_health", counting_probe)
    assert sorted(sw._healthy_engines()) == ["pi"]
    assert sorted(calls) == ["pi"]  # first sweep probes it
    calls.clear()
    assert sorted(sw._healthy_engines()) == ["pi"]
    assert calls == []  # second sweep served entirely from cache


def test_healthy_engines_ttl_zero_disables_cache(challenge, tmp_path: Path,
                                                 monkeypatch) -> None:
    sw = _probe_swarm(challenge, tmp_path, ["pi"])
    sw._health_probe_ttl = 0
    n = {"count": 0}

    def counting_probe(name, role):
        n["count"] += 1
        return True, ""

    monkeypatch.setattr(sw, "_probe_engine_health", counting_probe)
    sw._healthy_engines()
    sw._healthy_engines()
    assert n["count"] == 2  # ttl=0 → every sweep re-probes


def test_healthy_engines_failure_cached_shorter(challenge, tmp_path: Path,
                                                monkeypatch) -> None:
    # a FAILED verdict expires at a fraction of the TTL so a recovered engine
    # rejoins quickly. With a tiny TTL the failure window lapses between sweeps.
    sw = _probe_swarm(challenge, tmp_path, ["pi"])
    sw._health_probe_ttl = 0.4  # failure horizon = 0.4 * 0.25 = 0.1s
    state = {"ok": False, "calls": 0}

    def flip_probe(name, role):
        state["calls"] += 1
        return state["ok"], "" if state["ok"] else "down"

    monkeypatch.setattr(sw, "_probe_engine_health", flip_probe)
    sw._healthy_engines()              # pi down → cached fail (short horizon)
    assert state["calls"] == 1
    state["ok"] = True
    time.sleep(0.15)                   # past the failure horizon, within the TTL
    roster = sw._healthy_engines()     # must RE-probe and see recovery
    assert state["calls"] == 2
    assert roster == ["pi"]


# ── launch-time deployed-skill reconciliation (run-75378 drift gap) ──────────

async def test_reconcile_blackboard_skill_resyncs_and_emits(challenge, tmp_path,
                                                             monkeypatch):
    """A non-container run reconciles deployed skill copies at launch: when something
    was stale it re-syncs AND emits a board delta so the drift is visible."""
    from dswarm.core.event_bus import EventBus
    from dswarm.solver import blackboard_skill

    captured = []
    bus = EventBus()

    async def _sink(ev):
        captured.append(ev)
    bus.add_sink(_sink)
    sw = _probe_swarm(challenge, tmp_path, ["claude"])
    sw.bus = bus
    sw.worker_backend = "local"

    monkeypatch.setattr(
        blackboard_skill, "sync_deployed_blackboard_skills",
        lambda: [{"path": "/home/u/.claude/skills/dswarm-blackboard/blackboard.py",
                  "status": "synced", "was": "stale(deadbeef0000)", "now": "cafebabe1111"}])

    await sw._reconcile_blackboard_skill()
    deltas = [e for e in captured if e.payload.get("kind") == "skill_resynced"]
    assert len(deltas) == 1
    assert "stale" in deltas[0].payload.get("summary", "")


async def test_reconcile_blackboard_skill_skips_container_backend(challenge, tmp_path,
                                                                  monkeypatch):
    """Container workers use the image-baked skill — the host reconcile must be skipped
    entirely (and never even call the sync)."""
    from dswarm.solver import blackboard_skill

    sw = _probe_swarm(challenge, tmp_path, ["claude"])
    sw.worker_backend = "container"
    called = {"n": 0}
    monkeypatch.setattr(blackboard_skill, "sync_deployed_blackboard_skills",
                        lambda: called.__setitem__("n", called["n"] + 1) or [])

    await sw._reconcile_blackboard_skill()
    assert called["n"] == 0


# ── Coordinator: evidence-driven plan / dispatch loop ────────────────────────

def _coordinator_swarm(challenge, tmp_path, **kw):
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    return Swarm(
        challenge, [ModelSpec(solver_id="seat", model="mock")],
        llm=None, sandbox=sandbox, artifacts=arts,
        executor="cli", **kw,
    )


def test_pick_engine_prefers_unrunning():
    # pi-only: the single engine is the pick whether or not it is running
    # (there is no second engine to prefer when idle).
    sw = Swarm.__new__(Swarm)
    healthy = ["pi"]
    assert sw._pick_engine([], healthy) == "pi"          # none running
    assert sw._pick_engine(["pi"], healthy) == "pi"     # busy → least-loaded fallback


def test_pick_engine_prefers_direction_profile(challenge, tmp_path: Path) -> None:
    from dswarm.solver.worker_profiles import normalize_worker_profiles

    profiles = normalize_worker_profiles([
        {"id": "pi-web", "name": "pi-web", "engine": "pi", "transport": "pi_cli"},
        {"id": "pi-pwn", "name": "pi-pwn", "engine": "pi", "transport": "pi_cli"},
    ])
    sw = _coordinator_swarm(challenge, tmp_path, worker_profiles=profiles,
                            engines=["pi-web", "pi-pwn"])
    # _profile_for_direction only returns profiles on this run's roster
    assert sw._profile_for_direction("pwn") == "pi-pwn"
    assert sw._profile_for_direction("crypto") == ""
    # preferred (healthy + has capacity) wins over the idle heuristic
    assert sw._pick_engine(["pi-web"], ["pi-web", "pi-pwn"], role="explore",
                           preferred="pi-pwn") == "pi-pwn"
    # preferred that is NOT on the roster → normal fallback
    assert sw._pick_engine([], ["pi-web", "pi-pwn"], role="explore",
                           preferred="pi-crypto") in ("pi-web", "pi-pwn")


def test_healthy_matches_seat_label_aliases(challenge, tmp_path: Path) -> None:
    from dswarm.solver.worker_profiles import normalize_worker_profiles

    profiles = normalize_worker_profiles([
        {"id": "seat_pi_web_x", "name": "seat_pi_web_x", "label": "pi-web",
         "engine": "pi", "transport": "pi_cli"},
        {"id": "seat_pi_pwn_x", "name": "seat_pi_pwn_x", "label": "pi-pwn",
         "engine": "pi", "transport": "pi_cli"},
    ])
    sw = _coordinator_swarm(challenge, tmp_path, worker_profiles=profiles,
                            engines=["seat_pi_web_x", "seat_pi_pwn_x"])

    assert sw._healthy_matches("pi-web", ["seat_pi_web_x"]) is True
    assert sw._healthy_matches("pi-pwn", ["seat_pi_web_x"]) is True


def test_pick_engine_resolves_seat_label_alias(challenge, tmp_path: Path) -> None:
    from dswarm.solver.worker_profiles import normalize_worker_profiles

    profiles = normalize_worker_profiles([
        {"id": "seat_pi_web_x", "name": "seat_pi_web_x", "label": "pi-web",
         "engine": "pi", "transport": "pi_cli"},
        {"id": "seat_pi_pwn_x", "name": "seat_pi_pwn_x", "label": "pi-pwn",
         "engine": "pi", "transport": "pi_cli"},
    ])
    sw = _coordinator_swarm(challenge, tmp_path, worker_profiles=profiles,
                            engines=["seat_pi_web_x", "seat_pi_pwn_x"])
    healthy = ["seat_pi_web_x", "seat_pi_pwn_x"]

    assert sw._pick_engine(["seat_pi_web_x"], healthy, role="explore",
                           preferred="pi-pwn") == "seat_pi_pwn_x"


def test_open_intents_carry_direction(challenge, tmp_path: Path) -> None:
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-dir", goal="crack the key",
        payload={"worker_class": "code", "direction": "crypto"})
    open_intents = sw._open_intents()
    row = next(i for i in open_intents if i["intent_id"] == "I-dir")
    assert row["direction"] == "crypto"


def test_direction_from_profile_id_and_link_helpers() -> None:
    from dswarm.swarm import swarm as swarm_mod

    assert swarm_mod._direction_from_profile_id("pi-web") == "web"
    assert swarm_mod._direction_from_profile_id("pi-aisec") == "aisec"
    assert swarm_mod._direction_from_profile_id("pi-worker") == ""
    assert swarm_mod._direction_from_profile_id("") == ""


def test_ensure_direction_links_graceful_for_all_directions(tmp_path: Path) -> None:
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    home.mkdir()
    for direction in ("web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"):
        swarm_mod._ensure_direction_links(home, direction)
    # vendored direction skills are linked for web; no crash for the others
    assert (home / ".pi" / "agent" / "skills" / "ctf-web").is_symlink()
    assert not (home / ".pi" / "agent" / "skills" / "dswarm-web").exists()


def test_ensure_direction_links_surfaces_btfly_category_skill(tmp_path: Path) -> None:
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    home.mkdir()
    swarm_mod._ensure_direction_links(home, "web")
    link = home / ".pi" / "agent" / "skills" / "web"
    assert link.is_symlink()
    # dangling container-absolute target (the worker container has it baked)
    assert link.readlink() == Path("/home/ctf/.pi/agent/skills/web")


def test_reason_backpressure_trips_on_large_ordinary_queue(challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=2)
    for i in range(4):
        sw.shared_graph.propose_intent(
            actor="reason", intent_id=f"I-{i}", goal=f"ordinary task {i}",
            payload={"worker_class": "code"})
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-review", goal="review task",
        payload={"worker_class": "review"})

    open_intents = sw._open_intents()

    assert sw._ordinary_open_queue_depth(open_intents) == 4
    assert sw._reason_backpressure_active(open_intents) is True


# ── race-scout cold-start invariant (run-75379 BUG④ ─────────────────────────
# race-scout is a cold-start warmup for an EMPTY graph. On a reopen/resume of a
# populated graph it re-races a challenge that already has facts (BUG④: 3 fresh
# bootstrap workers re-racing 33+ verified facts). The guard must be an INVARIANT
# of the coordinator, not something each caller remembers to pass.













def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f












async def test_graph_tail_bridge_drains_direct_db_writes_once_in_seq_order(
        challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=1)
    assert sw.shared_graph is not None
    g = sw.shared_graph
    fact_seq = g.add_evidence(actor="skill", source="blackboard",
                              fact="admin password is hunter2", verified=True)
    dead_seq = g.add_dead_end(actor="skill", reason="ftp anonymous is disabled")
    g.propose_intent(actor="reason", intent_id="I-bridge", goal="try ssh")
    g.claim_intent(worker="cli-1", intent_id="I-bridge")
    g.conclude_intent(
        actor="cli-1",
        intent_id="I-bridge",
        result="explored",
        result_detail="Tried ssh with hunter2 and got permission denied.",
    )

    emitted: list[tuple[str, dict]] = []

    async def emit(kind: str, **fields):
        emitted.append((kind, fields))

    await sw._drain_graph_to_bus(emit_bb=emit)

    assert [kind for kind, _ in emitted] == [
        "fact_added",
        "dead_end",
        "intent_proposed",
        "intent_claimed",
        "intent_concluded",
    ]
    assert emitted[0][1]["fact_seq"] == fact_seq
    assert emitted[1][1]["dead_end_seq"] == dead_seq
    assert emitted[-1][1]["result"] == "explored"
    assert "permission denied" in emitted[-1][1]["result_detail"]

    await sw._drain_graph_to_bus(emit_bb=emit)
    assert [kind for kind, _ in emitted] == [
        "fact_added",
        "dead_end",
        "intent_proposed",
        "intent_claimed",
        "intent_concluded",
    ]


async def test_graph_tail_bridge_advances_watermark_only_after_emit_success(
        challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=1)
    assert sw.shared_graph is not None
    fact_seq = sw.shared_graph.add_evidence(
        actor="skill", source="blackboard", fact="service is nginx", verified=True)

    async def failing_emit(kind: str, **fields):
        raise RuntimeError("sink down")

    await sw._drain_graph_to_bus(emit_bb=failing_emit)
    assert sw._last_graph_event_seq == 0

    emitted: list[tuple[str, dict]] = []

    async def emit(kind: str, **fields):
        emitted.append((kind, fields))

    await sw._drain_graph_to_bus(emit_bb=emit)
    assert sw._last_graph_event_seq == fact_seq
    assert len(emitted) == 1
    assert emitted[0][0] == "fact_added"


async def test_graph_tail_bridge_skips_poison_event_after_bounded_retries(
        challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=1)
    assert sw.shared_graph is not None
    fact_seq = sw.shared_graph.add_evidence(
        actor="skill", source="blackboard", fact="poison bridge event",
        verified=True)

    async def failing_emit(kind: str, **fields):
        raise RuntimeError("sink still down")

    await sw._drain_graph_to_bus(emit_bb=failing_emit)
    await sw._drain_graph_to_bus(emit_bb=failing_emit)
    assert sw._last_graph_event_seq == 0

    await sw._drain_graph_to_bus(emit_bb=failing_emit)
    assert sw._last_graph_event_seq == fact_seq

    emitted: list[tuple[str, dict]] = []

    async def emit(kind: str, **fields):
        emitted.append((kind, fields))

    await sw._drain_graph_to_bus(emit_bb=emit)
    assert emitted == []


def test_operator_hint_intent_orders_before_existing_queue(challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=2)
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-old", goal="old queued task",
        payload={"worker_class": "code"})
    sw.shared_graph.propose_intent(
        actor="operator", intent_id="I-operator-test", goal="manual hint task",
        payload={"source": "operator_hint", "action": "hint"})

    open_intents = sw._open_intents()

    assert [i["intent_id"] for i in open_intents[:2]] == [
        "I-operator-test", "I-old",
    ]
    assert open_intents[0]["priority"] == 100


async def test_review_intents_wait_when_review_concurrency_full(challenge, tmp_path: Path):
    sw = _coordinator_swarm(
        challenge, tmp_path,
        stage_policy={"coordinator": {"review": {
            "enabled": True, "max_concurrent": 1,
        }}},
    )

    async def sleeper():
        await asyncio.sleep(0.05)

    lane = await sw._worker_lane_gate.acquire(
        mode="review", worker_class="review"
    )
    task = asyncio.create_task(sleeper())
    sw._active_review_tasks.add(task)
    intents = [
        {"intent_id": "I-review", "goal": "audit", "worker_class": "review"},
        {"intent_id": "I-verify", "goal": "reproduce", "worker_class": "verifier"},
        {"intent_id": "I-code", "goal": "exploit", "worker_class": "code"},
    ]

    try:
        filtered = sw._dispatchable_open_intents(intents)
        assert [i["intent_id"] for i in filtered] == ["I-code"]
    finally:
        sw._worker_lane_gate.release(lane)
        await task


async def test_review_worker_uses_reserved_capacity_when_ordinary_slots_full(
    challenge, tmp_path: Path, monkeypatch,
):
    sw = _coordinator_swarm(
        challenge, tmp_path, max_workers=1,
        stage_policy={"coordinator": {"review": {
            "enabled": True, "engine": "pi", "max_concurrent": 1,
            "cooldown_events": 0, "max_review_workers": 3,
        }}},
    )

    async def long_running():
        await asyncio.sleep(3600)

    ordinary = asyncio.create_task(long_running(), name="ordinary")
    tasks = {ordinary: "codex"}
    task_solvers = {ordinary: object()}
    emitted: list[tuple[str, dict]] = []

    class FakeReviewWorker:
        solver_id = "cli-pi-review"

        async def run(self):
            await asyncio.sleep(3600)

    review_factory_calls: list[dict[str, object]] = []

    monkeypatch.setattr(sw, "_select_review_engine", lambda healthy: "pi")

    def make_review_worker(engine: str, **kwargs: object):
        review_factory_calls.append({"engine": engine, **kwargs})
        return FakeReviewWorker()

    monkeypatch.setattr(sw, "_make_cli_worker", make_review_worker)

    async def emit_bb(kind, **fields):
        emitted.append((kind, fields))

    try:
        started = await sw._maybe_start_review(
            trigger="operator_hint",
            directive="audit duplicated candidates",
            healthy=["pi", "codex"],
            tasks=tasks,
            task_solvers=task_solvers,
            emit_bb=emit_bb,
        )

        assert started is True
        assert len(tasks) == 2
        assert sum(
            1 for task in tasks
            if task.get_name().startswith("review-")
        ) == 1
        assert any(k == "review_started" for k, _ in emitted)
        assert review_factory_calls[0]["runtime_operation_kind"] == "review"
        assert sw._worker_lane_gate.snapshot() == {
            "ordinary_active": 0, "review_active": 1,
        }
    finally:
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert sw._worker_lane_gate.snapshot() == {
        "ordinary_active": 0, "review_active": 0,
    }


async def test_review_intent_remains_dispatchable_when_ordinary_slots_full(
    challenge, tmp_path: Path,
):
    sw = _coordinator_swarm(
        challenge, tmp_path, max_workers=1,
        stage_policy={"coordinator": {"review": {
            "enabled": True, "max_concurrent": 1,
        }}},
    )

    async def long_running():
        await asyncio.sleep(3600)

    ordinary_lane = await sw._worker_lane_gate.acquire(mode="explore")
    ordinary = asyncio.create_task(long_running(), name="ordinary")
    tasks = {ordinary: "codex"}
    intents = [
        {"intent_id": "I-code", "goal": "exploit", "worker_class": "code"},
        {"intent_id": "I-review", "goal": "audit", "worker_class": "review"},
    ]

    try:
        filtered = sw._capacity_dispatchable_open_intents(
            sw._dispatchable_open_intents(intents), tasks)

        assert [i["intent_id"] for i in filtered] == ["I-review"]
    finally:
        sw._worker_lane_gate.release(ordinary_lane)
        ordinary.cancel()


async def test_run_reason_passes_standing_guidance(challenge, tmp_path: Path, monkeypatch):
    from dswarm.solver.reason import ReasonResult

    sw = _coordinator_swarm(challenge, tmp_path)
    sw.llm = object()
    sw._standing_guidance = ["Use the VPS tunnel; do not test internal hosts from the Mac."]
    seen: dict[str, list[str]] = {}
    original = sw.shared_graph.to_reason_summary

    def fake_summary(*, standing_guidance=None):
        seen["standing"] = list(standing_guidance or [])
        return original(standing_guidance=standing_guidance)

    async def fake_run_reason(**_kwargs):
        return ReasonResult(goal_met=False, intents=[], audit_notes=[])

    monkeypatch.setattr(sw.shared_graph, "to_reason_summary", fake_summary)
    monkeypatch.setattr("dswarm.solver.reason.run_reason", fake_run_reason)

    proposed = await sw._run_reason()

    assert proposed == 0
    assert seen["standing"] == sw._standing_guidance


async def test_run_reason_persists_model_selected_fact_pins(
        challenge, tmp_path: Path, monkeypatch):
    from dswarm.solver.reason import ReasonResult

    sw = _coordinator_swarm(challenge, tmp_path)
    sw.llm = object()
    fact_seq = sw.shared_graph.add_evidence(
        actor="cli-a", source="cmd", fact="后台口令是 admin / 猎人二号",
        verified=True)
    for i in range(10):
        sw.shared_graph.add_evidence(
            actor=f"noise-{i}", source="scan", fact=f"old noise {i}",
            verified=True)
    seen: dict[str, str] = {}

    async def fake_run_reason(**kwargs):
        seen["fact_index"] = kwargs.get("fact_index", "")
        return ReasonResult(goal_met=False, intents=[], audit_notes=[],
                            pinned_facts=[fact_seq])

    monkeypatch.setattr("dswarm.solver.reason.run_reason", fake_run_reason)

    proposed = await sw._run_reason()

    assert proposed == 0
    assert f"[#{fact_seq}]" in seen["fact_index"]
    assert fact_seq in sw.shared_graph.pinned_fact_seqs()
    assert "后台口令是 admin" in sw.shared_graph.to_reason_summary()


async def test_coordinator_applies_tier1_review_proposal(challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path)
    fact_seq = sw.shared_graph.add_evidence(
        actor="cli-a", source="pi", fact="JWT alg is HS256",
        verified=True, artifact_id="a1")
    sw.shared_graph.add_review_proposal(
        actor="cli-review", marker="FACT_CHALLENGE",
        payload={
            "fact_seq": fact_seq,
            "reason": "no raw header proof",
            "verification_goal": "Decode a real JWT header.",
        },
    )
    emitted: list[tuple[str, dict]] = []

    async def emit_bb(kind, **fields):
        emitted.append((kind, fields))

    applied = await sw._drain_review_proposals(emit_bb=emit_bb)

    assert applied == 1
    assert sw.shared_graph.challenged_facts()[0]["fact_seq"] == fact_seq
    assert any(k == "review_proposal_decision" and f["decision"] == "accepted"
               for k, f in emitted)


async def test_route_suppress_proposal_requires_three_real_failures(
    challenge, tmp_path: Path,
):
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.add_review_proposal(
        actor="cli-review", marker="ROUTE_SUPPRESS",
        payload={"route_hash": "web:login:sqli", "reason": "loop", "confidence": 0.95},
        tier="tier2",
    )
    emitted: list[tuple[str, dict]] = []

    async def emit_bb(kind, **fields):
        emitted.append((kind, fields))

    assert await sw._drain_review_proposals(emit_bb=emit_bb) == 0
    assert not sw.shared_graph.is_route_suppressed("web:login:sqli")
    assert emitted[-1][1]["decision"] == "deferred"

    for i in range(3):
        iid = f"I-fail-{i}"
        worker = f"cli-worker-{i}"
        sw.shared_graph.propose_intent(
            actor="reason", intent_id=iid, goal=f"try login SQLi {i}",
            payload={"worker_class": "code", "route_hash": "web:login:sqli"})
        sw.shared_graph.claim_intent(worker=worker, intent_id=iid)
        sw.shared_graph.conclude_intent(
            actor=worker, intent_id=iid, result="dead: failed with no flag")
    sw.shared_graph.add_review_proposal(
        actor="cli-review", marker="ROUTE_SUPPRESS",
        payload={"route_hash": "web:login:sqli", "reason": "loop now proven",
                 "confidence": 0.95},
        tier="tier2",
    )

    assert await sw._drain_review_proposals(emit_bb=emit_bb) == 1
    assert sw.shared_graph.is_route_suppressed("web:login:sqli")


def test_open_intents_dedupes_same_route_but_keeps_review(challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-a", goal="try login SQLi variant A",
        payload={"worker_class": "code", "route_hash": "web:login:sqli"})
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-b", goal="try login SQLi variant B",
        payload={"worker_class": "code", "route_hash": "web:login:sqli"})
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-review", goal="review login SQLi loop",
        payload={"worker_class": "review", "route_hash": "web:login:sqli"})

    ids = [i["intent_id"] for i in sw._open_intents()]

    assert ids == ["I-a", "I-review"]


def test_lane_lock_proposal_locks_and_next_intent_keeps_lane(
    challenge, tmp_path: Path,
):
    async def _run():
        sw = _coordinator_swarm(challenge, tmp_path)
        lane = "destructive:tcp:445@172.22.11.45"
        sw.shared_graph.add_review_proposal(
            actor="cli-review", marker="LANE_LOCK",
            payload={"lane_key": lane, "risk_class": "destructive",
                     "owner_worker": "cli-review", "reason": "serialize exploit"},
            tier="tier2",
        )
        emitted: list[tuple[str, dict]] = []

        async def emit_bb(kind, **fields):
            emitted.append((kind, fields))

        assert await sw._drain_review_proposals(emit_bb=emit_bb) == 1
        assert sw.shared_graph.active_lanes()[0]["lane_key"] == lane
        assert any(k == "lane_locked" for k, _ in emitted)

        sw.shared_graph.add_review_proposal(
            actor="cli-review", marker="NEXT_INTENT",
            payload={"id": "I-next", "goal": "retry SMB after lock",
                     "lane_key": lane, "risk_class": "destructive"},
        )
        assert await sw._drain_review_proposals(emit_bb=emit_bb) == 1
        with sw.shared_graph._lock:
            row = sw.shared_graph._conn.execute(
                "SELECT lane_key, risk_class FROM intents WHERE intent_id='I-next'"
            ).fetchone()
        assert row == (lane, "destructive")

    asyncio.run(_run())


def test_next_intent_infers_lane_from_goal_text(challenge, tmp_path: Path):
    async def _run():
        sw = _coordinator_swarm(challenge, tmp_path)
        emitted: list[tuple[str, dict]] = []

        async def emit_bb(kind, **fields):
            emitted.append((kind, fields))

        sw.shared_graph.add_review_proposal(
            actor="cli-review", marker="NEXT_INTENT",
            payload={
                "id": "I-review-lane-text",
                "goal": (
                    "CONSOLIDATED FINAL DIAGNOSTIC under "
                    "destructive:tcp:5000@107.170.15.231 lane: perform one "
                    "serialized exploit verification and do not fan out."
                ),
                "worker_class": "code",
            },
        )

        assert await sw._drain_review_proposals(emit_bb=emit_bb) == 1
        with sw.shared_graph._lock:
            row = sw.shared_graph._conn.execute(
                "SELECT lane_key, risk_class FROM intents "
                "WHERE intent_id='I-review-lane-text'"
            ).fetchone()
        assert row == ("destructive:tcp:5000@107.170.15.231", "destructive")
        assert any(
            fields.get("lane_key") == "destructive:tcp:5000@107.170.15.231"
            for kind, fields in emitted
            if kind == "intent_proposed"
        )

    asyncio.run(_run())


def test_open_intents_stays_pure_when_lane_is_locked(challenge, tmp_path: Path):
    sw = _coordinator_swarm(challenge, tmp_path)
    lane = "destructive:tcp:445@172.22.11.45"
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-lane", goal="exploit smb",
        payload={"lane_key": lane, "risk_class": "destructive"})
    sw.shared_graph.lock_lane(
        actor="coord", lane_key=lane, risk_class="destructive",
        owner_worker="other", owner_intent="I-other")
    before = len(sw.shared_graph.events())
    first = sw._open_intents()
    second = sw._open_intents()
    after = len(sw.shared_graph.events())
    assert first == second
    assert first[0]["lane_key"] == lane
    assert before == after
    assert not any(e["kind"] == "intent_lane_deferred" for e in sw.shared_graph.events())


def test_open_intents_backfills_structured_lane_from_existing_goal_text(
    challenge, tmp_path: Path,
):
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.propose_intent(
        actor="coordinator",
        intent_id="I-old-lane",
        goal=(
            "CONSOLIDATED FINAL DIAGNOSTIC under "
            "destructive:tcp:5000@107.170.15.231 lane; run exactly one probe."
        ),
        payload={"worker_class": "verifier"},
    )
    sw.shared_graph.propose_intent(
        actor="coordinator",
        intent_id="I-vague-lane",
        goal="Under the lane, mutate one field and compare timing.",
        payload={"worker_class": "verifier"},
    )

    rows = {it["intent_id"]: it for it in sw._open_intents()}
    assert rows["I-old-lane"]["lane_key"] == "destructive:tcp:5000@107.170.15.231"
    assert rows["I-old-lane"]["risk_class"] == "destructive"
    assert rows["I-vague-lane"]["lane_key"] == ""
    with sw.shared_graph._lock:
        db_rows = dict(sw.shared_graph._conn.execute(
            "SELECT intent_id, COALESCE(lane_key, '') FROM intents "
            "WHERE intent_id IN ('I-old-lane', 'I-vague-lane')"
        ).fetchall())
    assert db_rows["I-old-lane"] == "destructive:tcp:5000@107.170.15.231"
    assert db_rows["I-vague-lane"] == ""




def test_open_intents_uses_shared_graph_public_dispatch_api(
    challenge, tmp_path: Path,
):
    """Coordinator dispatch must not reach into SQLiteSharedGraph internals.

    This keeps the Swarm layer backend-swappable: an HTTP/future graph adapter can
    expose the SharedGraph protocol without leaking SQLite's private _conn/_lock.
    """
    sw = _coordinator_swarm(challenge, tmp_path)
    graph = sw.shared_graph
    graph.propose_intent(
        actor="coordinator",
        intent_id="I-public-api-lane",
        goal=(
            "CONSOLIDATED FINAL DIAGNOSTIC under "
            "destructive:tcp:5000@107.170.15.231 lane; run exactly one probe."
        ),
        payload={"worker_class": "code"},
    )

    class PublicGraphOnly:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.annotated: list[tuple[str, str, str]] = []

        def dispatchable_intents(self, *, now=None):
            return self.wrapped.dispatchable_intents(now=now)

        def annotate_intent_lane(self, *, intent_id: str, lane_key: str,
                                 risk_class: str = ""):
            self.annotated.append((intent_id, lane_key, risk_class))
            return self.wrapped.annotate_intent_lane(
                intent_id=intent_id, lane_key=lane_key, risk_class=risk_class)

        def is_route_suppressed(self, route_hash: str) -> bool:
            return self.wrapped.is_route_suppressed(route_hash)

        def check_resource_conflicts(self, **kwargs):
            return self.wrapped.check_resource_conflicts(**kwargs)

    public_graph = PublicGraphOnly(graph)
    sw.shared_graph = public_graph

    rows = sw._open_intents()

    assert rows == [{
        "intent_id": "I-public-api-lane",
        "goal": (
            "CONSOLIDATED FINAL DIAGNOSTIC under "
            "destructive:tcp:5000@107.170.15.231 lane; run exactly one probe."
        ),
        "worker_class": "code",
        "route_hash": "",
        "branch_id": "",
        "priority": 0.0,
        "priority_scale": "planner",
        "lane_key": "destructive:tcp:5000@107.170.15.231",
        "risk_class": "destructive",
        "resource_key": "",
        "direction": "",
    }]
    assert public_graph.annotated == [(
        "I-public-api-lane",
        "destructive:tcp:5000@107.170.15.231",
        "destructive",
    )]


def test_pick_engine_least_loaded_when_all_running():
    sw = Swarm.__new__(Swarm)
    healthy = ["pi"]
    # the one pi entry running → least-loaded returns it
    assert sw._pick_engine(["pi"], healthy) == "pi"


def test_pick_engine_three_profiles_no_heterogeneity(challenge, tmp_path: Path):
    # with a pi-only profile roster there is no second engine to prefer: a running
    # pi worker marks EVERY pi profile as running (base-engine match), so the pick
    # falls back to the priority-ordered least-loaded candidate.
    sandbox = SandboxManager(root=tmp_path / "sbx")
    arts = ArtifactStore(root=tmp_path / "arts")
    sw = Swarm(challenge, [ModelSpec(solver_id="seat", model="mock")],
               llm=None, sandbox=sandbox, artifacts=arts, executor="cli",
               worker_profiles=_pi_trio_profiles())
    healthy = ["pi-a", "pi-b", "pi-c"]
    assert sw._pick_engine([], healthy) == "pi-b"            # idle → top priority
    assert sw._pick_engine(["pi-b"], healthy) == "pi-b"     # running → least-loaded


# ── operator runtime worker control (spawn/kill a specific engine) ───────────

class _FakeWorker:
    def __init__(self, engine: str):
        self.solver_id = f"cli-{engine}-op"
        self.cancelled = False

    async def run(self):
        await asyncio.sleep(3600)  # long-lived until cancelled

    def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_apply_worker_cmds_spawn_then_kill(challenge, tmp_path, monkeypatch):
    sw = _coordinator_swarm(challenge, tmp_path, engines=["pi", "codex"])
    sw.worker_cmds = asyncio.Queue()
    worker_factory_calls: list[dict[str, object]] = []

    def make_operator_worker(engine: str, **kwargs: object):
        worker_factory_calls.append({"engine": engine, **kwargs})
        return _FakeWorker(engine)

    monkeypatch.setattr(sw, "_make_cli_worker", make_operator_worker)

    tasks: dict = {}
    task_solvers: dict = {}
    emitted: list = []

    async def emit_bb(kind, **f):
        emitted.append((kind, f))

    # spawn a claude worker on demand
    sw.worker_cmds.put_nowait({"action": "spawn", "engine": "pi"})
    await sw._apply_worker_cmds(
        tasks=tasks, task_solvers=task_solvers, healthy=["pi", "codex"],
        running_engines_fn=lambda: list(tasks.values()), emit_bb=emit_bb)
    assert len(tasks) == 1
    w = next(iter(task_solvers.values()))
    assert w.solver_id == "cli-pi-op"
    assert worker_factory_calls[0]["runtime_operation_kind"] == "bootstrap"
    assert any(k == "worker_spawned" for k, _ in emitted)
    assert sw._worker_lane_gate.snapshot() == {
        "ordinary_active": 1, "review_active": 0,
    }

    # kill it by solver_id → solver cancelled + worker_killed emitted
    sw.worker_cmds.put_nowait({"action": "kill", "solver_id": "cli-pi-op"})
    await sw._apply_worker_cmds(
        tasks=tasks, task_solvers=task_solvers, healthy=["pi", "codex"],
        running_engines_fn=lambda: list(tasks.values()), emit_bb=emit_bb)
    assert w.cancelled is True
    assert any(k == "worker_killed" for k, _ in emitted)

    for t in list(tasks):
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert sw._worker_lane_gate.snapshot() == {
        "ordinary_active": 0, "review_active": 0,
    }


@pytest.mark.asyncio
async def test_apply_worker_cmds_multiple_spawns_keep_worker_and_lane_isolated(
    challenge, tmp_path, monkeypatch,
):
    sw = _coordinator_swarm(
        challenge, tmp_path, engines=["pi", "codex"], max_workers=2,
    )
    sw.worker_cmds = asyncio.Queue()
    started: list[str] = []

    class RecordingWorker(_FakeWorker):
        async def run(self):
            started.append(self.solver_id)

    monkeypatch.setattr(
        sw, "_make_cli_worker", lambda engine, **kw: RecordingWorker(engine),
    )
    tasks: dict = {}
    task_solvers: dict = {}

    async def emit_bb(kind, **fields):
        return None

    sw.worker_cmds.put_nowait({"action": "spawn", "engine": "pi"})
    sw.worker_cmds.put_nowait({"action": "spawn", "engine": "codex"})
    await sw._apply_worker_cmds(
        tasks=tasks,
        task_solvers=task_solvers,
        healthy=["pi", "codex"],
        running_engines_fn=lambda: list(tasks.values()),
        emit_bb=emit_bb,
    )
    await asyncio.gather(*tasks)

    assert sorted(started) == ["cli-codex-op", "cli-pi-op"]
    assert sw._worker_lane_gate.snapshot() == {
        "ordinary_active": 0, "review_active": 0,
    }


@pytest.mark.asyncio
async def test_apply_worker_cmds_rejects_unknown_engine_and_max(challenge, tmp_path, monkeypatch):
    sw = _coordinator_swarm(challenge, tmp_path, engines=["pi"])
    sw.worker_cmds = asyncio.Queue()
    monkeypatch.setattr(sw, "_make_cli_worker",
                        lambda engine, **kw: _FakeWorker(engine))
    tasks: dict = {}
    task_solvers: dict = {}
    emitted: list = []

    async def emit_bb(kind, **f):
        emitted.append((kind, f))

    # a bogus engine is NOT in this swarm's roster → spawn rejected
    sw.worker_cmds.put_nowait({"action": "spawn", "engine": "bogus"})
    await sw._apply_worker_cmds(
        tasks=tasks, task_solvers=task_solvers, healthy=["pi"],
        running_engines_fn=lambda: list(tasks.values()), emit_bb=emit_bb)
    assert len(tasks) == 0
    assert any(k == "worker_spawn_rejected" and f.get("reason") == "unknown_engine"
               for k, f in emitted)

    # at max_workers → spawn rejected
    emitted.clear()
    sw.max_workers = 0
    sw.worker_cmds.put_nowait({"action": "spawn", "engine": "pi"})
    await sw._apply_worker_cmds(
        tasks=tasks, task_solvers=task_solvers, healthy=["pi"],
        running_engines_fn=lambda: list(tasks.values()), emit_bb=emit_bb)
    assert len(tasks) == 0
    assert any(k == "worker_spawn_rejected" and f.get("reason") == "max_workers"
               for k, f in emitted)




async def test_coordinator_multiflag_waits_for_all(challenge, tmp_path: Path, monkeypatch):
    """expected_flags=2: one worker returning ONE flag must NOT end the run; the
    coordinator keeps going until two distinct flags are collected."""
    from dswarm.solver.types import SolveOutcome

    challenge.expected_flags = 2
    sw = _coordinator_swarm(challenge, tmp_path, start_workers=2)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["pi", "codex"])

    # each engine returns a DIFFERENT single flag; neither alone completes the run.
    sw = _coordinator_swarm(challenge, tmp_path, start_workers=2)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["pi", "pi"])

    flag_pool = ["flag{a}", "flag{b}"]
    spawn = {"n": 0}

    class FakeWorker:
        def __init__(self, engine):
            self.solver_id = f"cli-{engine}"
            self._f = flag_pool[spawn["n"] % len(flag_pool)]
            spawn["n"] += 1
        async def run(self):
            await asyncio.sleep(0)
            return SolveOutcome(True, self._f, 1, None, "solved", flags=[self._f])

    monkeypatch.setattr(sw, "_make_cli_worker",
                        lambda engine, **kw: FakeWorker(engine))

    out = await sw.run()
    assert out.solved is True
    # the run collected BOTH distinct flags (not just the first worker's)
    assert set(sw._found_flags) == {"flag{a}", "flag{b}"}
    assert sw._flags_complete() is True


async def test_coordinator_singleflag_stops_on_first(challenge, tmp_path: Path, monkeypatch):
    """expected_flags=1 (default): the first flag completes the run immediately —
    byte-identical to the legacy 'first flag wins'."""
    from dswarm.solver.types import SolveOutcome

    sw = _coordinator_swarm(challenge, tmp_path, start_workers=2)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["pi"])

    class FakeWorker:
        def __init__(self, engine):
            self.solver_id = f"cli-{engine}"
        async def run(self):
            await asyncio.sleep(0)
            return SolveOutcome(True, "flag{x}", 1, None, "solved", flags=["flag{x}"])

    monkeypatch.setattr(sw, "_make_cli_worker",
                        lambda engine, **kw: FakeWorker(engine))
    out = await sw.run()
    assert out.solved is True and out.flag == "flag{x}"
    assert sw._found_flags == ["flag{x}"]  # stopped at one










async def test_operator_hint_queues_review_request(challenge, tmp_path):
    sw = _coordinator_swarm(
        challenge, tmp_path,
        stage_policy={"coordinator": {"review": {
            "enabled": True, "on_operator_hint": True,
        }}},
    )

    await _drain_one(sw, {
        "target": "global", "action": "hint",
        "text": "pivot through the SOCKS tunnel instead of repeated SSH hops",
    })

    assert sw._queued_review_requests
    assert sw._queued_review_requests[0]["trigger"] == "operator_hint"
    assert "SOCKS tunnel" in sw._queued_review_requests[0]["directive"]








async def _async_zero():
    return 0


# ── winner KILLS the loser's subprocess, not just its task (bug #2) ───────────




@pytest.mark.asyncio
async def test_reason_worker_runtime_worker_creation_does_not_block_event_loop(challenge, tmp_path, monkeypatch):
    """Reason worker construction can touch Docker; it must not block timeouts.

    Startup-test cancellation is driven by asyncio timeouts. If _make_cli_worker
    runs synchronously on the event loop, a slow Docker/supervisor startup prevents
    those timeouts and cleanup from running, which can leave a run container alive.
    """
    import threading
    import time

    from dswarm.swarm.agents import AgentProfile, DispatchDecision
    from dswarm.swarm.runtime import SwarmWorkerRuntime

    sw = _coordinator_swarm(challenge, tmp_path, engines=["pi-web"])
    release_creation = threading.Event()

    class NeverRunWorker:
        solver_id = "cli-pi-web-create"

        async def run(self):
            await asyncio.sleep(3600)

        def cancel(self):
            pass

    def slow_make_cli_worker(*args, **kwargs):
        release_creation.wait(timeout=0.4)
        return NeverRunWorker()

    monkeypatch.setattr(sw, "_make_cli_worker", slow_make_cli_worker)
    monkeypatch.setattr(sw, "_release_worker_account", lambda worker: None)

    runtime = SwarmWorkerRuntime(sw, healthy=["pi-web"])
    decision = DispatchDecision(
        intent_id="I-create", profile="pi-web", goal="do not block", mode="explore"
    )
    profile = AgentProfile(id="pi-web", worker_profile="pi-web", mode="explore")

    start = time.perf_counter()
    task = asyncio.create_task(runtime.run(decision, profile))
    await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - start

    release_creation.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_reason_worker_runtime_cancels_underlying_worker_on_task_cancel(challenge, tmp_path, monkeypatch):
    """Cancelling the ReasonSwarm worker task must signal the shelled CLI worker.

    A bare asyncio task cancellation unwinds the coroutine but does not stop the
    subprocess/thread that CliSolver.run owns; SwarmWorkerRuntime must call
    worker.cancel() before releasing the account slot.
    """
    from dswarm.swarm.agents import AgentProfile, DispatchDecision
    from dswarm.swarm.runtime import SwarmWorkerRuntime

    sw = _coordinator_swarm(challenge, tmp_path, engines=["pi-web"])
    events: list[str] = []

    class BlockingWorker:
        solver_id = "cli-pi-web-runtime-cancel"

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = False

        async def run(self):
            events.append("run.start")
            self.started.set()
            await asyncio.sleep(3600)

        def cancel(self):
            events.append("worker.cancel")
            self.cancelled = True

    worker = BlockingWorker()

    def fake_make_cli_worker(*args, **kwargs):
        return worker

    def fake_release_worker_account(w):
        events.append(f"release:{getattr(w, 'solver_id', '')}")

    monkeypatch.setattr(sw, "_make_cli_worker", fake_make_cli_worker)
    monkeypatch.setattr(sw, "_release_worker_account", fake_release_worker_account)

    runtime = SwarmWorkerRuntime(sw, healthy=["pi-web"])
    decision = DispatchDecision(
        intent_id="I-cancel", profile="pi-web", goal="cancel me", mode="explore"
    )
    profile = AgentProfile(id="pi-web", worker_profile="pi-web", mode="explore")

    task = asyncio.create_task(runtime.run(decision, profile))
    await asyncio.wait_for(worker.started.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.cancelled is True
    assert events == [
        "run.start",
        "worker.cancel",
        "release:cli-pi-web-runtime-cancel",
    ]


async def test_cancel_solver_is_noop_without_cancel_method():
    # a solver without a cancel() method — _cancel_solver must not raise.
    Swarm._cancel_solver(None)                       # None is safe
    Swarm._cancel_solver(object())                   # no cancel attr is safe

    class Boom:
        def cancel(self): raise RuntimeError("nope")
    Swarm._cancel_solver(Boom())                     # a throwing cancel is swallowed




def test_retry_goal_lists_dead_ends(challenge, tmp_path):
    """_retry_goal surfaces the board's ruled-out paths (so a re-bootstrap doesn't
    retry them) AND pushes the worker to DRIVE a lead to a working exploit — not the
    old 're-examine / try a different angle' wording that made retry workers conclude
    after a few probes (run-7349)."""
    sw = _coordinator_swarm(challenge, tmp_path)
    try:
        sw.shared_graph.add_dead_end(actor="cli-pi", reason="SQLi on /login is sanitized")
    except Exception:
        pass
    goal = sw._retry_goal()
    assert "HAS a solution" in goal
    # it must push depth-to-exploit, not shallow reconsideration
    assert "exploit" in goal.lower()
    assert "do not stop at recon" in goal.lower() or "do not conclude after a few" in goal.lower()
    # and it lists the ruled-out dead-end so the worker doesn't retry it
    assert "SQLi on /login" in goal


def test_make_cli_worker_assigns_unique_labels(challenge, tmp_path):
    """Each spawned worker gets a UNIQUE solver_id (so the deck draws one lane per
    worker), keeping the cli-<engine> prefix (so the engine badge still resolves).
    The first worker of an engine keeps the bare cli-<engine> for back-compat."""
    sw = _coordinator_swarm(challenge, tmp_path)
    a = sw._make_cli_worker("pi", mode="bootstrap")
    b = sw._make_cli_worker("pi", mode="explore")
    c = sw._make_cli_worker("pi", mode="bootstrap")
    d = sw._make_cli_worker("pi", mode="explore")
    assert a.solver_id == "cli-pi"      # 1st pi → bare prefix
    assert b.solver_id == "cli-pi-2"    # 2nd pi → distinct
    assert c.solver_id == "cli-pi-3"    # 3rd pi → distinct
    assert d.solver_id == "cli-pi-4"
    # all distinct → distinct lanes on the deck
    assert len({a.solver_id, b.solver_id, c.solver_id, d.solver_id}) == 4
    # prefix preserved so workerEngine() detects the engine from the id alone
    assert all("pi" in s for s in (a.solver_id, b.solver_id))
    assert all("pi" in s for s in (c.solver_id, d.solver_id))
    # swarm sub-workers are worker-scoped: their end is WORKER_FINISHED, not the run.
    assert all(w.lifecycle_scope == "worker" for w in (a, b, c, d))




async def test_m11_cancelled_coordinator_finalizes_and_closes_graph(
        challenge, tmp_path, monkeypatch):
    """M11: when the coordinator is CANCELLED mid-run, it must still finalize — close
    the shared_graph (release the SQLite WAL/-shm handles) and emit the run-level
    RUN_FINISHED — instead of leaking them (the cleanup used to sit after the finally,
    on the normal-return path only)."""
    from dswarm.core.event_bus import EventBus
    from dswarm.core.events import Event, EventType

    captured = []
    bus = EventBus()
    async def _sink(ev):
        captured.append(ev)
    bus.add_sink(_sink)

    sw = _coordinator_swarm(challenge, tmp_path, start_workers=1, bus=bus)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["claude"])

    closed = {"n": 0}
    if sw.shared_graph is not None:
        orig_close = sw.shared_graph.close
        def _spy_close():
            closed["n"] += 1
            return orig_close()
        monkeypatch.setattr(sw.shared_graph, "close", _spy_close)

    class HangWorker:
        def __init__(self, engine):
            self.solver_id = f"cli-{engine}"
        async def run(self):
            await asyncio.sleep(3600)  # hang until cancelled

    monkeypatch.setattr(sw, "_make_cli_worker",
                        lambda engine, *, mode, intent_goal="", intent_id="": HangWorker(engine))

    task = asyncio.create_task(sw.run())
    await asyncio.sleep(0.1)        # let it spawn + start hanging
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert sw._run_finalized is True, "a cancelled coordinator must finalize"
    if sw.shared_graph is not None:
        assert closed["n"] >= 1, "shared_graph must be closed on a cancelled run"
    run_finished = [e for e in captured if e.event_type is EventType.RUN_FINISHED]
    assert len(run_finished) == 1, "a cancelled run still emits exactly one RUN_FINISHED"
    # L3: the coordinator's bus sinks are detached on finalize (no leak on a reused bus)
    assert sw._coord_sinks == [], "coordinator bus sinks must be detached on finalize"


async def test_finalize_merges_shared_graph_flags_before_finished(tmp_path):
    """Coordinator finalization is the last line of defense: if flags were already
    persisted in the shared graph, RUN_FINISHED must carry them even when there is
    no winner.json-producing worker outcome in hand."""
    from dswarm.core.event_bus import EventBus
    from dswarm.core.events import EventType

    ch = Challenge(id="c-multi", name="multi", category="web", points=0,
                   description="collect two", flag_format=r"flag\{[^}]+\}",
                   expected_flags=2, multi_flag=True)
    events = []
    bus = EventBus()

    async def _sink(ev):
        events.append(ev)
    bus.add_sink(_sink)

    sw = _coordinator_swarm(ch, tmp_path, bus=bus)
    assert sw.shared_graph is not None
    sw.shared_graph.flag_found(actor="cli-a", flag="flag{one}")
    sw.shared_graph.flag_found(actor="cli-b", flag="flag{two}")

    await sw._finalize_coordinator_run(
        winner=None, flag=None, goal_complete=False, per_solver={})

    fin = [e for e in events if e.event_type is EventType.RUN_FINISHED][-1]
    assert fin.payload["solved"] is True
    assert fin.payload["flag"] == "flag{one}"
    assert fin.payload["flags"] == ["flag{one}", "flag{two}"]


# ── lease + OODA refactor: stall-kill removed, lease closes the loop ──────────

async def test_coordinator_does_not_steer_kill_on_global_fact_stall(
        challenge, tmp_path: Path, monkeypatch):
    """The run-7352 fix: a worker that emits no GLOBAL verified fact must NOT be
    steer-killed. The old design called request_steer() after stall_seconds of no
    global fact, which murdered freshly-spawned workers mid-exploit. There is no
    stall-kill anymore — a worker runs until it finishes on its own."""
    from dswarm.solver.types import SolveOutcome

    # stall_seconds tiny: under the OLD code this would steer-kill the worker fast.
    sw = _coordinator_swarm(challenge, tmp_path, start_workers=1, stall_seconds=0.01,
                            wall_clock_budget=2.0)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["claude"])
    monkeypatch.setattr(sw, "_run_reason", lambda: _async_zero())

    steered = {"n": 0}

    class SlowNoFactWorker:
        solver_id = "cli-pi"
        def request_steer(self):           # coordinator must NEVER call this for reclaim
            steered["n"] += 1
        async def run(self):
            await asyncio.sleep(0.5)        # works a while, emits no fact
            return SolveOutcome(False, None, 1, None, "explored, no flag")

    monkeypatch.setattr(sw, "_make_cli_worker", lambda *a, **k: SlowNoFactWorker())
    await asyncio.wait_for(sw.run(), timeout=5)
    assert steered["n"] == 0, "coordinator must not steer-kill a worker on global fact stall"


# ── MIGRATION RED LINE (single-shot migration, DESIGN_single_shot_migration.md) ──
# This is the load-bearing invariant for reverting to the single-shot model.
# The run-7352 death spiral happened because a GLOBAL signal (global fact stall)
# decided a SINGLE worker's fate. The migration's red line: a worker's life is
# governed ONLY by its own clock (its own timeout / its own lease) — NEVER by a
# global signal (global fact count, global stall time, another worker's progress).
# If a future change re-introduces a global-progress reclaim path, this fails.
async def test_REDLINE_no_global_signal_kills_a_progressless_worker(
        challenge, tmp_path: Path, monkeypatch):
    """A worker that produces NO global fact for a long time, while the run is
    otherwise idle (no other progress anywhere), must run to its OWN natural
    completion — the coordinator must neither steer it nor cancel its task. This
    guards the single-shot migration against re-growing the run-7352 stall-kill leg
    in any form (steer OR cancel)."""
    from dswarm.solver.types import SolveOutcome

    # tiny stall_seconds: the OLD stall-kill would have fired almost immediately.
    sw = _coordinator_swarm(challenge, tmp_path, start_workers=1, stall_seconds=0.01,
                            wall_clock_budget=2.0)
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["claude"])
    monkeypatch.setattr(sw, "_run_reason", lambda: _async_zero())

    events = {"steered": 0, "cancelled": 0, "ran_to_completion": False}

    class SlowNoFactWorker:
        solver_id = "cli-pi"
        def request_steer(self):       # global logic must NEVER steer for reclaim
            events["steered"] += 1
        def cancel(self):              # global logic must NEVER cancel for reclaim
            events["cancelled"] += 1
        async def run(self):
            try:
                # emits no global fact for a meaningful stretch (deep-exploit setup)
                await asyncio.sleep(0.6)
            except asyncio.CancelledError:
                # if the coordinator cancelled the task on a global signal, that's
                # the death-spiral leg — record it and re-raise.
                events["cancelled"] += 1
                raise
            events["ran_to_completion"] = True
            return SolveOutcome(False, None, 1, None, "explored, no flag")

    monkeypatch.setattr(sw, "_make_cli_worker", lambda *a, **k: SlowNoFactWorker())
    await asyncio.wait_for(sw.run(), timeout=5)

    assert events["steered"] == 0, \
        "RED LINE: a global signal must not steer a progressless worker (run-7352)"
    assert events["cancelled"] == 0, \
        "RED LINE: a global signal must not cancel a progressless worker (run-7352)"
    assert events["ran_to_completion"], \
        "worker must reach its own natural completion, governed by its own clock"


def test_make_cli_worker_explore_gets_short_timeout(challenge, tmp_path):
    """Dual to the above: the ONLY backstop that frees a slot held by a stuck explore
    is its SHORT per-turn timeout. explore must get explore_timeout; bootstrap keeps
    the long default (whole-challenge rush)."""
    sw = _coordinator_swarm(challenge, tmp_path, explore_timeout=720)
    boot = sw._make_cli_worker("pi", mode="bootstrap")
    expl = sw._make_cli_worker("pi", mode="explore",
                               intent_goal="probe", intent_id="I1-abc")
    assert expl.timeout == 720, "explore worker must get the short explore_timeout"
    assert boot.timeout == 2400, "bootstrap worker keeps the long default timeout"
    assert expl.timeout < boot.timeout


def test_generic_bootstrap_prefers_open_reason_intent(challenge, tmp_path):
    """D (run-3154 intent starvation): a generic bootstrap spawn with no intent
    must convert to a focused explore for the oldest compatible open reason
    intent, claim it under its own solver_id, and adopt the intent's goal —
    so focused intents are never starved by whole-challenge-rush churn."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I3-abc", goal="scan uploaded images for flag",
        payload={"worker_class": "shell_agent"})
    w = sw._make_cli_worker("pi", mode="bootstrap")
    assert w.mode == "explore", "generic spawn must pivot to the open intent"
    assert w.intent_id_assigned == "I3-abc"
    assert w.intent_goal == "scan uploaded images for flag"
    # claimed atomically under this worker → its conclusion is owner-accepted.
    row = sw.shared_graph._conn.execute(
        "SELECT worker, status FROM intents WHERE intent_id='I3-abc'").fetchone()
    assert row[0] == w.solver_id
    assert row[1] == "claimed"
    # the intent is now owned — a second generic spawn pivots to nothing (no other
    # open intent) and stays a generic whole-challenge rush.
    w2 = sw._make_cli_worker("pi", mode="bootstrap")
    assert w2.mode == "bootstrap"


def test_generic_bootstrap_skips_incompatible_direction_intent(challenge, tmp_path):
    """An open intent whose direction needs a different worker profile must NOT be
    hijacked by a generic spawn of the wrong engine."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-rev", goal="unpack the ELF",
        payload={"worker_class": "shell_agent", "direction": "rev"})
    w = sw._make_cli_worker("pi", mode="bootstrap")
    assert w.mode == "bootstrap", "rev intent must not be claimed by a plain pi worker"
    assert w.intent_id_assigned == ""


def test_generic_bootstrap_claim_lost_falls_back_rejected(challenge, tmp_path, monkeypatch):
    """If the atomic claim for the pre-empted open intent is lost (a concurrent
    spawn won the race), the converted spawn must NOT duplicate the work — it
    raises WorkerSpawnRejected (slot freed) instead of running a redundant
    explore."""
    from dswarm.swarm.swarm import WorkerSpawnRejected

    sw = _coordinator_swarm(challenge, tmp_path)
    monkeypatch.setattr(
        sw, "_open_intents",
        lambda: [{"intent_id": "I-taken", "goal": "probe admin",
                  "worker_class": "shell_agent", "direction": ""}])
    monkeypatch.setattr(sw.shared_graph, "claim_intent", lambda **kw: False)
    with pytest.raises(WorkerSpawnRejected):
        sw._make_cli_worker("pi", mode="bootstrap")


def test_open_intents_includes_expired_lease(challenge, tmp_path):
    """lease closure: an intent whose claim LEASE EXPIRED (worker died holding it)
    must be re-offered by _open_intents, else a stuck worker orphans its intent
    forever. A still-live claim must NOT be re-offered."""
    import time
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-open", goal="never claimed")
    g.propose_intent(actor="reason", intent_id="I-live", goal="claimed, live lease")
    g.propose_intent(actor="reason", intent_id="I-dead", goal="claimed, expired lease")
    # live claim (long lease) — must stay hidden
    assert g.claim_intent(worker="w1", intent_id="I-live", lease_s=1000.0)
    # expired claim: claim with a lease already in the past
    assert g.claim_intent(worker="w2", intent_id="I-dead", lease_s=-1.0)

    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-open" in open_ids          # never claimed → available
    assert "I-dead" in open_ids          # lease expired → re-offered (the fix)
    assert "I-live" not in open_ids      # live claim → not re-offered


def test_conclude_intent_lease_fencing(challenge, tmp_path):
    """owner/lease fencing: a LATE worker concluding an intent whose lease already
    lapsed (and which a new worker may now own) must NOT clobber the fresh claim.
    The conclusion event is still recorded, but the table state is not flipped."""
    import time
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I1-x", goal="g")
    # worker A claims with an already-expired lease (simulates a hung/slow worker)
    g.claim_intent(worker="A", intent_id="I1-x", lease_s=-1.0)
    # worker B re-claims it (lease expired → allowed) with a live lease
    assert g.claim_intent(worker="B", intent_id="I1-x", lease_s=1000.0)
    # late worker A tries to conclude as dead_end — must be FENCED (B owns it now)
    g.conclude_intent(actor="A", intent_id="I1-x", result="dead_end")
    with g._lock:
        row = g._conn.execute(
            "SELECT status, worker FROM intents WHERE intent_id='I1-x'").fetchone()
    assert row[0] == "claimed", "late conclude must not flip a re-claimed intent to done"
    assert row[1] == "B", "the fresh owner must remain B"


def test_conclude_intent_solved_always_wins(challenge, tmp_path):
    """The fence exempts a SOLVED conclusion: a real flag ends the run regardless of
    lease state, so it must always flip the intent to done."""
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I2-x", goal="g")
    g.claim_intent(worker="A", intent_id="I2-x", lease_s=-1.0)  # expired
    g.conclude_intent(actor="A", intent_id="I2-x", result="solved")
    with g._lock:
        row = g._conn.execute(
            "SELECT status FROM intents WHERE intent_id='I2-x'").fetchone()
    assert row[0] == "done", "a solved conclusion must always win, even on an expired lease"


def test_supersede_open_intents_retires_obsolete_asks(challenge, tmp_path):
    """run-11190 convergence fix #1: once the operator supplies a resource, the open
    'ask the operator for X' intents are obsolete and must be retired so fresh
    workers stop re-claiming them. supersede matches by goal substring and only
    touches open / expired-lease rows — a LIVE claim is left alone, and unrelated
    intents are untouched."""
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-ask1",
                     goal="Request the operator for the L2 SSH password")
    g.propose_intent(actor="reason", intent_id="I-ask2",
                     goal="Submit L1 flag on the BreachLab dashboard to unlock L2")
    g.propose_intent(actor="reason", intent_id="I-solve",
                     goal="Recover scattered secrets via GitHub dorking")
    g.propose_intent(actor="reason", intent_id="I-live",
                     goal="ask the operator something — but actively worked")
    g.claim_intent(worker="w-live", intent_id="I-live", lease_s=1000.0)  # live

    killed = g.supersede_open_intents(actor="coordinator", match="operator",
                                      reason="operator supplied L2 password")
    assert "I-ask1" in killed
    assert "I-live" not in killed, "a live claim must NOT be superseded"
    # the matched open intent is now done → no longer re-offered
    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-ask1" not in open_ids
    assert "I-solve" in open_ids, "an unrelated solve intent must remain open"
    assert "I-live" not in open_ids  # live claim was already hidden

    # a second needle ('dashboard') retires the other obsolete ask
    killed2 = g.supersede_open_intents(actor="coordinator", match="dashboard")
    assert "I-ask2" in killed2
    assert "I-ask2" not in {i["intent_id"] for i in sw._open_intents()}


def test_reopen_false_positive_does_not_revive_superseded_intent(challenge, tmp_path):
    """#11 regression: marking a flag a false positive must reopen the SOLVED intents
    only — NOT the 'ask the operator for X' intents that were SUPERSEDED when the
    operator supplied the resource. The old reopen flipped EVERY status='done' row back
    to open, resurrecting the retired asks (run-11190 238-worker 'request the password'
    loop came back on a mark-false)."""
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    # a real solve
    fs = g.add_evidence(actor="cli-pi", source="pi", fact="real", verified=True)
    g.propose_intent(actor="reason", intent_id="I-solve", goal="exploit /login")
    g.conclude_intent(actor="cli-pi", intent_id="I-solve",
                      result="solved", to_fact_seq=fs)
    # an ask-operator intent that the operator obsoleted → superseded (status='done')
    g.propose_intent(actor="reason", intent_id="I-ask",
                     goal="Request the operator for the L2 SSH password")
    superseded = g.supersede_open_intents(actor="coordinator", match="operator")
    assert "I-ask" in superseded
    # a barren explored intent (also 'done', result not solved)
    g.propose_intent(actor="reason", intent_id="I-barren", goal="brute /admin")
    g.conclude_intent(actor="cli-pi", intent_id="I-barren", result="explored")

    info = g.reopen_after_false_positive(actor="operator", flag="flag{fake}")
    assert info["reopened"] == ["I-solve"], \
        "only the solved intent reopens; superseded/barren stay retired"
    # confirm the superseded ask did NOT come back as open
    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-ask" not in open_ids
    assert "I-barren" not in open_ids
    assert "I-solve" in open_ids


async def test_drain_hitl_does_not_supersede_submit_intent(challenge, tmp_path):
    """#12 regression: an unrelated operator hint must NOT retire a legitimate
    in-flight 'submit candidate flag to verifier' intent. The bare needle "submit"
    was removed from the supersede list — on a rate-limited chained-flag challenge
    (Specter), an irrelevant hint used to kill the active submission intent and stall
    the chain."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-submit",
                     goal="Submit candidate flag to the verifier endpoint")
    g.propose_intent(actor="reason", intent_id="I-ask",
                     goal="Request the operator for the dashboard token")

    # an unrelated operator hint flows through _drain_hitl → triggers supersede
    await _drain_one(sw, {"action": "hint", "text": "try port 8080 next"})

    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-submit" in open_ids, \
        "a 'submit candidate flag' intent must survive an unrelated operator hint"
    # the genuine ask-operator intent is still correctly retired by 'dashboard'
    assert "I-ask" not in open_ids


async def test_drain_hitl_hint_records_operator_directive(challenge, tmp_path):
    """B: an operator hint is now a FIRST-CLASS OperatorDirective (not a fake
    low-confidence candidate fact). It still binds a claimable directive-tagged
    intent the next worker batch picks up — but it is NOT injected as evidence."""
    sw = _coordinator_swarm(challenge, tmp_path)
    hint = "try /robots.txt and then /data/note.txt"

    await _drain_one(sw, {"target": "global", "action": "hint", "text": hint})

    assert hint in sw._next_worker_guidance

    # the directive is recorded on the operator_directives table, active + bound
    directives = sw.shared_graph.operator_directives()
    assert any(d["text"] == hint and d["action"] == "hint" for d in directives)
    bound = [d for d in directives if d["text"] == hint][0]
    assert bound["status"] == "bound"
    assert hint in sw.shared_graph.active_operator_directive_texts()

    # it must NOT have become a fake candidate fact (design §12)
    fact_events = [
        e for e in sw.shared_graph.events()
        if e.get("kind") == "fact_added"
        and (e.get("payload") or {}).get("fact") == f"Operator hint: {hint}"
    ]
    assert not fact_events, "operator hint must NOT be a candidate fact anymore"

    # it binds a claimable directive-tagged intent
    assert any(goal == hint for goal in sw.shared_graph.open_goal_texts())


async def test_drain_hitl_directive_classification_recorded(challenge, tmp_path):
    """F: a worker hand-raise (external_blocker) is persisted as a classified
    hitl_request so the deck can distinguish it from an auto-resolving kind."""
    from dswarm.core.event_bus import EventBus
    from dswarm.core.events import Event, EventType, hitl_request_payload

    bus = EventBus()
    sw = _coordinator_swarm(challenge, tmp_path, bus=bus)

    async def _help_sink(ev):
        if ev.event_type is EventType.HITL_REQUEST:
            payload = dict(ev.payload or {})
            need_kind = str(payload.get("need_kind") or "external_blocker")
            need_text = str(payload.get("need", "")).strip()
            worker = str(payload.get("worker", ""))
            if need_text:
                sw.shared_graph.add_hitl_request(
                    worker=worker or "worker", need=need_text, need_kind=need_kind,
                    status=("awaiting_operator" if need_kind == "external_blocker"
                            else "auto_resolved"))

    bus.add_sink(_help_sink)
    await bus.emit(Event(
        event_type=EventType.HITL_REQUEST, run_id=sw.run_id,
        challenge_id=challenge.id, solver_id="cli-pi",
        payload=hitl_request_payload("cli-pi", "need a VPS with 4444 open",
                                     kind="need_input")))
    # the classification persisted with awaiting_operator status
    rows = sw.shared_graph.events()
    classified = [e for e in rows if e.get("kind") == "hitl_classified"]
    assert classified, "hand-raise must be persisted as a classified hitl_request"
    assert classified[0]["payload"]["need_kind"] == "external_blocker"


def test_coordinator_rechecks_external_blocker_before_pausing(challenge, tmp_path):
    sw = _coordinator_swarm(challenge, tmp_path)

    assert sw._rechecked_need_kind(
        "I am unsure whether to try JWT first or upload first",
        "external_blocker",
    ) == "worker_uncertainty"
    assert sw._rechecked_need_kind(
        "target returns connection refused and the instance may be expired",
        "external_blocker",
    ) == "external_blocker"


async def test_drain_hitl_mark_false_invalidates_only_target_flag_live(challenge, tmp_path):
    """A live run receiving mark_false must immediately remove only the selected
    flag and reopen only the intent linked to that flag."""
    from dswarm.core.event_bus import EventBus
    from dswarm.core.events import EventType

    bus = EventBus()
    events = []
    bus.add_sink(lambda ev: events.append(ev) or _noop())
    sw = _coordinator_swarm(challenge, tmp_path, bus=bus)
    sw._found_flags = ["flag{a}", "flag{b}", "flag{c}"]
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-a", goal="get flag a")
    g.propose_intent(actor="reason", intent_id="I-b", goal="get flag b")
    g.conclude_intent(actor="cli-a", intent_id="I-a", result="solved")
    g.conclude_intent(actor="cli-b", intent_id="I-b", result="solved")
    g.flag_found(actor="cli-a", flag="flag{a}", intent_id="I-a")
    g.flag_found(actor="cli-b", flag="flag{b}", intent_id="I-b")

    await _drain_one(sw, {"target": "global", "action": "mark_false",
                          "flag": "flag{b}", "text": "flag{b}"})

    assert sw._found_flags == ["flag{a}", "flag{c}"]
    assert g.snapshot().flags == ["flag{a}"]
    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-b" in open_ids
    assert "I-a" not in open_ids
    assert any(
        e.event_type is EventType.BLACKBOARD_DELTA
        and (e.payload or {}).get("kind") == "flag_invalidated"
        and (e.payload or {}).get("flag") == "flag{b}"
        for e in events
    )


# ── 缺陷4: standing guidance LRU + clear (single-shot migration follow-up) ────
async def _drain_one(sw, cmd):
    """Push one HITL cmd, run the drain briefly, stop it."""
    sw.hitl_inbox = sw.hitl_inbox or asyncio.Queue()
    await sw.hitl_inbox.put(cmd)
    t = asyncio.create_task(sw._drain_hitl())
    await asyncio.sleep(0.03)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass


async def test_m3_bare_hint_without_resource_does_not_supersede(challenge, tmp_path):
    """M3: a contentless operator command (no text/url/standing) must NOT run the
    ask-operator supersede sweep — its broad needles (operator/unlock/dashboard)
    could wrongly retire a legitimate in-flight intent on an unrelated hint."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-ask",
                     goal="Request the operator for the dashboard token")
    # a resume action carries no resource → supersede must NOT fire
    await _drain_one(sw, {"action": "resume", "text": ""})
    open_ids = {i["intent_id"] for i in sw._open_intents()}
    assert "I-ask" in open_ids, "a contentless command must not retire ask-operator intents"


async def test_m5_solver_scoped_command_only_clears_that_workers_help(challenge, tmp_path):
    """M5: a hint scoped solver:<id> must only clear THAT worker's pending help — a
    hint addressed to worker B must not wipe worker A's still-unmet blocker."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    sw._pending_help = [
        {"worker": "cli-pi-1", "need": "need a VPS"},
        {"worker": "cli-pi-2", "need": "need the dashboard token"},
    ]
    await _drain_one(sw, {"action": "hint", "text": "use http on 8080",
                          "target": "solver:cli-pi-2"})
    workers = {h["worker"] for h in sw._pending_help}
    assert "cli-pi-1" in workers, "worker A's unmet blocker must survive a B-scoped hint"
    assert "cli-pi-2" not in workers, "the targeted worker's ask is cleared"


async def test_dismiss_clears_help_unfreezes_and_deadends(challenge, tmp_path):
    """Operator DISMISSES a hand-raise without supplying the resource: the pending
    ask is cleared, the swarm unpauses (operator_event set, _operator_paused False),
    and a dead-end is recorded so a re-spawned worker doesn't immediately re-raise."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    sw._operator_event = asyncio.Event()
    sw._operator_paused = True
    sw._pending_help = [
        {"worker": "cli-pi-1", "need": "need a public VPS for reverse shell"},
        {"worker": "cli-pi-2", "need": "target seems expired"},
    ]
    deadends = []
    if sw.insight is not None:
        orig = sw.insight.dead_end
        async def _spy(by, reason): deadends.append(reason); await orig(by, reason)
        sw.insight.dead_end = _spy  # type: ignore[assignment]

    await _drain_one(sw, {"action": "dismiss", "target": "global"})

    assert sw._pending_help == [], "global dismiss clears all pending help"
    assert sw._operator_paused is False, "dismiss must unfreeze the swarm"
    assert sw._operator_event.is_set(), "dismiss must wake the coordinator"
    if sw.insight is not None:
        assert any("dismissed" in d for d in deadends), "a dead-end is recorded so it won't re-raise"


async def test_dismiss_scoped_only_clears_that_worker(challenge, tmp_path):
    """A solver-scoped dismiss clears only that worker's ask, leaving others pending."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    sw._operator_event = asyncio.Event()
    sw._pending_help = [
        {"worker": "cli-pi-1", "need": "need a VPS"},
        {"worker": "cli-pi-2", "need": "need a token"},
    ]
    await _drain_one(sw, {"action": "dismiss", "target": "solver:cli-pi-1"})
    workers = {h["worker"] for h in sw._pending_help}
    assert workers == {"cli-pi-2"}, "only the scoped worker's ask is dismissed"


def test_m6_help_sink_dedups_same_blocker_and_caps(challenge, tmp_path):
    """M6: the same blocker raised by many workers is deduped on (worker, need), and
    the pending list is bounded so a never-give-up run can't grow it unbounded."""
    from dswarm.swarm.swarm import _PENDING_HELP_MAX
    from dswarm.core.events import EventType, hitl_request_payload
    from dswarm.core.event_bus import Event

    async def _run():
        sw = _coordinator_swarm(challenge, tmp_path)
        # build the help sink exactly as _run_coordinator does
        async def _help_sink(ev):
            if ev.event_type is EventType.HITL_REQUEST:
                payload = dict(ev.payload or {})
                key = (str(payload.get("worker", "")), str(payload.get("need", "")).strip())
                for h in sw._pending_help:
                    if (str(h.get("worker", "")), str(h.get("need", "")).strip()) == key:
                        break
                else:
                    sw._pending_help.append(payload)
                    if len(sw._pending_help) > _PENDING_HELP_MAX:
                        del sw._pending_help[: len(sw._pending_help) - _PENDING_HELP_MAX]
        # same blocker from 5 distinct workers but identical (worker,need) repeats
        for _ in range(5):
            await _help_sink(Event(event_type=EventType.HITL_REQUEST, run_id="r",
                payload=hitl_request_payload("cli-pi-1", "need a VPS")))
        assert len(sw._pending_help) == 1, "an identical (worker,need) ask dedups to one"
        # many DISTINCT blockers → capped
        for i in range(_PENDING_HELP_MAX + 10):
            await _help_sink(Event(event_type=EventType.HITL_REQUEST, run_id="r",
                payload=hitl_request_payload(f"w{i}", f"blocker {i}")))
        assert len(sw._pending_help) <= _PENDING_HELP_MAX, "pending help must be bounded"

    asyncio.run(_run())


def test_m1_insight_bus_dedups_repeated_guidance():
    """M1: an operator hint storm (identical guidance re-sent N times) must not flood
    the bounded history — each copy would evict a real VERIFIED_FACT / DEAD_END and
    replay to every cold subscriber. An identical trailing guidance is dropped; a
    fact in between, or a changed hint, still publishes."""
    from dswarm.swarm.insight_bus import InsightBus, InsightKind

    async def _run():
        bus = InsightBus("run-kp")
        for _ in range(11):
            await bus.guidance("try /admin", action="hint", target="global")
        guidance = [i for i in bus.history if i.kind is InsightKind.GUIDANCE]
        assert len(guidance) == 1, "11 identical hints must collapse to one in history"
        # a different hint still publishes
        await bus.guidance("try /api instead", action="hint", target="global")
        assert len([i for i in bus.history if i.kind is InsightKind.GUIDANCE]) == 2
        # a hint repeated AFTER other activity re-broadcasts (not the most-recent one)
        await bus.fact("cli-1", "creds are admin/admin", "a1")
        await bus.guidance("try /admin", action="hint", target="global")
        assert len([i for i in bus.history if i.kind is InsightKind.GUIDANCE]) == 3

    asyncio.run(_run())


async def test_defect4_standing_lru_caps_count(challenge, tmp_path):
    """defect-4: standing guidance is LRU-capped so the cumulative text can't bloat
    every new worker's prompt unbounded (the 36k-token claude empty-exit)."""
    from dswarm.swarm.swarm import _STANDING_MAX
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    for i in range(_STANDING_MAX + 5):
        await _drain_one(sw, {"action": "hint", "text": f"hint-{i}", "standing": True})
    assert len(sw._standing_guidance) == _STANDING_MAX           # capped
    assert sw._standing_guidance[-1] == f"hint-{_STANDING_MAX + 4}"  # most-recent kept
    assert "hint-0" not in sw._standing_guidance                  # oldest evicted


async def test_defect4_clear_standing(challenge, tmp_path):
    """defect-4: clear_standing wipes all (or one by exact text) so an operator can
    retract a stale correction — the list was only-grew before."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()
    await _drain_one(sw, {"action": "hint", "text": "a", "standing": True})
    await _drain_one(sw, {"action": "hint", "text": "b", "standing": True})
    assert sw._standing_guidance == ["a", "b"]
    # clear one by text
    await _drain_one(sw, {"action": "clear_standing", "text": "a"})
    assert sw._standing_guidance == ["b"]
    # clear all
    await _drain_one(sw, {"action": "clear_standing"})
    assert sw._standing_guidance == []


def test_defect4_standing_block_char_budget(challenge, tmp_path):
    """defect-4: even within the count cap, the per-worker injected block is bounded
    by a char budget (most-recent hints win)."""
    from dswarm.solver.cli_solver import CliSolver
    s = CliSolver(None, challenge, driver=None, engine="pi", kb=False)
    s._standing_guidance = ["x" * 3000, "y" * 3000, "z-newest"]  # 6KB+ > 4KB budget
    block = s._standing_block()
    assert "z-newest" in block                       # newest always kept
    assert len(block) < s._STANDING_CHAR_BUDGET + 200  # bounded (+ header slack)
    assert block.count("x" * 3000) == 0              # oldest dropped over budget


# ── M-3: intent-level HITL (single-shot migration) ───────────────────────────
async def test_m3_redirect_reaches_next_spawned_worker(challenge, tmp_path, monkeypatch):
    """Single-shot migration M-3: a non-standing redirect/hint can't steer a live
    worker anymore, so it must reach the NEXT spawned worker — the redirect url as
    the new target override, the text as one-shot guidance — and the guidance is
    consumed (one-shot), not re-applied to every future worker."""
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.hitl_inbox = asyncio.Queue()

    # operator drops a non-standing redirect.
    await sw.hitl_inbox.put({"action": "redirect", "text": "decode the cookie, not brute force",
                             "url": "http://new-target:9000", "target": "global"})
    drain = asyncio.create_task(sw._drain_hitl())
    await asyncio.sleep(0.05)            # let the drain consume it
    drain.cancel()
    try:
        await drain
    except asyncio.CancelledError:
        pass

    assert sw._target_redirect == "http://new-target:9000"
    assert "decode the cookie, not brute force" in sw._next_worker_guidance

    # the next spawned worker gets BOTH: the redirect url as its target override and
    # the text folded into its guidance.
    w = sw._make_cli_worker("pi", mode="bootstrap")
    assert w._target_override == "http://new-target:9000"
    assert "decode the cookie, not brute force" in w._standing_guidance
    # one-shot: the coordinator's pending guidance is consumed after the spawn.
    assert sw._next_worker_guidance == []


# ── race-scout layer (DESIGN_race_scout_layer.md) ────────────────────────────










def test_stage_policy_reads_coordinator_key():
    """The coordinator stage policy is keyed "coordinator"; from_config reads it and
    round-trips it through model_dump. Unknown keys are ignored."""
    from dswarm.swarm.stage_policy import StagePolicy
    sp = StagePolicy.from_config({"coordinator": {"wall_clock_budget": 7},
                                  "unknown_key": {"wall_clock_budget": 42}})
    assert sp.coordinator == {"wall_clock_budget": 7}
    assert sp.model_dump()["coordinator"] == {"wall_clock_budget": 7}














def test_nofact_deadend_conclude_is_not_redispatched(challenge, tmp_path):
    """run-11190 convergence fix #2 (DB contract the caller fix relies on): a no-fact
    dead_end conclude (to_fact_seq=None) must flip the intent to 'done' so its expired
    lease does NOT resurrect the same stale direction. The caller bug was in
    cli_solver._run_explore, which SKIPPED this conclude when `lfs is None` (no fact
    recorded) — leaving the row 'claimed' → lease expiry re-offered it forever
    (238-worker loop). The fix drops that gate; this asserts the DB side does the
    right thing for a None fact pointer so the unconditional conclude is safe."""
    sw = _coordinator_swarm(challenge, tmp_path)
    g = sw.shared_graph
    g.propose_intent(actor="reason", intent_id="I-nf", goal="a doomed direction")
    # worker claims with an already-expired lease (so without the fix the row would
    # be re-offered), then concludes dead_end with NO fact pointer.
    g.claim_intent(worker="wx", intent_id="I-nf", lease_s=-1.0)
    g.conclude_intent(actor="wx", intent_id="I-nf", result="dead_end",
                      to_fact_seq=None)  # the no-fact path
    with g._lock:
        row = g._conn.execute(
            "SELECT status FROM intents WHERE intent_id='I-nf'").fetchone()
    assert row[0] == "done", "a no-fact dead_end conclude must still flip status to done"
    assert "I-nf" not in {i["intent_id"] for i in sw._open_intents()}, \
        "a concluded no-fact dead_end must NOT be re-dispatched on lease expiry"


# ── standing guidance reaches workers spawned AFTER the operator gave it ──────

def test_standing_guidance_injected_into_new_worker_turn1(challenge, tmp_path):
    """The VPS/SSH-hint bug: a worker spawned AFTER the operator's standing hint
    must carry it in its turn-1 prompt. Before, the coordinator didn't persist
    standing, so _make_cli_worker built workers with an empty _standing_block and
    late workers never saw the VPS info."""
    sw = _coordinator_swarm(challenge, tmp_path)
    # operator gave standing guidance earlier in the run (coordinator persisted it)
    sw._standing_guidance.append("reverse shell: ssh root@38.247.145.244 (VPS relay)")
    # a worker spawned NOW must inherit it
    w = sw._make_cli_worker("pi", mode="explore",
                            intent_goal="probe", intent_id="I1-x")
    block = w._standing_block()
    assert "38.247.145.244" in block, "new worker's turn-1 standing block must carry the VPS hint"
    prompt = w._build_explore_prompt()
    assert "38.247.145.244" in prompt, "the VPS hint must be in the worker's actual turn-1 prompt"


def test_drain_hitl_persists_standing_on_coordinator(challenge, tmp_path):
    """_drain_hitl must store a standing hint on the coordinator's canonical list
    (so future _make_cli_worker calls inherit it), not just broadcast it live."""
    import asyncio as _aio
    sw = _coordinator_swarm(challenge, tmp_path)
    inbox: _aio.Queue = _aio.Queue()
    sw.hitl_inbox = inbox

    async def drive():
        drain = _aio.create_task(sw._drain_hitl())
        await inbox.put({"target": "global", "action": "hint",
                         "text": "ssh root@1.2.3.4 for reverse shell", "standing": True})
        await _aio.sleep(0.05)
        drain.cancel()
        try:
            await drain
        except _aio.CancelledError:
            pass

    _aio.run(drive())
    assert any("1.2.3.4" in s for s in sw._standing_guidance), \
        "standing hint must be persisted on the coordinator for late-spawned workers"


def test_drain_hitl_stop_sets_operator_stop_and_wakes(challenge, tmp_path):
    """An operator `stop` (or `complete`) command flips _operator_stop and wakes the
    coordinator — the graceful-terminate lever for a run that never gates a flag
    (run-10070). It is distinct from a steer, which only guides workers."""
    import asyncio as _aio
    sw = _coordinator_swarm(challenge, tmp_path)
    inbox: _aio.Queue = _aio.Queue()
    sw.hitl_inbox = inbox
    sw._operator_event = _aio.Event()

    async def drive():
        drain = _aio.create_task(sw._drain_hitl())
        await inbox.put({"target": "global", "action": "stop"})
        await _aio.sleep(0.05)
        drain.cancel()
        try:
            await drain
        except _aio.CancelledError:
            pass

    _aio.run(drive())
    assert sw._operator_stop is True
    assert sw._operator_event.is_set()


def test_drain_hitl_steer_does_not_stop(challenge, tmp_path):
    """A normal steer/hint must NOT set _operator_stop — only stop/complete do."""
    import asyncio as _aio
    sw = _coordinator_swarm(challenge, tmp_path)
    inbox: _aio.Queue = _aio.Queue()
    sw.hitl_inbox = inbox
    sw._operator_event = _aio.Event()

    async def drive():
        drain = _aio.create_task(sw._drain_hitl())
        await inbox.put({"target": "global", "action": "hint", "text": "try /admin"})
        await _aio.sleep(0.05)
        drain.cancel()
        try:
            await drain
        except _aio.CancelledError:
            pass

    _aio.run(drive())
    assert sw._operator_stop is False


# ── worker raises its hand → coordinator pauses for the operator (not re-spawn) ──













# ── code-review fixes (origin/main..HEAD review) ─────────────────────────────

def test_missing_profile_does_not_leak_budget(challenge, tmp_path):
    """#3: when a worker_profile is missing for the requested engine/role,
    _make_cli_worker must raise WorkerSpawnRejected WITHOUT charging _spawned_total
    (the old code did _reserve_worker_spawn() BEFORE resolving the profile, then
    bailed — leaking a phantom spawn toward max_total_workers and crashing the
    coordinator with a bare RuntimeError)."""
    from dswarm.swarm.swarm import WorkerSpawnRejected
    sw = _coordinator_swarm(
        challenge, tmp_path,
        worker_profiles=[{"id": "pi-sub", "engine": "pi",
                          "roles": ["review"], "runtime": "local"}],
    )
    before = sw._spawned_total
    # request a role the profile can't serve → rejected, not budget-charged
    with pytest.raises(WorkerSpawnRejected):
        sw._make_cli_worker("pi", mode="bootstrap")
    assert sw._spawned_total == before, "a rejected spawn must not charge the budget"
    # and it must NOT be a plain RuntimeError that spawn sites don't catch
    assert issubclass(WorkerSpawnRejected, RuntimeError)


def test_pi_subscription_uses_profile_capacity_not_account_mutex(challenge, tmp_path):
    """pi subscription profiles are not account-mutexed. They obey the profile's
    ordinary worker capacity just like any other profile."""
    sw = _coordinator_swarm(
        challenge, tmp_path,
        worker_profiles=[{
            "id": "pi-sub",
            "engine": "pi",
            "credential_mode": "subscription",
            "credential_account": "pi-main",
            "roles": ["bootstrap", "explore"],
            "runtime": "local",
            "max_running": 2,
            "enabled": True,
        }],
        engines=["pi-sub"],
    )
    first = sw._profile_for_engine("pi", role="bootstrap")
    assert first is not None
    sw._claim_worker_account("cli-pi-1", "pi", first, role="bootstrap")

    second = sw._profile_for_engine("pi", role="explore", advance=False)
    assert second is not None
    sw._claim_worker_account("cli-pi-2", "pi", second, role="explore")
    assert sw._profile_for_engine("pi", role="bootstrap", advance=False) is None

    class _S:
        def __init__(self, sid): self.solver_id = sid

    sw._release_worker_account(_S("cli-pi-1"))
    assert sw._profile_for_engine("pi", role="bootstrap", advance=False) is not None


def test_pi_config_links_replace_stale_copied_config(tmp_path):
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    ext = home / ".pi" / "agent" / "extensions"
    ext.mkdir(parents=True)
    (ext / "old-provider.ts").write_text("// stale image copy", encoding="utf-8")
    settings = home / ".pi" / "agent" / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    swarm_mod._ensure_pi_config_links(home, config_target_root="/fresh/pi-config")

    assert (home / ".pi/agent/extensions").is_symlink()
    assert str((home / ".pi/agent/extensions").readlink()).replace("\\", "/") == (
        "/fresh/pi-config/extensions")
    assert (home / ".pi/agent/settings.json").is_symlink()
    assert str((home / ".pi/agent/settings.json").readlink()).replace("\\", "/") == (
        "/fresh/pi-config/settings.json")


def test_container_runtime_links_blackboard_skill_into_isolated_home(challenge, tmp_path, monkeypatch):
    import dswarm.solver.container_exec as container_exec

    wroot = tmp_path / "workspace" / "workers"
    wroot.mkdir(parents=True, exist_ok=True)
    sw = _coordinator_swarm(
        challenge, tmp_path, worker_backend="container", worker_root=wroot,
    )
    monkeypatch.delenv("DSWARM_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DSWARM_BLACKBOARD_URL", raising=False)
    monkeypatch.setenv("DSWARM_MODEL_GATEWAY_PORT", "19101")
    chowned = []
    monkeypatch.setattr(
        container_exec, "_chown_tree_to_worker",
        lambda path: chowned.append(Path(path)),
    )

    class _FakeContainer:
        def to_container_path(self, path: str) -> str:
            # separator-normalize: container paths are always POSIX
            return (path.replace(str(tmp_path / "workspace"), "/home/kali/workspace")
                    .replace("\\", "/"))

    env = sw._runtime_env_for(
        "pi", "cli-pi", container=_FakeContainer(), task_token="task-token"
    )

    assert env["HOME"] == "/home/kali/workspace/homes/cli-pi"
    assert env["PI_CODING_AGENT_DIR"] == "/home/kali/workspace/homes/cli-pi/.pi/agent"
    assert env["DSWARM_TASK_TOKEN"] == "task-token"
    assert env["DEEPSEEK_API_KEY"] == "task-token"
    assert env["DSWARM_PI_PROVIDER"] == "ctf-gateway"
    assert env["DSWARM_GATEWAY_URL"] == "http://host.docker.internal:19101/v1"
    assert env["DSWARM_WORKER_MODEL"] == "deepseek-v4-flash"
    home = tmp_path / "workspace" / "homes" / "cli-pi"
    assert chowned == [home]
    expected_links = {
        ".pi/agent/skills/dswarm-blackboard": "/home/kali/workspace/.dswarm_runtime/dswarm-blackboard",
        ".pi/agent/settings.json": "/home/kali/workspace/.dswarm_runtime/pi-config/settings.json",
        ".pi/agent/models-store.json": "/home/kali/workspace/.dswarm_runtime/pi-config/models-store.json",
        ".pi/agent/models.json": "/home/kali/workspace/.dswarm_runtime/pi-config/models.json",
        ".pi/agent/extensions": "/home/kali/workspace/.dswarm_runtime/pi-config/extensions",
    }
    for rel, target in expected_links.items():
        link = home / rel
        assert link.is_symlink()
        # WindowsPath normalizes separators; the target is a POSIX container path
        assert str(link.readlink()).replace("\\", "/") == target
    runtime_provider = (tmp_path / "workspace" / ".dswarm_runtime" / "pi-config"
                        / "extensions" / "dswarm-worker-provider.ts")
    assert runtime_provider.is_file()
    assert 'readSecret("OPENAI_API_KEY")' in runtime_provider.read_text(encoding="utf-8")



@pytest.mark.asyncio
async def test_run_teardown_revokes_all_tokens_for_run(challenge, tmp_path, monkeypatch):
    from dswarm.solver.modelgateway import ModelGateway, WorkerClaims
    from dswarm.solver.types import SolveOutcome

    gateway = ModelGateway(host="127.0.0.1", port=0)
    token = gateway.issue_worker(WorkerClaims(
        run_id="teardown-run", challenge_id=challenge.id,
        worker_instance_id="late-worker", solver_id="cli-pi",
        profile_id="pi", configured_account_id=None, token_scope="worker",
    ))
    monkeypatch.setattr(ModelGateway, "instance", staticmethod(lambda: gateway))
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.run_id = "teardown-run"
    monkeypatch.setattr(sw, "_reconcile_blackboard_skill", lambda: asyncio.sleep(0))
    async def finish():
        return SolveOutcome(False, None, 0, None, "stopped")
    monkeypatch.setattr(sw, "_run_reason_scheduler", finish)

    outcome = await sw.run()

    assert outcome.solved is False
    assert gateway.claims_for_token(token) is None


# ── review-policy sanitization ───────────────────────────────────────────────

def test_container_runtime_prefers_mapped_shared_graph_db(
        challenge, tmp_path, monkeypatch):
    import dswarm.solver.container_exec as container_exec

    workspace = tmp_path / "workspace"
    worker_root = workspace / "workers"
    worker_root.mkdir(parents=True)
    sw = _coordinator_swarm(
        challenge, tmp_path, worker_backend="container", worker_root=worker_root,
        graph_dir=workspace / "graph", blackboard_token="bb-token",
    )
    monkeypatch.delenv("DSWARM_BLACKBOARD_URL", raising=False)
    monkeypatch.setattr(container_exec, "_chown_tree_to_worker", lambda _path: None)

    class FakeContainer:
        def to_container_path(self, path: str) -> str:
            return (path.replace(str(workspace), "/home/kali/workspace")
                    .replace("\\", "/"))

    env = sw._runtime_env_for("pi", "cli-pi", container=FakeContainer())

    assert env["DSWARM_BLACKBOARD_DB"] == "/home/kali/workspace/graph/shared_graph.db"
    assert env["DSWARM_CHALLENGE_ID"] == challenge.id
    assert "DSWARM_BLACKBOARD_URL" not in env
    assert "DSWARM_BLACKBOARD_RUN_ID" not in env
    assert "DSWARM_BLACKBOARD_TOKEN" not in env


def test_container_runtime_explicit_blackboard_url_overrides_shared_db(
        challenge, tmp_path, monkeypatch):
    import dswarm.solver.container_exec as container_exec

    workspace = tmp_path / "workspace"
    worker_root = workspace / "workers"
    worker_root.mkdir(parents=True)
    sw = _coordinator_swarm(
        challenge, tmp_path, worker_backend="container", worker_root=worker_root,
        graph_dir=workspace / "graph", blackboard_token="bb-token",
    )
    monkeypatch.setenv(
        "DSWARM_BLACKBOARD_URL", "http://blackboard.internal:8000/api/blackboard")
    monkeypatch.setattr(container_exec, "_chown_tree_to_worker", lambda _path: None)

    class FakeContainer:
        def to_container_path(self, path: str) -> str:
            return (path.replace(str(workspace), "/home/kali/workspace")
                    .replace("\\", "/"))

    env = sw._runtime_env_for("pi", "cli-pi", container=FakeContainer())

    assert env["DSWARM_BLACKBOARD_DB"] == "/home/kali/workspace/graph/shared_graph.db"
    assert env["DSWARM_BLACKBOARD_URL"] == \
        "http://blackboard.internal:8000/api/blackboard"
    assert env["DSWARM_BLACKBOARD_RUN_ID"] == sw.run_id
    assert env["DSWARM_BLACKBOARD_TOKEN"] == "bb-token"


def test_runtime_uses_http_blackboard_fallback_without_shared_graph(
        challenge, tmp_path, monkeypatch):
    sw = _coordinator_swarm(challenge, tmp_path, blackboard_token="bb-token")
    if sw.shared_graph is not None:
        sw.shared_graph.close()
    sw.shared_graph = None
    monkeypatch.delenv("DSWARM_BLACKBOARD_URL", raising=False)

    env = sw._runtime_env_for("pi", "cli-pi", container=None)

    assert env["DSWARM_BLACKBOARD_URL"].endswith("/api/blackboard")
    assert env["DSWARM_BLACKBOARD_RUN_ID"] == sw.run_id
    assert env["DSWARM_BLACKBOARD_TOKEN"] == "bb-token"
    assert "DSWARM_BLACKBOARD_DB" not in env


def test_runtime_env_injects_profile_effort_and_endpoint(
        challenge, tmp_path, monkeypatch):
    from dswarm.solver.credential_accounts import (
        CredentialAccountStore,
        account_store_root,
    )

    root = account_store_root(tmp_path)
    CredentialAccountStore(root).upsert_secret(
        account_id="pi-web-main",
        engine="api",
        secret="profile-key",
        base_url="https://custom.example/v1",
        target_engine="pi",
    )
    sw = _coordinator_swarm(
        challenge, tmp_path, credential_accounts_root=str(root)
    )
    profile = {
        "id": "pi-web",
        "label": "pi-web",
        "credential_account": "pi-web-main",
        "model": "deepseek-v4-pro",
        "effort": "high",
        "base_url": "https://custom.example/v1",
        "wire_api": "openai-responses",
        "auth_mode": "custom",
        "auth_header": "X-API-Token",
        "auth_prefix": "Token",
    }

    env = sw._runtime_env_for(
        "pi", "cli-pi", container=None, profile=profile
    )

    assert env["DSWARM_WORKER_MODEL"] == "deepseek-v4-pro"
    assert env["DSWARM_WORKER_THINKING"] == "high"
    assert env["DSWARM_PI_PROVIDER"] == "dswarm-worker"
    assert env["OPENAI_BASE_URL"] == "https://custom.example/v1"
    assert env["OPENAI_API_KEY"] == "profile-key"
    assert env["DSWARM_WORKER_BASE_URL"] == "https://custom.example/v1"
    assert env["DSWARM_WORKER_WIRE_API"] == "openai-responses"
    assert env["DSWARM_WORKER_AUTH_MODE"] == "custom"
    assert env["DSWARM_WORKER_AUTH_HEADER"] == "X-API-Token"
    assert env["DSWARM_WORKER_AUTH_PREFIX"] == "Token"
    assert env["DSWARM_WORKER_API_KEY"] == "profile-key"


def test_container_runtime_endpoint_uses_secret_file_and_runtime_pi_config(
        challenge, tmp_path, monkeypatch):
    import dswarm.solver.container_exec as container_exec
    from dswarm.solver.credential_accounts import (
        CredentialAccountStore,
        account_store_root,
    )

    workspace = tmp_path / "workspace"
    worker_root = workspace / "workers"
    worker_root.mkdir(parents=True)
    root = account_store_root(tmp_path)
    CredentialAccountStore(root).upsert_secret(
        account_id="pi-web-main",
        engine="api",
        secret="profile-key",
        base_url="https://custom.example/v1",
        target_engine="pi",
    )
    sw = _coordinator_swarm(
        challenge, tmp_path, worker_backend="container", worker_root=worker_root,
        credential_accounts_root=str(root),
    )
    monkeypatch.setattr(container_exec, "_chown_tree_to_worker", lambda _path: None)

    class FakeContainer:
        def to_container_path(self, path: str) -> str:
            return path.replace(str(workspace), "/home/kali/workspace").replace("\\", "/")

    profile = {
        "id": "pi-web",
        "label": "pi-web",
        "credential_account": "pi-web-main",
        "model": "deepseek-v4-pro",
        "effort": "high",
        "base_url": "https://custom.example/v1",
        "wire_api": "openai-responses",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    }

    env = sw._runtime_env_for(
        "pi", "cli-pi", container=FakeContainer(), profile=profile
    )

    assert env["DSWARM_PI_PROVIDER"] == "dswarm-worker"
    assert env["OPENAI_BASE_URL"] == "https://custom.example/v1"
    assert env["DSWARM_WORKER_BASE_URL"] == "https://custom.example/v1"
    assert env["OPENAI_API_KEY_FILE"] == "/run/dswarm/accounts/pi-web-main/API_KEY"
    assert env["DSWARM_WORKER_API_KEY_FILE"] == "/run/dswarm/accounts/pi-web-main/API_KEY"
    assert env["PI_CODING_AGENT_DIR"] == "/home/kali/workspace/homes/cli-pi/.pi/agent"
    assert "OPENAI_API_KEY" not in env
    assert "DSWARM_WORKER_API_KEY" not in env

    home = workspace / "homes" / "cli-pi"
    assert (workspace / ".dswarm_runtime" / "pi-config" / "extensions"
            / "dswarm-worker-provider.ts").is_file()
    assert str((home / ".pi/agent/extensions").readlink()).replace("\\", "/") == (
        "/home/kali/workspace/.dswarm_runtime/pi-config/extensions")


@pytest.mark.asyncio
async def test_finalize_refreshes_workspace_board_atomically_and_idempotently(
        challenge, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    worker_root = workspace / "workers"
    worker_root.mkdir(parents=True)
    sw = _coordinator_swarm(
        challenge, tmp_path, worker_root=worker_root, graph_dir=workspace / "graph")
    assert sw.shared_graph is not None
    sw.shared_graph.propose_intent(
        actor="reason", intent_id="I-final-board", goal="inspect the final route")
    assert sw.shared_graph.claim_intent(
        worker="cli-pi", intent_id="I-final-board") is True

    board_path = workspace / ".dswarm_board.md"
    board_path.write_text(
        "<!-- dswarm-team-board -->\n## Open intents\n- stale claimed intent\n",
        encoding="utf-8",
    )

    async def fail_bus_drain(**_kwargs):
        raise RuntimeError("simulated bus failure")

    monkeypatch.setattr(sw, "_drain_graph_to_bus", fail_bus_drain)

    await sw._finalize_coordinator_run(
        winner="cli-pi", flag="flag{done}", goal_complete=True, per_solver={})

    first = board_path.read_text(encoding="utf-8")
    assert first.startswith("<!-- dswarm-team-board -->\n")
    assert "## Open intents" not in first
    assert "## Already attempted" in first
    assert "inspect the final route" in first
    assert "closed_by_solve" in first
    assert "stale claimed intent" not in first

    await sw._finalize_coordinator_run(
        winner="cli-pi", flag="flag{done}", goal_complete=True, per_solver={})
    assert board_path.read_text(encoding="utf-8") == first


def test_clean_review_policy_preserves_max_challenges_per_cycle():
    """run-75377 knob: an operator-set max_challenges_per_cycle must survive
    _clean_review_policy. It used to be dropped (not in the int whitelist), so the
    read site always fell back to the hard-coded 8 — the knob was inert."""
    cleaned = Swarm._clean_review_policy({"enabled": True, "max_challenges_per_cycle": 3})
    assert cleaned["max_challenges_per_cycle"] == 3
    # absent → documented default of 8 (consistent with siblings like max_review_workers)
    assert Swarm._clean_review_policy({})["max_challenges_per_cycle"] == 8
    # a configured 0 is preserved as a genuine "disable challenge fan-out this cycle",
    # NOT silently rewritten to 8 (the read site honors the same floor).
    assert Swarm._clean_review_policy({"max_challenges_per_cycle": 0})["max_challenges_per_cycle"] == 0
    # garbage / negative is coerced: non-int falls back to default, negative clamps to 0.
    assert Swarm._clean_review_policy({"max_challenges_per_cycle": "nope"})["max_challenges_per_cycle"] == 8
    assert Swarm._clean_review_policy({"max_challenges_per_cycle": -5})["max_challenges_per_cycle"] == 0


# ── End-to-end lifecycle integration (TODO_IMPLEMENTATION_DISCUSSION Phases 1-7) ──

def test_lifecycle_integration_directive_review_resource_compact(challenge, tmp_path: Path):
    """Drive the full new lifecycle through the real Swarm + SharedGraph on a local
    mock challenge: operator directive (B) → claimable directive intent; fact review
    reject/merge (A) → snapshot filtering; resource lock (E) → dispatch preflight;
    finalize-by-stop-reason (J); compaction (H). No API key / scripted worker."""
    sw = _coordinator_swarm(challenge, tmp_path, max_workers=2)
    g = sw.shared_graph

    # --- A: facts + review lifecycle ---
    f_good = g.add_evidence(actor="cli-a", source="x", fact="SMB open on .45", verified=True)
    f_bad = g.add_evidence(actor="cli-b", source="x", fact="bogus RCE on .45", verified=True)
    f_dup = g.add_evidence(actor="cli-c", source="x", fact="port 445 listening", verified=True)
    g.reject_fact(actor="review", fact_seq=f_bad, reason="not reproducible")
    g.merge_fact(actor="review", from_fact_seq=f_dup, to_fact_seq=f_good, reason="same finding")
    facts = [e.fact for e in g.snapshot().evidence]
    assert facts == ["SMB open on .45"]  # rejected + merged dropped

    # --- B: operator directive → first-class steering + claimable directive intent ---
    info = g.add_operator_directive(action="redirect", text="pivot to internal 10.0.0.5",
                                    preempt_policy="soft_rebind")
    g.propose_intent(actor="operator", intent_id=f"I-{info['directive_id']}",
                     goal="pivot to internal 10.0.0.5",
                     payload={"directive_id": info["directive_id"], "priority": "operator"})
    summary = g.to_reason_summary()
    assert "Operator directives" in summary and "pivot to internal 10.0.0.5" in summary

    # --- E: resource lock + dispatch preflight (an intent on a locked resource is held) ---
    g.request_resource_lock(actor="cli-x", resource_key="destructive:tcp:445@10.0.0.5",
                            owner_worker="cli-x")
    g.propose_intent(actor="reason", intent_id="I-collide",
                     goal="exploit 10.0.0.5 smb",
                     payload={"resource_key": "destructive:tcp:445@10.0.0.5"})
    g.propose_intent(actor="reason", intent_id="I-free", goal="enumerate users")
    dispatchable = {i["intent_id"] for i in sw._open_intents()}
    assert "I-collide" not in dispatchable  # locked resource → preflight skip
    assert "I-free" in dispatchable
    assert f"I-{info['directive_id']}" in dispatchable  # operator intent dispatchable

    # --- H: compaction retires a barren closed intent, keeps facts ---
    g.propose_intent(actor="reason", intent_id="I-barren", goal="dead direction")
    g.claim_intent(worker="cli-z", intent_id="I-barren")
    g.conclude_intent(actor="cli-z", intent_id="I-barren", result="no flag")
    cinfo = g.compact_graph(trigger="no_progress_time")
    assert "I-barren" in cinfo["retired_intent_ids"]
    assert any(e.fact == "SMB open on .45" for e in g.snapshot().evidence)

    # --- J: a non-solved crash terminal (runtime_failure) holds active intents back
    # as resume; revive flips them active again. (operator_stop now CLOSES instead —
    # see test_shared_graph.test_finalize_operator_stop_closes_active_intents.)
    out = g.release_claims_for_finalize(reason="runtime_failure")
    assert "I-free" in out["resumed_intents"]
    assert g.open_goal_texts() == []  # all resume/retired, nothing active

    # revive restores them
    revived = g.revive_resume_intents()
    assert "I-free" in revived


# ── split-brain flag-completion source of truth (run-75379 BUG④ ─────────────
# A flag reaches the AUTHORITATIVE shared graph the moment a worker accepts it
# (_accept_flag → shared_graph.flag_found), but _found_flags (the in-memory list
# _flags_complete() reads) is fed ONLY from a reaped `outcome.flags`. A worker
# cancelled/errored right after it accepted a flag — or the live DB→bus bridge —
# can put a flag in the graph/UI without it ever reaching _found_flags. In
# run-75379 the graph held 4 valid flags while _found_flags was stuck at 2, so
# the run never finalized and spawned ~55 post-solve waves. _sync_flags_from_graph
# reconciles against the snapshot so completion reads the real flag count.

def test_sync_flags_from_graph_absorbs_graph_only_flags(challenge, tmp_path: Path):
    """Flags recorded ONLY via the shared-graph path (never via outcome.flags) must
    make _flags_complete() true — no split-brain. Since the wiring commit,
    _flags_complete() itself reconciles against the authoritative graph before
    every verdict, so a lost clean outcome can no longer blind completion."""
    challenge.expected_flags = 2
    challenge.multi_flag = True
    sw = _coordinator_swarm(challenge, tmp_path)
    assert sw.shared_graph is not None
    # two flags land on the graph (as _accept_flag would) but _found_flags is empty:
    # the worker outcomes never delivered them (cancelled-after-accept / DB bridge).
    sw.shared_graph.flag_found(actor="cli-x", flag="flag{a}", intent_id=None)
    sw.shared_graph.flag_found(actor="cli-y", flag="flag{b}", intent_id=None)
    assert sw._found_flags == []

    assert sw._flags_complete() is True                  # wired reconcile absorbs them
    assert set(sw._found_flags) == {"flag{a}", "flag{b}"}
    assert sw._sync_flags_from_graph() == []             # idempotent once absorbed


def test_sync_flags_drops_operator_invalidated_flag(challenge, tmp_path: Path):
    """An operator-invalidated (blacklisted) flag must NOT count toward
    expected_flags after reconcile (BUG③ cross-check): syncing FROM the snapshot,
    which already excludes invalidated flags, removes it from the in-memory set."""
    challenge.expected_flags = 2
    challenge.multi_flag = True
    sw = _coordinator_swarm(challenge, tmp_path)
    assert sw.shared_graph is not None
    sw.shared_graph.flag_found(actor="cli-x", flag="flag{real}", intent_id=None)
    sw.shared_graph.flag_found(actor="cli-y", flag="flag{bogus}", intent_id=None)
    sw._sync_flags_from_graph()
    assert sw._flags_complete() is True                  # 2/2 before invalidation

    # operator marks the bogus one false → it must leave the count.
    sw.shared_graph.reopen_after_false_positive(actor="operator", flag="flag{bogus}")
    sw._sync_flags_from_graph()

    assert sw._found_flags == ["flag{real}"]             # bogus dropped
    assert sw._flags_complete() is False                 # 1/2 again, not falsely complete


def test_sync_flags_noop_without_graph(challenge, tmp_path: Path):
    """No shared graph → reconcile is a no-op and never wipes the in-memory set
    (a transient/absent graph must not erase genuinely-held flags)."""
    challenge.expected_flags = 1
    sw = _coordinator_swarm(challenge, tmp_path)
    sw.shared_graph = None
    sw._found_flags = ["flag{kept}"]
    assert sw._sync_flags_from_graph() == []
    assert sw._found_flags == ["flag{kept}"]             # untouched


def test_flags_complete_falls_back_to_memory_when_graph_unreadable(
    challenge, tmp_path: Path,
):
    """The run-75379 wiring is strictly additive: when the graph snapshot cannot
    be read (DB failure), completion degrades to the pre-wiring memory-only
    verdict instead of raising or wiping held flags."""
    challenge.expected_flags = 1
    sw = _coordinator_swarm(challenge, tmp_path)
    assert sw.shared_graph is not None
    real_graph = sw.shared_graph

    class _Unreadable:
        def snapshot(self):
            raise RuntimeError("db locked")

        def invalidated_flags(self):
            raise RuntimeError("db locked")

    sw.shared_graph = _Unreadable()
    try:
        sw._found_flags = ["flag{mem}"]
        assert sw._flags_complete() is True              # memory still decides
        assert sw._found_flags == ["flag{mem}"]          # nothing wiped
    finally:
        sw.shared_graph = real_graph
    # with a readable graph restored, an absent-from-graph flag stays held but the
    # graph's own valid flags are absorbed on the next verdict.
    real_graph.flag_found(actor="cli-x", flag="flag{graph}", intent_id=None)
    assert sw._flags_complete() is True
    assert set(sw._found_flags) >= {"flag{mem}", "flag{graph}"}


def test_flags_complete_absorbs_single_flag_run_from_graph(challenge, tmp_path: Path):
    """expected_flags=1 race mode: one graph-only accepted flag finishes the run —
    byte-identical guarantee to first-flag-wins, now backed by the graph."""
    challenge.expected_flags = 1
    sw = _coordinator_swarm(challenge, tmp_path)
    assert sw._flags_complete() is False
    sw.shared_graph.flag_found(actor="cli-x", flag="flag{single}", intent_id=None)
    assert sw._flags_complete() is True


def test_flags_complete_never_finishes_unknown_count_collect_mode(
    challenge, tmp_path: Path,
):
    """multi_flag=True with expected_flags<=1 never completes by count, even when
    the graph holds many accepted flags."""
    challenge.multi_flag = True
    challenge.expected_flags = 1
    sw = _coordinator_swarm(challenge, tmp_path)
    for i in range(5):
        sw.shared_graph.flag_found(actor=f"cli-{i}", flag=f"flag{{{i}}}",
                                   intent_id=None)
    assert sw._flags_complete() is False

async def test_review_factory_exception_releases_reserved_lane(
    challenge, tmp_path: Path, monkeypatch,
):
    sw = _coordinator_swarm(
        challenge, tmp_path, max_workers=1,
        stage_policy={"coordinator": {"review": {
            "enabled": True, "engine": "pi", "max_concurrent": 1,
            "cooldown_events": 0,
        }}},
    )
    monkeypatch.setattr(sw, "_select_review_engine", lambda healthy: "pi")

    def boom(*args, **kwargs):
        raise RuntimeError("review construction failed")

    monkeypatch.setattr(sw, "_make_cli_worker", boom)

    async def emit_bb(kind, **fields):
        return None

    with pytest.raises(RuntimeError, match="review construction failed"):
        await sw._maybe_start_review(
            trigger="operator_hint",
            directive="audit",
            healthy=["pi"],
            tasks={},
            task_solvers={},
            emit_bb=emit_bb,
        )

    assert sw._worker_lane_gate.snapshot() == {
        "ordinary_active": 0, "review_active": 0,
    }


def test_container_backend_without_frozen_policy_fails_closed(
    challenge, tmp_path: Path,
) -> None:
    from dswarm.solver.runtime_policy import RuntimePolicyError

    swarm = Swarm(
        challenge,
        [ModelSpec(solver_id="seat", model="mock")],
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sbx"),
        artifacts=ArtifactStore(root=tmp_path / "arts"),
        executor="cli",
        worker_backend="container",
    )

    assert swarm._backend_for_engine("pi") == "container"
    with pytest.raises(RuntimePolicyError, match="runtime_policy_required"):
        swarm._make_cli_worker("pi", mode="bootstrap")
