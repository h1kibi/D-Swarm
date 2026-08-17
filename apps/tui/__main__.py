"""Launchable TUI entrypoint:  `uv run python -m apps.tui [options]`.

`apps/tui/app.py` only defines the `DSwarmTUI` widget — it has no runner, so
`python -m apps.tui.app` just loads classes and exits. THIS module is the real
launcher: it wires an EventBus + a background driver (mock or real swarm) to the
TUI and runs it.

Modes:
  (no args)            mock driver — scripted event stream, NO API key, UI demo.
  --swarm KEY          solve a real NYU-bench challenge by key (needs a key).
  --swarm --desc "…" --target URL --category web
                       solve an ad-hoc challenge described inline.

The TUI is a dumb subscriber (§3): it renders the bus and routes HITL commands
back into the run. Same contract the web deck uses.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping

from dswarm.core.cost import CostController
from dswarm.core.dotenv_boot import load_env
from dswarm.core.event_bus import EventBus
from dswarm.core.session_store import SessionStore
from dswarm.core.events import Event, EventType, hitl_response_payload

from apps.tui.app import DSwarmTUI

load_env()  # pick up repo-root .env so --swarm finds the key (shell env wins)


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m apps.tui",
                                description="Project D-Swarm TUI command deck")
    p.add_argument("--swarm", action="store_true",
                   help="run the real solver swarm (needs DSWARM_DEEPSEEK_API_KEY)")
    p.add_argument("--key", default="",
                   help="NYU-bench challenge key to solve (with --swarm)")
    p.add_argument("--desc", default="", help="ad-hoc challenge description")
    p.add_argument("--target", default="", help="ad-hoc challenge target URL/host")
    p.add_argument("--category", default="web",
                   help="track: web/crypto/reverse/forensics/misc/pwn")
    p.add_argument("--n-solvers", type=int, default=2, help="swarm size")
    return p.parse_args(argv)


async def _run_owned_swarm(swarm: Any, sandbox: Any) -> None:
    """Run a swarm, then release worker resources in owner order.

    ``Swarm.run`` deliberately does not close its pool manager: web/TUI control
    planes own that lifecycle.  Sandbox cleanup must happen first so workers and
    their mounted workspace are gone before the long-lived pool is closed.
    """
    try:
        await swarm.run()
    finally:
        try:
            await sandbox.shutdown_all()
        finally:
            manager = getattr(swarm, "pool_manager", None)
            if manager is not None:
                await manager.close()


async def _mock_driver(bus: EventBus, cost: CostController, run_id: str) -> None:
    from examples.mock_solver import run_mock_solve

    await run_mock_solve(bus, cost, run_id=run_id)


async def _swarm_driver(
    bus: EventBus,
    cost: CostController,
    run_id: str,
    args: argparse.Namespace,
    *,
    runtime_context_factory: Callable[[str, argparse.Namespace], Mapping[str, Any]] | None = None,
) -> None:
    """Run the real TUI path with a per-run Docker runtime owner.

    The mock path never calls this function, so it cannot construct snapshots,
    managers, Docker clients, or credential projections.
    """
    import os

    from apps.web.worker_config import WorkerConfigStore
    from dswarm.core.llm import LLMClient
    from dswarm.solver.credential_accounts import account_store_root, ensure_pi_account_from_env
    from dswarm.solver.runtime_factory import build_docker_runtime_context
    from dswarm.solver.worker_profiles import normalize_profile_roster
    from dswarm.learning.distill import TemplateStore
    from dswarm.models.solve_graph import Challenge
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.solver.result import ArtifactStore
    from dswarm.solver.types import SolverConfig
    from dswarm.swarm.budget import ProfileBudgetGate
    from dswarm.swarm.models import default_lineup
    from dswarm.swarm.swarm import Swarm

    sessions_root = Path(
        os.environ.get("DSWARM_SESSIONS_ROOT")
        or (
            Path(os.environ["DSWARM_HOST_DATA_ROOT"]) / "sessions"
            if os.environ.get("DSWARM_HOST_DATA_ROOT")
            else Path("sessions")
        )
    )
    sessions_root.mkdir(parents=True, exist_ok=True)
    root = sessions_root / run_id
    for child in ("workspace", "sbx", "arts", "graph"):
        (root / child).mkdir(parents=True, exist_ok=True)

    challenge = Challenge(
        id=run_id, name=args.key or args.desc[:32] or run_id,
        category=args.category, points=0, description=args.desc,
        target=args.target or None,
        flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}",
    )
    sandbox = SandboxManager(bus=bus, root=root / "sbx")
    arts = ArtifactStore(root=root / "arts")
    knowledge = TemplateStore(root=os.environ.get("DSWARM_KNOWLEDGE_DIR", "knowledge"))

    store = SessionStore(sessions_root)
    bus.add_critical_sink(store.sink, store.append_checked)

    factory_supplied = runtime_context_factory is not None
    if factory_supplied:
        runtime_context = dict(runtime_context_factory(run_id, args))
        budget_gate = runtime_context.get("budget_gate") or ProfileBudgetGate()
        selected_profiles: list[dict[str, Any]] = []
        names: list[str] = []
        cfg: dict[str, Any] = {}
    else:
        ensure_pi_account_from_env(sessions_root)
        cfg = WorkerConfigStore(sessions_root).resolve(args.category)
        profiles = [
            dict(profile) for profile in cfg.get("worker_profiles", [])
            if isinstance(profile, Mapping)
        ]
        names = normalize_profile_roster(cfg.get("engines", []), profiles)
        by_name = {
            str(profile.get("name") or profile.get("id") or ""): profile
            for profile in profiles
        }
        selected_profiles = [by_name[name] for name in names if name in by_name]
        budget_gate = ProfileBudgetGate()
        runtime_context = build_docker_runtime_context(
            run_id=run_id,
            sessions_root=sessions_root,
            bus=bus,
            budget_gate=budget_gate,
            worker_profiles=selected_profiles,
            runtime_profiles=cfg.get("runtime_profiles", []),
            run_max_workers=cfg.get("max_workers", 0),
        )

    if factory_supplied:
        # Preserve the historical injection seam: callers that provide a runtime
        # factory own all runtime fields and must not trigger default worker
        # config, snapshot, or Docker composition.
        swarm_kwargs = dict(runtime_context)
    else:
        swarm_kwargs = {
            "engines": names,
            "worker_profiles": selected_profiles,
            "runtime_profiles": cfg.get("runtime_profiles"),
            "max_workers": cfg.get("max_workers", args.n_solvers),
            "start_workers": cfg.get("start_workers", args.n_solvers),
            "wall_clock_budget": cfg.get("wall_clock_budget"),
            "max_total_workers": cfg.get("max_total_workers"),
            "cost_budget_usd": cfg.get("cost_budget_usd"),
            "stage_policy": cfg.get("stage_policy"),
            "budget_gate": budget_gate,
            "credential_accounts_root": account_store_root(sessions_root),
            "worker_root": root / "workspace",
            "graph_dir": root / "graph",
            **runtime_context,
        }
        swarm_kwargs = {key: value for key, value in swarm_kwargs.items() if value is not None}

    async with LLMClient(cost=cost, bus=bus) as llm:
        swarm = Swarm(
            challenge, default_lineup(args.n_solvers), llm=llm, sandbox=sandbox,
            bus=bus, cost=cost, artifacts=arts, config=SolverConfig(),
            run_id=run_id, knowledge=knowledge, executor="cli",
            **swarm_kwargs,
        )
        await _run_owned_swarm(swarm, sandbox)


def _driver_for_args(
    bus: EventBus,
    cost: CostController,
    run_id: str,
    args: argparse.Namespace,
    *,
    runtime_context_factory: Callable[[str, argparse.Namespace], Mapping[str, Any]] | None = None,
):
    """Select a driver without touching runtime/Docker state for mock mode."""
    if args.swarm:
        lineup = f"swarm×{args.n_solvers} ({args.category})"
        driver = _swarm_driver(
            bus,
            cost,
            run_id,
            args,
            runtime_context_factory=runtime_context_factory,
        )
    else:
        lineup = "mock (UI demo — pass --swarm to solve for real)"
        driver = _mock_driver(bus, cost, run_id)
    return lineup, driver


async def _amain(args: argparse.Namespace) -> None:
    run_id = "tui-run"
    bus = EventBus()
    cost = CostController(bus=bus)
    lineup, driver = _driver_for_args(bus, cost, run_id, args)

    async def _run_driver() -> None:
        try:
            await driver
        finally:
            await bus.close()

    driver_task = asyncio.create_task(_run_driver())

    async def hitl(target: str, action: str, text: str) -> None:
        await bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload(target, action, text=text),
        ))

    app = DSwarmTUI(bus, hitl=hitl, lineup=lineup, stop_on_finish=False)
    try:
        await app.run_async()
    finally:
        driver_task.cancel()
        await asyncio.gather(driver_task, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    asyncio.run(_amain(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
