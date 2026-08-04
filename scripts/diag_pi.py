"""Diagnose pi on this Windows host: spawn `pi --mode rpc`, send a prompt,
drain stdout/stderr for `wait_s`, then kill. Prints what we saw."""
import json
import subprocess
import sys
import threading
import time

WAIT = 45
argv = [r"C:\Program Files\pi-windows-x64\pi.exe", "--mode", "rpc",
        "--no-session", "--no-skills", "--no-extensions", "--no-context-files"]
print("argv:", argv, flush=True)

proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                        errors="replace")

out_lines, err_lines = [], []

def drain(fh, sink):
    try:
        for line in fh:
            sink.append(line)
    except Exception as e:
        sink.append(f"<drain err: {e}>\n")

t_out = threading.Thread(target=drain, args=(proc.stdout, out_lines), daemon=True)
t_err = threading.Thread(target=drain, args=(proc.stderr, err_lines), daemon=True)
t_out.start(); t_err.start()

time.sleep(3)  # let pi boot
try:
    proc.stdin.write(json.dumps({"id": "req-1", "type": "prompt",
                                 "message": "Reply with exactly: OK"}) + "\n")
    proc.stdin.flush()
    print("prompt sent at t=3s", flush=True)
except Exception as e:
    print(f"stdin write failed: {e}", flush=True)

t0 = time.time()
while time.time() - t0 < WAIT:
    time.sleep(1)
    n_out, n_err = len(out_lines), len(err_lines)
    if n_out or n_err:
        print(f"[t={int(time.time()-t0):2d}s] stdout_lines={n_out} stderr_lines={n_err}", flush=True)
        break
else:
    print(f"[t={WAIT}s] NO output at all — pi produced nothing", flush=True)

time.sleep(3)
alive = proc.poll() is None
print("alive after wait:", alive, flush=True)
if alive:
    proc.kill()
    print("killed", flush=True)
else:
    print("exit code:", proc.returncode, flush=True)

time.sleep(1)
print("=== STDOUT ===", flush=True)
sys.stdout.write("".join(out_lines[-40:]))
print("=== STDERR ===", flush=True)
sys.stdout.write("".join(err_lines[-40:]))
