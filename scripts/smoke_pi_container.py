"""P2 acceptance smoke: a REAL swarm run with a pi worker INSIDE the per-category
worker container (ctf-swarm-pi:0.2.0, rcp supervisor backend).

- boots a local HTTP target (bound to 0.0.0.0; the container reaches it via
  host.docker.internal)
- creates a credential account (pi-main/API_KEY) for the container key injection
- runs the dswarm ReasonSwarm scheduler (executor=cli, engines=["pi"],
  worker_backend="container")
- expects: pi worker inside the container curls the target, the flag passes the
  provenance gate, the run finishes solved.
"""
import asyncio
import functools
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, r"C:\Projects\Agent-projects\ctf-swarm")

from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.llm import LLMClient
from dswarm.models.solve_graph import Challenge
from dswarm.sandbox.manager import SandboxManager
from dswarm.solver.result import ArtifactStore
from dswarm.swarm.swarm import Swarm

FLAG = "flag{smoke_pi_container_ok}"
PORT = 18889


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

    # 0.0.0.0: the worker runs in a container and reaches the host via
    # host.docker.internal.
    httpd = _Server(("0.0.0.0", PORT), functools.partial(_Handler, directory=str(root)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


async def main() -> int:
    os.environ.setdefault("DSWARM_DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    os.environ.setdefault("DSWARM_PI_PROVIDER", "deepseek")
    # Docker Desktop: the supervisor's in-container chown over the bind mount can
    # take ~60s before it dials back — allow more than the 40s default.
    os.environ.setdefault("DSWARM_CONTROL_LINK_DEADLINE", "120")
    if not os.environ["DSWARM_DEEPSEEK_API_KEY"]:
        print("FATAL: no DEEPSEEK_API_KEY in environment")
        return 2

    target_root = Path(r"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-container-target")
    target_root.mkdir(parents=True, exist_ok=True)
    start_target(target_root)
    print(f"target up: http://0.0.0.0:{PORT}/ (flag={FLAG})", flush=True)

    # credential account for the container key injection (pi-main/API_KEY)
    # fresh workspace EVERY run: a reused graph.db leaks prior-run facts/sessions
    # into the next run (the smoke's own debug output), and a growing workspace
    # slows the supervisor's in-container chown past the 40s link deadline.
    import shutil as _sh
    root = Path(r"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-container")
    if root.exists():
        _sh.rmtree(root, ignore_errors=True)
    # a killed smoke run (e.g. a hard-interrupted process) leaves the run container
    # RUNNING — ensure_container would reuse it with a STALE token and the new
    # supervisor Hello gets rejected. Remove any leftover run container first.
    import subprocess as _cleanup_sp
    _cleanup_sp.run(["docker", "rm", "-f", "dswarm-run-smoke-pi-container"],
                    capture_output=True, timeout=30)
    accounts = root / "accounts"
    acct = accounts / "pi-main"
    acct.mkdir(parents=True, exist_ok=True)
    (acct / "API_KEY").write_text(os.environ["DEEPSEEK_API_KEY"] + "\n", encoding="utf-8")
    root.mkdir(parents=True, exist_ok=True)
    challenge = Challenge(
        id="smoke-pi-container",
        name="smoke-pi-container",
        category="web",
        points=50,
        description=(
            "A tiny website is running at http://host.docker.internal:%d/. "
            "It contains a flag. Fetch the page and find it." % PORT
        ),
        target=f"http://host.docker.internal:{PORT}/",
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
        challenge,
        llm=llm, sandbox=sandbox, bus=bus, cost=cost, artifacts=arts,
        run_id="smoke-pi-container",
        executor="cli",
        engines=["pi"],
        web_access=True,
        max_workers=2,
        worker_root=root / "workspace" / "workers",
        graph_dir=root / "workspace" / "graph",
        credential_accounts_root=str(accounts),
        worker_backend="container",
        reason_model="deepseek-v4-flash",
        wall_clock_budget=90.0,
        barren_limit=3,
    )
    print("swarm constructed; running (container backend, image=ctf-swarm-pi)...", flush=True)
    # gateway request logging to the smoke stdout (it logs at INFO via logging)
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                         stream=sys.stdout, force=True)
    import subprocess as _sp
    # spy on the rcp exec spec: what argv/env actually goes to the supervisor
    import dswarm.solver.control_client as _cc
    _orig_rcp = _cc.run_cli_streaming_rcp

    def _spy_rcp(driver, argv, **kw):
        # the --provider flag is the WHOLE P3 question: it must say ctf-gateway,
        # not deepseek (a deepseek flag + task-token key = instant 401 + echo).
        prov = ""
        try:
            i = argv.index("--provider")
            prov = argv[i + 1] if i + 1 < len(argv) else ""
        except ValueError:
            pass
        print("RCP argv[0]:", argv[0], "provider:", prov or "(unset)", flush=True)
        print("RCP argv count:", len(argv), "prompt len:", len(argv[-1]) if argv else 0, flush=True)
        print("RCP cwd:", kw.get("container_cwd"), flush=True)
        print("RCP env:", json.dumps(kw.get("env") or {}, ensure_ascii=False)[:600], flush=True)
        # dump the real worker prompts for offline diagnosis (one file per call)
        try:
            import pathlib as _pl
            import time as _t
            _spy_rcp._n = getattr(_spy_rcp, "_n", 0) + 1
            _pl.Path(rf"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-container\worker{_spy_rcp._n}.prompt.txt") \
                .write_text(argv[-1] if argv else "", encoding="utf-8")
        except Exception:
            pass
        try:
            res = _orig_rcp(driver, argv, **kw)
            print("RCP res status:", json.dumps(getattr(res, "runtime_status", {}) or {}, ensure_ascii=False),
                  "text:", repr((res.text or "")[:300]), flush=True)
            # dump the worker's raw output (text + stderr) per call — the
            # authoritative "what did the worker actually produce" record.
            try:
                import pathlib as _pl
                _pl.Path(rf"C:\Projects\Agent-projects\ctf-swarm\sessions\smoke-container\worker{_spy_rcp._n}.result.txt") \
                    .write_text(f"--- text ---\n{res.text or ''}\n--- stderr ---\n{res.raw_stderr or ''}\n",
                                encoding="utf-8")
            except Exception:
                pass
            return res
        except Exception as e:
            import traceback as _tb
            print("RCP EXCEPTION:", type(e).__name__, str(e)[:500], flush=True)
            _tb.print_exc()
            raise

    _cc.run_cli_streaming_rcp = _spy_rcp
    # spy on CliSolver.run for the full worker exception
    import dswarm.solver.cli_solver as _cs
    _orig_solver_run = _cs.CliSolver.run

    def _spy_solver_run(self, *a, **k):
        try:
            return _orig_solver_run(self, *a, **k)
        except Exception as e:
            import traceback as _tb
            print(f"SOLVER EXCEPTION ({getattr(self, 'solver_label', '?')}): "
                  f"{type(e).__name__}: {e}", flush=True)
            _tb.print_exc()
            raise

    _cs.CliSolver.run = _spy_solver_run
    # spy on the container streaming entry (covers pre-rcp steps)
    import dswarm.solver.container_exec as _ce
    _orig_rsc = _ce.run_cli_streaming_container

    def _spy_rsc(driver, argv, **kw):
        print("RSC entry argv0:", argv[0] if argv else None, flush=True)
        try:
            res = _orig_rsc(driver, argv, **kw)
            print("RSC res status:", json.dumps(getattr(res, "runtime_status", {}) or {}, ensure_ascii=False),
                  "text:", repr((res.text or "")[:200]), flush=True)
            return res
        except Exception as e:
            import traceback as _tb
            print("RSC EXCEPTION:", type(e).__name__, str(e)[:400], flush=True)
            _tb.print_exc()
            raise

    _ce.run_cli_streaming_container = _spy_rsc

    def _mid_dump():
        time.sleep(20)
        try:
            r = _sp.run(["docker", "logs", "dswarm-run-smoke-pi-container"],
                        capture_output=True, text=True, timeout=20)
            print("=== MID supervisor logs ===", flush=True)
            print((r.stdout or "")[-3000:], flush=True)
            print((r.stderr or "")[-1500:], flush=True)
            r2 = _sp.run(["docker", "exec", "dswarm-run-smoke-pi-container",
                          "sh", "-c", "ps aux | head -20"],
                         capture_output=True, text=True, timeout=20)
            print("=== MID container ps ===", flush=True)
            print((r2.stdout or "")[-2000:], flush=True)
            # container -> gateway reachability, right from inside the worker container
            r3 = _sp.run(["docker", "exec", "dswarm-run-smoke-pi-container",
                          "sh", "-c",
                          "curl -sS -m 8 http://host.docker.internal:9101/health 2>&1; "
                          "echo; echo RC=$?; "
                          "ls -la /home/kali/workspace/homes/cli-pi/.pi/agent/extensions/ 2>&1"],
                         capture_output=True, text=True, timeout=25)
            print("=== MID gateway probe (in-container) ===", flush=True)
            print((r3.stdout or "")[-1500:], flush=True)
        except Exception as e:
            print("mid dump:", e, flush=True)

    import threading as _th
    _th.Thread(target=_mid_dump, daemon=True).start()
    try:
        outcome = await sw.run()
    finally:
        # diagnostics BEFORE teardown: did the isolated HOME get its links?
        h = root / "workspace" / "homes"
        try:
            print("homes dump:", flush=True)
            if h.exists():
                for p in sorted(h.rglob("*")):
                    print("  ", p.relative_to(h), flush=True)
            else:
                print("  MISSING", flush=True)
            import subprocess as _sp
            r = _sp.run(["docker", "logs", "dswarm-run-smoke-pi-container"],
                        capture_output=True, text=True, timeout=30)
            print("=== supervisor logs ===", flush=True)
            print((r.stdout or "")[-4000:], flush=True)
            print((r.stderr or "")[-2000:], flush=True)
        except Exception as e:
            print("diag:", e, flush=True)
        # teardown the run container whatever happened
        try:
            from dswarm.solver.container_exec import teardown_container
            teardown_container("smoke-pi-container", remove=True)
        except Exception as e:
            print("teardown:", e, flush=True)
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
        try:
            print(" ", k, flush=True)
        except UnicodeEncodeError:
            pass  # non-encodable payload on a GBK console — skip the line

    ok = outcome.solved and FLAG in (outcome.flags or [])
    print("SMOKE_RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
