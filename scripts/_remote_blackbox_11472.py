import asyncio
import json
import os
from pathlib import Path

from dswarm.core.dotenv_boot import load_env

load_env()

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.llm import LLMClient
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.swarm.swarm import Swarm

TARGET = "http://49.232.142.230:11472/"
RUN_ID = "remote-blackbox-11472"

async def main() -> int:
    os.environ.setdefault("DSWARM_DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    if not os.environ.get("DSWARM_DEEPSEEK_API_KEY"):
        print("missing provider key")
        return 2
    root = Path("sessions") / RUN_ID
    for child in ("workspace", "sbx", "arts", "graph"):
        (root / child).mkdir(parents=True, exist_ok=True)
    challenge = Challenge(
        id=RUN_ID,
        name="remote-upload-fire",
        category="web",
        description=(
            "Authorized black-box CTF web challenge. Inspect and interact only with the live target; "
            "do not read challenge source or a solution. Find the real flag if present and prove it "
            "through actual command/tool output. The landing page exposes a multipart upload form and "
            "an HTML comment hints at read.php filename access."
        ),
        target=TARGET,
        flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}",
        expected_flags=1,
    )
    bus = EventBus()
    events = []
    bus.add_sink(lambda event: events.append(event))
    cost = CostController(bus=bus)
    sandbox = SandboxManager(bus=bus, root=root / "sbx")
    artifacts = ArtifactStore(root=root / "arts")
    try:
        async with LLMClient(cost=cost, bus=bus) as llm:
            swarm = Swarm(
                challenge, llm=llm, sandbox=sandbox, bus=bus, cost=cost,
                artifacts=artifacts, run_id=RUN_ID, executor="cli", engines=["pi"],
                web_access=True, max_workers=2, worker_root=root / "workspace" / "workers",
                graph_dir=root / "graph", reason_model="deepseek-v4-flash",
                wall_clock_budget=300.0, barren_limit=3,
            )
            outcome = await swarm.run()
    finally:
        await sandbox.shutdown_all()
    trace = {
        "run_id": RUN_ID,
        "target": TARGET,
        "solved": bool(outcome.solved),
        "flags": list(outcome.flags or []),
        "winner": str(outcome.winner or ""),
        "event_types": [event.event_type.value for event in events],
        "events": [
            {"seq": int(getattr(event, "seq", 0) or 0), "type": event.event_type.value,
             "solver_id": event.solver_id, "payload": event.payload}
            for event in events
        ],
    }
    (root / "blackbox-trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_id": RUN_ID, "solved": trace["solved"], "flags": trace["flags"], "event_types": trace["event_types"]}, ensure_ascii=False))
    return 0 if trace["solved"] else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
