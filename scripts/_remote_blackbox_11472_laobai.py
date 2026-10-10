import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from dswarm.core.dotenv_boot import load_env
load_env()

ROOT = Path("sessions/remote-blackbox-14598-zhipu")
SESSIONS_ROOT = Path("sessions")
RUN_ID = "remote-blackbox-14598-zhipu"
TARGET = "http://49.232.142.230:14598/"
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
MODEL = "glm-5.3-flash"

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.llm import LLMClient
from dswarm.core.session_store import SessionStore
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.credential_accounts import account_store_root
from dswarm.solver.control_receiver import ControlReceiver
from dswarm.solver.modelgateway import ModelGateway
from dswarm.solver.result import ArtifactStore
from dswarm.solver.runtime_factory import build_docker_runtime_context
from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuilder
from dswarm.swarm.budget import ProfileBudgetGate
from dswarm.swarm.swarm import Swarm

async def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    subprocess.run(["docker", "rm", "-f", "dswarm-run-" + RUN_ID], capture_output=True, timeout=30)
    secret = Path("sessions/_secrets/llm_providers/zhipu/API_KEY")
    if not secret.is_file() or not secret.read_text(encoding="utf-8").strip():
        print("missing zhipu provider secret")
        return 2
    challenge = Challenge(
        id=RUN_ID,
        name="remote-upload-fire-zhipu-14598",
        category="web",
        description=(
            "Authorized black-box CTF web challenge. Use only the live target; do not read source or solution. "
            "Find and prove the real flag from actual command/tool output. The page exposes multipart upload "
            "and an HTML comment hints at read.php?filename=. Explore the upload and filename-read behavior safely."
        ),
        target=TARGET,
        flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}",
        expected_flags=1,
    )
    profiles = [{
        "id": "pi-web", "name": "pi-web", "engine": "pi", "transport": "pi",
        "provider_ref": "zhipu", "base_url": "", "api_key_ref": "", "runtime": "docker-web",
        "image": "ctf-swarm-pi:0.3.0-rc.1",
        "roles": ["recon", "bootstrap", "explore", "respond", "review"],
        "max_running": 2, "max_review_running": 1, "priority": 20,
        "model": MODEL, "effort": "high", "enabled": True,
    }]
    runtime_profiles = [{"id": "docker-web", "backend": "container", "label": "Docker web", "network": "bridge", "memory": "12g", "cpus": "4", "pids_limit": 2048}]
    bus = EventBus()
    events = []
    async def collect(event):
        events.append(event)
    store = SessionStore(SESSIONS_ROOT)
    bus.add_critical_sink(store.sink, store.append_checked)
    bus.add_sink(collect)
    cost = CostController(bus=bus)
    budget = ProfileBudgetGate()
    sandbox = SandboxManager(root=ROOT / "sandbox")
    artifacts = ArtifactStore(root=ROOT / "artifacts")
    key = secret.read_text(encoding="utf-8").strip()
    llm = LLMClient(api_key=key, base_url=BASE_URL, reasoning_effort="high", timeout=120, overall_timeout=240, cost=cost, bus=bus)
    review_llm = LLMClient(api_key=key, base_url=BASE_URL, reasoning_effort="high", timeout=120, overall_timeout=240, cost=cost, bus=bus)
    ControlReceiver.instance()
    gateway = ModelGateway.instance()
    gateway.account_root = str(account_store_root(SESSIONS_ROOT))
    gateway.sessions_root = str(SESSIONS_ROOT)
    gateway.configure_usage_bridge(bus=bus, loop=asyncio.get_running_loop(), run_id=RUN_ID)
    runtime = build_docker_runtime_context(run_id=RUN_ID, sessions_root=SESSIONS_ROOT, bus=bus, budget_gate=budget, worker_profiles=profiles, runtime_profiles=runtime_profiles, run_max_workers=2, snapshot_builder=RuntimeSnapshotBuilder())
    try:
        swarm = Swarm(challenge, llm=llm, review_llm=review_llm, sandbox=sandbox, bus=bus, cost=cost, artifacts=artifacts, run_id=RUN_ID, executor="cli", cli_engine="pi", engines=["pi-web"], web_access=True, kb=False, max_workers=2, budget_gate=budget, worker_root=ROOT / "workspace" / "workers", graph_dir=ROOT / "graph", reason_model=MODEL, reason_planner_diagnostic={"provider": "zhipu", "model": MODEL}, review_policy={"enabled": True, "max_concurrent": 1}, wall_clock_budget=900.0, barren_limit=6, worker_backend="container", worker_profiles=profiles, llm_profiles={"planner": dict(profiles[0]), "review": dict(profiles[0])}, runtime_profiles=runtime_profiles, credential_accounts_root=str(account_store_root(SESSIONS_ROOT)), runtime_policy=runtime["runtime_policy"], runtime_snapshot=runtime["runtime_snapshot"], pool_manager=runtime["pool_manager"])
        outcome = await swarm.run()
        trace = {"run_id": RUN_ID, "target": TARGET, "provider": "zhipu", "model": MODEL, "solved": bool(outcome.solved), "flags": list(outcome.flags or []), "winner": str(outcome.winner or ""), "event_types": [e.event_type.value for e in events], "events": [{"seq": int(getattr(e, "seq", 0) or 0), "type": e.event_type.value, "solver_id": e.solver_id, "payload": e.payload} for e in events]}
        (ROOT / "blackbox-trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: trace[k] for k in ("run_id", "provider", "model", "solved", "flags", "event_types")}, ensure_ascii=False))
        return 0 if trace["solved"] else 1
    finally:
        await runtime["pool_manager"].close()
        await llm.aclose()
        await review_llm.aclose()

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
