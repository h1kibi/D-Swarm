"""End-to-end: run a REAL pi (deepseek) worker through muteki's
run_cli_streaming + PiDriver path — the exact code path a swarm worker uses."""
import sys
import threading

sys.path.insert(0, r"C:\Projects\Agent-projects\ctf-swarm")

from muteki.solver.cli_driver import PiDriver, run_cli_streaming, StreamStep

d = PiDriver()
print("bin:", d.bin, flush=True)
print("close_stdin:", d.close_stdin, flush=True)

argv = d.build_execute(
    "Reply with exactly: OK. Then stop.", None, web_access=False)
print("argv:", argv, flush=True)

steps: list[StreamStep] = []


def on_step(s: StreamStep) -> None:
    steps.append(s)
    print(f"  step: kind={s.kind} session={s.session} tool={s.tool} "
          f"text={str(s.text)[:80]!r}", flush=True)


res = run_cli_streaming(d, argv, cwd=r"C:\Projects\Agent-projects\ctf-swarm\sessions",
                        timeout=120, on_step=on_step)
print("=== RESULT ===", flush=True)
print("text:", repr(res.text[:200]), flush=True)
print("session:", res.session, flush=True)
print("in_tokens:", res.input_tokens, "out_tokens:", res.output_tokens, flush=True)
print("timed_out:", res.timed_out, "cancelled:", res.cancelled, flush=True)
print("steps:", [(s.kind, s.session) for s in steps], flush=True)

# resume the same session with a follow-up (the conclude-turn path)
if res.session:
    argv2 = d.build_resume("Now reply with exactly: DONE", res.session)
    print("resume argv:", argv2, flush=True)
    res2 = run_cli_streaming(d, argv2, cwd=r"C:\Projects\Agent-projects\ctf-swarm\sessions",
                             timeout=120, on_step=lambda s: None)
    print("resume text:", repr(res2.text[:200]), flush=True)
    print("resume session:", res2.session, flush=True)
