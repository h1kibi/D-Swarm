"""P1 acceptance smoke: a REAL swarm run with a pi (deepseek) worker.

- boots a local HTTP target whose index page contains the flag
- runs the muteki coordinator (executor=cli, engines=["pi"])
- expects: worker curls the target, the flag passes the provenance gate,
  the run finishes solved with the flag on the shared graph
"""
import asyncio
import functools
import http.server
import os
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, r"C:\Projects\Agent-projects\ctf-swarm")

from muteki.core.cost import CostController
from muteki.core.event_bus import EventBus
from muteki.core.llm import LLMClient
from muteki.models.solve_graph import Challenge
from muteki.sandbox.manager import SandboxManager
from muteki.solver.result import ArtifactStore
from muteki.swarm.swarm import Swarm

FLAG = "flag{smoke_pi_ok}"
PORT = 18888


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # keep stdout clean
        pass


def start_target(root: Path) -> threading.Thread:
    (root / "index.html").write_text(
        "<html><body><h1>smoke target</h1>"
        "<p>Welcome. The flag is on this page.</p>"
        f"<p>FLAG: {FLAG}</p></body></html>",
        encoding="utf-8",
    )

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    httpd = _Server(("127.0.0.1", PORT), functools.partial(_Handler, directory=str(root)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


async def main() -> int:
    # Reason planner reads MUTEKI_DEEPSEEK_API_KEY; the host has DEEPSEEK_API_KEY.
    os.environ.setdefault("MUTEKI_DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    if not os.environ["MUTEKI_DEEPSEEK_API_KEY"]:
        print("FATAL: no DEEPSEEK_API_KEY in environment")
        return 2

    target_root = Path(r"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-target")
    target_root.mkdir(parents=True, exist_ok=True)
    start_target(target_root)
    print(f"target up: http://127.0.0.1:{PORT}/ (flag={FLAG})", flush=True)

    root = Path(r"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-pi")
    root.mkdir(parents=True, exist_ok=True)
    challenge = Challenge(
        id="smoke-pi",
        name="smoke-pi",
        category="web",
        points=50,
        description=(
            "A tiny website is running at http://127.0.0.1:%d/. "
            "It contains a flag. Fetch the page and find it." % PORT
        ),
        target=f"http://127.0.0.1:{PORT}/",
        flag_format=r"flag\{[^}]+\}",
        expected_flags=1,
    )
    bus = EventBus()
    sink_events: list = []
    bus.add_sink(lambda ev: sink_events.append(ev))

    llm = LLMClient()
    sandbox = SandboxManager(root=root / "sbx")
    arts = ArtifactStore(root=root / "arts")
    cost = CostController()

    sw = Swarm(
        challenge, [],  # lineup unused by the CLI executor
        llm=llm, sandbox=sandbox, bus=bus, cost=cost, artifacts=arts,
        run_id="smoke-pi",
        executor="cli",
        engines=["pi"],
        web_access=True,
        coordinator=True,
        race_scout=False,          # straight to the coordinator loop
        start_workers=1,
        max_workers=2,
        worker_root=root / "workspace" / "workers",
        graph_dir=root / "workspace" / "graph",
        reason_model="deepseek-v4-flash",
        stall_seconds=0.1,
        wall_clock_budget=240.0,
        barren_limit=3,
    )
    print("swarm constructed; running…", flush=True)
    outcome = await sw.run()
    print("=== OUTCOME ===", flush=True)
    print("solved:", outcome.solved, flush=True)
    print("flags:", outcome.flags, flush=True)
    print("winner:", outcome.winner, flush=True)

    kinds = []
    for e in sink_events:
        try:
            kinds.append(e.type.value)
        except Exception:
            kinds.append(str(e))
    print("=== EVENT TYPES ===", flush=True)
    for k in kinds:
        print(" ", k, flush=True)

    ok = outcome.solved and FLAG in (outcome.flags or [])
    print("SMOKE_RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
