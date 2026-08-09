"""Diagnose the rcp worker-exec chain on this Windows host step by step:
receiver -> supervisor container -> start_worker -> stream frames."""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, r"C:\Projects\Agent-projects\ctf-swarm")

from dswarm.solver.control_receiver import ControlReceiver, DEFAULT_CONTROL_PORT

RUN_ID = "diag-rcp"
WORKSPACE = r"C:\Projects\Agent-projects\ctf-swarm\sessions\diag-rcp\workspace"
ACCOUNTS_HOST = r"C:\Projects\Agent-projects\ctf-swarm\sessions\diag-rcp\accounts"
os.makedirs(os.path.join(WORKSPACE, "workers", "cli-pi"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE, ".dswarm_control"), exist_ok=True)
os.makedirs(os.path.join(ACCOUNTS_HOST, "pi-main"), exist_ok=True)
with open(os.path.join(ACCOUNTS_HOST, "pi-main", "API_KEY"), "w") as f:
    f.write(os.environ.get("DEEPSEEK_API_KEY", "") + "\n")

def step(msg):
    print(f"[diag] {msg}", flush=True)

# 1. start the host receiver
ControlReceiver.instance().start()
step(f"receiver listening (expect port {DEFAULT_CONTROL_PORT})")

# 2. register token + start the supervisor container
import secrets
token = secrets.token_hex(16)
with open(os.path.join(WORKSPACE, ".dswarm_control", "token"), "w") as f:
    f.write(token)
ControlReceiver.instance().expect(RUN_ID, token)
step("token registered; starting supervisor container")

name = "dswarm-run-" + RUN_ID
subprocess.run(["docker", "rm", "-f", name], capture_output=True)
r = subprocess.run([
    "docker", "run", "-d", "--name", name, "--network", "bridge",
    "--add-host", "host.docker.internal:host-gateway",
    "--mount", f"type=bind,source={WORKSPACE},target=/home/kali/workspace",
    "--mount", f"type=bind,source={os.path.join(WORKSPACE, '.dswarm_control')},target=/run/dswarm/control",
    "--mount", f"type=bind,source={ACCOUNTS_HOST},target=/run/dswarm/accounts",
    "ctf-swarm-pi:0.2.0",
    "--connect", f"host.docker.internal:{DEFAULT_CONTROL_PORT}",
    "--run-id", RUN_ID,
], capture_output=True, text=True, timeout=60)
print("docker run rc:", r.returncode, r.stderr.strip()[:300], flush=True)

# 3. wait for the supervisor to dial in
link = ControlReceiver.instance().await_link(RUN_ID, deadline_s=40)
step(f"supervisor link up: alive={link.alive}")

# 4. send a StartWorker with the REAL worker env shape (FILE-based key injection)
import os as _os
spec = {
    "argv": ["pi", "--mode", "json", "--session-dir", ".pi-sessions",
             "--exclude-tools", "WebSearch,WebFetch", "Reply with exactly: OK"],
    "cwd": "/home/kali/workspace/workers/cli-pi",
    "env": {
        "HOME": "/home/kali/workspace/homes/cli-pi",
        "DSWARM_PI_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY_FILE": "/run/dswarm/accounts/pi-main/API_KEY",
    },
    "timeout_sec": 30,
    "tag": "diag",
}
step("sending StartWorker (pi --mode json, FILE key injection)")
t0 = time.time()
try:
    wid, q = link.start_worker(spec, timeout=30)
    step(f"start_worker OK: worker_id={wid} ({(time.time()-t0):.1f}s)")
    frames = []
    while True:
        f = q.get(timeout=15)
        if f is None:
            step("queue closed")
            break
        frames.append(f)
        print("  frame:", json.dumps(f)[:300], flush=True)
        if f.get("t") == "exit":
            break
except Exception as e:
    print(f"[diag] start_worker FAILED: {type(e).__name__}: {e}", flush=True)

# 5. teardown
try:
    link.teardown(timeout=10)
except Exception as e:
    print("teardown:", e, flush=True)
subprocess.run(["docker", "rm", "-f", name], capture_output=True)
print("[diag] done", flush=True)
