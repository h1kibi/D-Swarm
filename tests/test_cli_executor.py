"""CLI worker executor 鈥?driver argv construction, output parsing, flag extraction,
external-USD cost accounting. Pure/unit (no real CLI subprocess, no API key)."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from pathlib import Path

import pytest

from dswarm.core.cost import Budget, CostController
from dswarm.core.events import EventType
from dswarm.models.solve_graph import Challenge
from dswarm.solver import cli_solver
from dswarm.solver import blackboard_skill
from dswarm.solver.cli_driver import (
    CliResult, PiDriver, StreamStep, DRIVERS,
    driver_for, get_driver, _kill_proc_tree,
)
from dswarm.solver.cli_solver import CliSolver
from dswarm.solver.container_exec import CONTAINER_WORKSPACE, ContainerHandle
from dswarm.solver.container_runtime import ContainerRuntimeExecutor


def _CP(rc: int, out: str = "", err: str = "") -> "subprocess.CompletedProcess":
    """Canned CompletedProcess for mocked subprocess.run (no real CLI, no key)."""
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


# 鈹€鈹€ driver argv 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def test_worker_env_maps_blackboard_db_into_container_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    db = workspace / "graph" / "shared_graph.db"
    db.parent.mkdir(parents=True)
    db.write_text("")

    class _Graph:
        db_path = db

    ch = Challenge(
        id="env-map",
        name="env-map",
        category="misc",
        description="path mapping",
        flag_format="flag{...}",
    )
    handle = ContainerHandle(
        run_id="env-map",
        host_workspace=str(workspace),
        container="dswarm-run-env-map",
    )
    solver = CliSolver(
        None,
        ch,
        engine="pi",
        shared_graph=_Graph(),
        container=handle,
        worker_env={"HOME": f"{CONTAINER_WORKSPACE}/workers/_homes/cli-pi"},
    )

    env = solver._worker_env()

    assert env["HOME"] == f"{CONTAINER_WORKSPACE}/workers/_homes/cli-pi"
    assert env["DSWARM_BLACKBOARD_DB"] == f"{CONTAINER_WORKSPACE}/graph/shared_graph.db"
    assert env["DSWARM_CHALLENGE_ID"] == "env-map"


def test_worker_env_prepends_stable_tool_path_before_host_shims(monkeypatch):
    import os as _os
    monkeypatch.setenv(
        "PATH",
        _os.pathsep.join(["/Users/snowywar/.jenv/shims", "/opt/homebrew/bin", "/custom/bin"]),
    )
    ch = Challenge(
        id="env-path",
        name="env-path",
        category="misc",
        description="path stability",
        flag_format="flag{...}",
    )
    solver = CliSolver(None, ch, engine="pi")

    parts = solver._worker_env()["PATH"].split(_os.pathsep)

    assert parts[:4] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    assert parts.index("/usr/bin") < parts.index("/Users/snowywar/.jenv/shims")
    assert parts.count("/opt/homebrew/bin") == 1
    assert "/custom/bin" in parts


def test_worker_env_blackboard_script_points_at_repo_copy_for_source_runs(tmp_path):
    """A source checkout (the test env) resolves the skill to the IN-REPO copy for
    EVERY engine 鈥?no deployed ~/.pi or ~/.agents copy that can drift out of sync
    (run-75378). A container gets a run-local materialized copy, not a stale image copy."""
    ch = Challenge(
        id="env-board",
        name="env-board",
        category="misc",
        description="blackboard env",
        flag_format="flag{...}",
    )

    repo_skill = (
        Path(cli_solver.__file__).resolve().parent.parent.parent
        / "skills" / "dswarm-blackboard" / "blackboard.py"
    )
    assert repo_skill.is_file()  # sanity: we ARE running from a source checkout

    for engine in ("pi",):
        env = CliSolver(None, ch, engine=engine)._worker_env()
        assert env["DSWARM_BLACKBOARD_SCRIPT"] == str(repo_skill)

    handle = ContainerHandle(
        run_id="env-board",
        host_workspace=str(tmp_path),
        container="dswarm-run-env-board",
    )
    cont_env = CliSolver(None, ch, engine="pi", container=handle)._worker_env()
    runtime_script = tmp_path / ".dswarm_runtime" / "dswarm-blackboard" / "blackboard.py"
    assert runtime_script.is_file()
    assert runtime_script.read_bytes() == repo_skill.read_bytes()
    assert cont_env["DSWARM_BLACKBOARD_SCRIPT"] ==         f"{CONTAINER_WORKSPACE}/.dswarm_runtime/dswarm-blackboard/blackboard.py"


def test_worker_env_blackboard_script_falls_back_to_deployed_for_installs(monkeypatch):
    """An installed deployment (no repo skill adjacent to the package) falls back to the
    engine-specific user-scope copy installed by scripts/install_blackboard_skill.sh."""
    ch = Challenge(
        id="env-board-install",
        name="env-board-install",
        category="misc",
        description="blackboard env",
        flag_format="flag{...}",
    )
    # Simulate "no in-repo skill" so the install fallback path is exercised.
    monkeypatch.setattr(blackboard_skill, "_repo_blackboard_script", lambda: None)

    env = CliSolver(None, ch, engine="pi")._worker_env()
    assert env["DSWARM_BLACKBOARD_SCRIPT"].endswith(
        "/.pi/agent/skills/dswarm-blackboard/blackboard.py")


def test_worker_env_exposes_current_intent_id():
    ch = Challenge(
        id="env-intent",
        name="env-intent",
        category="misc",
        description="intent id",
        flag_format="flag{...}",
    )
    solver = CliSolver(
        None,
        ch,
        engine="pi",
        mode="explore",
        intent_goal="probe /admin",
        intent_id="I-admin",
    )

    assert solver._worker_env()["DSWARM_INTENT_ID"] == "I-admin"


# argv[0] is the RESOLVED engine binary (a pinned official path), not the bare
# name 鈥?so assert against d.bin, which is the contract these tests actually mean.
def test_pi_sends_a_real_hello_probe():
    # pi builds a non-empty one-turn json-mode argv carrying the hello prompt
    # (the codex/cursor symmetry fix retired with those engines).
    drv = PiDriver()
    argv = drv._hello_argv()
    assert argv, f"{drv.name} has no hello probe"
    assert drv.HELLO_PROMPT in argv, f"{drv.name} probe omits the hello prompt"


def test_health_detail_retries_once_then_succeeds(monkeypatch):
    # a single transient miss must NOT report red 鈥?retry recovers it.
    d = PiDriver()
    _ = d.bin  # resolve+cache the binary BEFORE we mock run (resolution probes too)
    calls = {"n": 0}

    def fake_run(argv, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _CP(1, "", "rate limit (overloaded)")  # transient
        return _CP(0, '{"type":"agent_settled"}')          # recovered

    monkeypatch.setattr("dswarm.solver.cli_driver.subprocess.run", fake_run)
    monkeypatch.setattr("dswarm.solver.cli_driver.time.sleep", lambda *_: None)
    ok, detail = d.health_detail()
    assert ok is True and detail == ""
    assert calls["n"] == 2  # exactly one retry


def test_health_detail_classifies_persistent_failure(monkeypatch):
    d = PiDriver()
    _ = d.bin  # cache the binary before mocking run

    def fake_run(argv, **kw):
        return _CP(1, "", "Invalid API key (401)")

    monkeypatch.setattr("dswarm.solver.cli_driver.subprocess.run", fake_run)
    monkeypatch.setattr("dswarm.solver.cli_driver.time.sleep", lambda *_: None)
    ok, detail = d.health_detail()
    assert ok is False
    # the real reason is surfaced, NOT a blanket "check login / quota"
    assert "401" in detail and "exited 1" in detail


def test_health_detail_classifies_timeout(monkeypatch):
    d = PiDriver()
    _ = d.bin  # cache the binary before mocking run

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd="x", timeout=60)

    monkeypatch.setattr("dswarm.solver.cli_driver.subprocess.run", fake_run)
    monkeypatch.setattr("dswarm.solver.cli_driver.time.sleep", lambda *_: None)
    ok, detail = d.health_detail()
    assert ok is False and "timed out" in detail


def test_healthcheck_bool_delegates_to_detail(monkeypatch):
    # back-compat: the swarm still calls the bool healthcheck(); it must mirror
    # health_detail()'s verdict.
    d = PiDriver()
    monkeypatch.setattr(d, "health_detail", lambda: (True, ""))
    assert d.healthcheck() is True
    monkeypatch.setattr(d, "health_detail", lambda: (False, "nope"))
    assert d.healthcheck() is False


def test_engine_status_is_cheap_and_does_not_deep_probe(monkeypatch):
    import dswarm.solver.cli_driver as cli_driver

    monkeypatch.setenv("DSWARM_PI_BIN", "/usr/bin/pi")
    monkeypatch.setattr(cli_driver, "_runs_ok", lambda _path: True)

    def fail_deep_probe():
        raise AssertionError("/api/engines must not spend a model turn")

    monkeypatch.setattr(cli_driver.DRIVERS["pi"], "health_detail", fail_deep_probe)

    rows = cli_driver.engine_status(
        profiles=[{
            "id": "pi-main",
            "name": "pi-main",
            "engine": "pi",
            "transport": "pi_cli",
            "model": "deepseek-v4-flash",
        }],
    )

    assert rows == [{
        "engine": "pi",
        "bin": "/usr/bin/pi",
        "available": True,
        "healthy": None,
        "health_detail": "",
        "profile_id": "pi-main",
        "profile_name": "pi-main",
        "model": "deepseek-v4-flash",
        "backend": "local",
    }]


def test_health_detail_falls_back_to_version_when_no_hello(monkeypatch):
    # a hypothetical driver with no cheap dry-run (empty _hello_argv) degrades to
    # the --version liveness check rather than reporting red.
    d = PiDriver()
    _ = d.bin  # cache the binary before mocking run
    monkeypatch.setattr(d, "_hello_argv", lambda: [])

    def fake_run(argv, **kw):
        assert "--version" in argv
        return _CP(0, "pi 0.81.1")

    monkeypatch.setattr("dswarm.solver.cli_driver.subprocess.run", fake_run)
    ok, detail = d.health_detail()
    assert ok is True and detail == ""


def test_pi_execute_argv_single_shot_json():
    d = PiDriver()
    assert d.new_session() is None  # pi assigns its own session
    argv = d.build_execute("DO THE THING", None)
    assert argv[0] == d.bin
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    # worker-scoped session storage (relative 鈫?resolves under the worker cwd)
    assert "--session-dir" in argv
    assert argv[argv.index("--session-dir") + 1] == ".pi-sessions"
    # prompt is a positional arg, last (per docs/json.md: `pi --mode json "..."`)
    assert argv[-1] == "DO THE THING"
    assert "-p" not in argv


def test_pi_execute_provider_flag_from_env(monkeypatch):
    d = PiDriver()
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "anthropic")
    argv = d.build_execute("GO", None)
    assert "--provider" in argv
    assert argv[argv.index("--provider") + 1] == "anthropic"
    monkeypatch.delenv("DSWARM_PI_PROVIDER")
    assert "--provider" not in d.build_execute("GO", None)


def test_pi_offline_denies_web_tools():
    d = PiDriver()
    online = d.build_execute("GO", None, web_access=True)
    assert "--exclude-tools" not in online  # web on by default
    offline = d.build_execute("GO", None, web_access=False)
    assert "--exclude-tools" in offline
    denied = offline[offline.index("--exclude-tools") + 1]
    assert "WebSearch" in denied and "WebFetch" in denied


def test_pi_resume_uses_session_id_or_continue():
    d = PiDriver()
    # known session id 鈫?resume that session
    argv = d.build_resume("CONCLUDE", "019fce1e-44b4-7201-bbe8-9d44d5f61a48")
    assert argv[0] == d.bin and "--session" in argv
    assert argv[argv.index("--session") + 1] == "019fce1e-44b4-7201-bbe8-9d44d5f61a48"
    assert argv[-1] == "CONCLUDE"
    # unknown session 鈫?continue the worker's most recent session
    argv2 = d.build_resume("CONCLUDE", "")
    assert "-c" in argv2
    assert "--session" not in argv2


def test_pi_parse_recovers_session_id():
    d = PiDriver()
    out = "\n".join([
        '{"type":"session","version":3,"id":"019fce1e-44b4-7201-bbe8-9d44d5f61a48","timestamp":"2026-08-04T18:51:58.004Z"}',
        '{"type":"message_end","message":{"role":"assistant","text":"hi"}}',
        '{"type":"agent_settled"}',
    ])
    res = d.parse(out, "")
    assert res.session == "019fce1e-44b4-7201-bbe8-9d44d5f61a48"
    # the session event also surfaces as a live StreamStep
    steps = d.parse_stream_steps('{"type":"session","version":3,"id":"sess-abc"}')
    assert steps == [StreamStep("session", session="sess-abc")]


def test_pi_parse_accumulates_assistant_text_and_usage():
    d = PiDriver()
    out = "\n".join([
        '{"type":"turn_start"}',
        '{"type":"tool_execution_start","toolCallId":"c1","toolName":"bash","args":{"command":"ls -la"}}',
        '{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash","result":{"content":[{"type":"text","text":"total 48\\nflag.txt"}]},"isError":false}',
        '{"type":"message","message":{"role":"assistant","text":"plain message event"}}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"found it"},{"type":"text","text":"FOUND_FLAG=flag{abc}"}]}}',
        '{"type":"turn_end","message":{"role":"assistant","text":"wrapping up"},"toolResults":[]}',
        '{"type":"agent_end","messages":[{"role":"assistant","text":"final answer FOUND_FLAG=flag{abc}"}]}',
        '{"type":"agent_settled"}',
    ])
    res = d.parse(out, "")
    assert "plain message event" in res.text
    assert "found it" in res.text
    assert "final answer" in res.text
    assert "FOUND_FLAG=flag{abc}" in res.text


def test_pi_parse_recovers_usage_from_events():
    d = PiDriver()
    out = "\n".join([
        '{"type":"message_end","message":{"role":"assistant","text":"hi"},"usage":{"input_tokens":11,"output_tokens":7}}',
        '{"type":"agent_settled"}',
    ])
    res = d.parse(out, "")
    assert res.input_tokens == 11 and res.output_tokens == 7


def test_pi_parse_falls_back_to_raw_stdout():
    d = PiDriver()
    res = d.parse("pi: something weird happened\n", "stderr line")
    assert "weird" in res.text and "stderr line" in res.raw_stderr


def test_pi_parse_stream_steps_shapes():
    d = PiDriver()
    # tool start 鈫?tool step with the command
    steps = d.parse_stream_steps('{"type":"tool_execution_start","toolCallId":"c1","toolName":"bash","args":{"command":"nmap -p 80 x"}}')
    assert steps == [StreamStep("tool", tool="bash", text="nmap -p 80 x")]
    # tool end 鈫?tool_result step with raw = full output for the provenance gate
    steps = d.parse_stream_steps('{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash","result":{"content":[{"type":"text","text":"PORT STATE\\n80 open"}]},"isError":false}')
    assert len(steps) == 1 and steps[0].kind == "tool_result"
    assert steps[0].raw == "PORT STATE\n80 open" and steps[0].text == "PORT STATE\n80 open"
    # message_end 鈫?one reasoning step per complete message
    steps = d.parse_stream_steps('{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"step one"}]}}')
    assert steps == [StreamStep("reasoning", text="step one")]
    # some pi json-mode versions only emit `message` events
    steps = d.parse_stream_steps('{"type":"message","message":{"role":"assistant","text":"step one"}}')
    assert steps == [StreamStep("reasoning", text="step one")]
    # message_update deltas are NOT surfaced (would flood the deck)
    assert d.parse_stream_steps('{"type":"message_update","message":{},"assistantMessageEvent":{"type":"text_delta","delta":"x"}}') == []
    # non-JSON tolerated
    assert d.parse_stream_steps("not json") == []


def test_is_stream_delta_filters_pi_protocol_lines_but_keeps_prose():
    # run-3154 seq 83: a trailing pi RPC stream delta was recorded as a bogus fact.
    # run-3155 seq 7: an `agent_end` with EMPTY messages/content was recorded too.
    # Protocol envelopes are NOT worker prose and must be filtered out of the
    # end-of-run summary.
    for delta in [
        '{"type":"session","version":3,"id":"sess-1"}',
        '{"type":"agent_start"}',
        '{"type":"turn_start"}',
        '{"type":"message_start","message":{"role":"user","content":[]}}',
        '{"type":"message_update","message":{},"assistantMessageEvent":{"type":"text_delta","delta":"x"}}',
        '{"type":"toolcall_delta","toolCallId":"c1","delta":{"type":"text_delta","text":"curl"}}',
        '{"type":"tool_execution_start","toolCallId":"c1","toolName":"bash","args":{"command":"ls"}}',
        '{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash","result":{"content":[{"type":"text","text":"real output"}]}}',
        '{"type":"agent_settled"}',
        # content-bearing types with NO actual prose are still protocol artifacts.
        '{"type":"message_end","message":{"role":"assistant","content":[]}}',
        '{"type":"turn_end","message":{"role":"assistant","content":[]}}',
        '{"type":"agent_end","messages":[{"role":"assistant","content":[]}]}',
        '{"type":"agent_end","messages":[]}',
    ]:
        assert cli_solver._is_stream_delta(delta), delta
    # content-bearing events carry the assistant's actual text and are kept.
    for prose in [
        '{"type":"message","message":{"role":"assistant","text":"done"}}',
        '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}',
        '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}',
        '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"done"}]}]}',
    ]:
        assert not cli_solver._is_stream_delta(prose), prose
    # plain worker prose and non-JSON lines are never deltas.
    assert not cli_solver._is_stream_delta("flag{a1b2c3}")
    assert not cli_solver._is_stream_delta("VERIFIED_FACT=admin password is x")
    assert not cli_solver._is_stream_delta("")


def test_pi_hello_ok_accepts_completed_turn():
    d = PiDriver()
    assert d._hello_ok(_CP(0, '{"type":"agent_end","messages":[]}\n{"type":"agent_settled"}')) is True
    assert d._hello_ok(_CP(0, '{"type":"agent_start"}')) is False  # started but never finished
    assert d._hello_ok(_CP(0, "no events")) is False


# 鈹€鈹€ offline / web-access toggle (clean eval hygiene) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_registry():
    assert set(DRIVERS) == {"pi"}
    assert get_driver("pi").name == "pi"


@pytest.mark.posix
def test_kill_proc_tree_kills_setsid_escaped_orphan_and_reaps():
    """A worker child that setsid()'s out of the process group must STILL be
    killed (killpg alone misses it 鈫?orphan leaks a slot/CPU/port), and the
    parent must be reaped (no <defunct> zombie). Regression for the live-only
    worker-process leak seen in the run-0011 transcript."""
    import os, subprocess, sys, time
    # parent (own session/group) spawns a setsid'd child that writes its pid and sleeps.
    parent_src = (
        "import os, time, subprocess\n"
        "c = subprocess.Popen(['python3','-c',"
        "\"import os,time;open('%s','w').write(str(os.getpid()));time.sleep(120)\"],"
        " start_new_session=True)\n"
        "time.sleep(120)\n"
    )
    cpid_file = "/tmp/_kt_test_child_%d.pid" % os.getpid()
    try:
        os.unlink(cpid_file)
    except OSError:
        pass
    proc = subprocess.Popen([sys.executable, "-c", parent_src % cpid_file],
                            start_new_session=True)
    # wait for the child to register its pid
    cpid = None
    for _ in range(40):
        try:
            cpid = int(open(cpid_file).read())
            break
        except (OSError, ValueError):
            time.sleep(0.1)
    assert cpid is not None, "setsid child never started"
    assert os.getpgid(cpid) != os.getpgid(proc.pid), "child did not escape the group"

    _kill_proc_tree(proc)

    def _alive(pid):
        try:
            os.kill(pid, 0); return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    # the escaped orphan must be dead
    time.sleep(0.5)
    assert not _alive(cpid), "setsid-escaped orphan survived _kill_proc_tree"
    # the parent must be reaped (poll() returns a code, not None 鈫?not a zombie)
    assert proc.poll() is not None, "parent not reaped (zombie)"
    try:
        os.unlink(cpid_file)
    except OSError:
        pass


def test_driver_for_resolves_profile_id_to_base_engine():
    """A bare profile id string ("pi-sub-container") must resolve to its base
    engine driver, NOT KeyError on DRIVERS[id]. Regression: local runs crashed
    because the engine roster holds profile ids, and driver_for(<id-string>) used
    to index DRIVERS directly."""
    assert driver_for("pi-sub-container").name == "pi"
    assert driver_for("pi-api-container").name == "pi"
    # base engine names and transports still resolve
    assert driver_for("pi").name == "pi"
    assert driver_for("pi_cli").name == "pi"
    # a profile DICT still resolves via its transport/engine
    assert driver_for({"id": "pi-sub-container", "engine": "pi",
                       "transport": "pi_cli"}).name == "pi"


def test_driver_for_local_profile_injects_selected_model(monkeypatch):
    """A subscription/local worker profile is the scheduling unit. Its selected
    model must be used by the health probe and worker argv; otherwise an exhausted
    default model can falsely degrade the pi worker even when the profile's
    selected model is available."""
    monkeypatch.setenv("DSWARM_PI_BIN", "/usr/bin/pi")
    drv = driver_for({
        "id": "pi-sub-container",
        "name": "pi-sub-container",
        "engine": "pi",
        "transport": "pi_cli",
        "credential_mode": "subscription",
        "credential_account": "",
        "runtime": "local",
        "model": "deepseek-v4-flash",
    })

    hello = drv._hello_argv()
    execute = drv.build_execute("PROMPT", drv.new_session())

    assert hello[hello.index("--model") + 1] == "deepseek-v4-flash"
    assert execute[execute.index("--model") + 1] == "deepseek-v4-flash"


def test_get_driver_unknown_name_raises_clear_error():
    """An unresolvable engine name gives an actionable ValueError, not a bare
    KeyError, so the failure points at the profile-id-vs-base-engine confusion."""
    import pytest
    with pytest.raises(ValueError, match="unknown engine"):
        get_driver("totally-not-an-engine")


def test_endpoint_healthcheck_resolves_file_backed_key(monkeypatch, tmp_path):
    """A file-backed key must reach the shared HTTP endpoint probe."""
    import dswarm.solver.cli_driver as cli_driver

    seen = []

    def fake_probe_endpoint(profile, *, api_key, validate_model=False, **kwargs):
        seen.append({
            "profile": profile,
            "api_key": api_key,
            "validate_model": validate_model,
        })
        return {"ok": True, "detail": "模型验证成功"}

    monkeypatch.setattr(cli_driver, "probe_endpoint", fake_probe_endpoint)

    # (a) explicit file: ref
    keyfile = tmp_path / "API_KEY"
    keyfile.write_text("file-secret-123\n")
    drv = driver_for({
        "name": "pi-api", "engine": "pi", "transport": "pi_cli",
        "credential_mode": "api", "base_url": "https://ds.example",
        "api_key_ref": f"file:{keyfile}",
    })
    assert drv.healthcheck() is True
    assert seen[-1]["api_key"] == "file-secret-123"
    assert seen[-1]["validate_model"] is True

    # (b) no ref, but the credential-injection *_API_KEY_FILE env is set.
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(keyfile))
    drv2 = driver_for({
        "name": "ds2", "engine": "pi", "transport": "pi_cli",
        "credential_mode": "api", "base_url": "https://ds.example",
    })
    assert drv2.healthcheck() is True
    assert seen[-1]["api_key"] == "file-secret-123"
    assert seen[-1]["validate_model"] is True


# 鈹€鈹€ engine binary resolution (pin official, skip broken third-party) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# A broken `@cometix/claude-code` repackage earlier on PATH crashes at load and
# would silently degrade the swarm; the resolver must skip it and pin a runnable
# official binary. These tests drive the resolver with fakes so they don't depend
# on what's actually installed on the host.

def test_resolve_prefers_env_override(monkeypatch):
    from dswarm.solver import cli_driver as mod
    monkeypatch.setenv("DSWARM_PI_BIN", "/custom/path/pi")
    # env override wins outright 鈥?no PATH scan, no run probe
    assert mod.resolve_engine_bin("pi") == "/custom/path/pi"


def test_resolve_skips_known_bad_repackage(monkeypatch):
    from dswarm.solver import cli_driver as mod
    monkeypatch.delenv("DSWARM_PI_BIN", raising=False)
    # no known-good location exists in this fake world
    monkeypatch.setattr(mod, "_KNOWN_GOOD", {"pi": []})
    # PATH has the broken repackage first, then a good one
    bad = "/n/node_modules/@cometix/repackage/pi.js"
    good = "/opt/official/pi"
    monkeypatch.setattr(mod, "_which_all", lambda name: [bad, good])
    # cometix realpath looks bad; the good one runs
    monkeypatch.setattr(mod, "_runs_ok", lambda p: p == good)
    assert mod.resolve_engine_bin("pi") == good


def test_resolve_known_good_location_wins_over_path(monkeypatch):
    from dswarm.solver import cli_driver as mod
    monkeypatch.delenv("DSWARM_PI_BIN", raising=False)
    good = "/blessed/pi"
    monkeypatch.setattr(mod, "_KNOWN_GOOD", {"pi": [good]})
    # WindowsPath normalizes separators (str() renders `\blessed\pi`), so
    # compare on the normalized form.
    monkeypatch.setattr(mod.Path, "exists",
                        lambda self: str(self).replace("\\", "/") == good)
    monkeypatch.setattr(mod, "_looks_bad", lambda p: False)
    monkeypatch.setattr(mod, "_runs_ok", lambda p: True)
    # PATH scan would return something else, but the known-good location is checked first
    monkeypatch.setattr(mod, "_which_all", lambda name: ["/somewhere/else/pi"])
    assert mod.resolve_engine_bin("pi") == good


def test_resolve_falls_back_to_bare_name_when_all_broken(monkeypatch):
    from dswarm.solver import cli_driver as mod
    monkeypatch.delenv("DSWARM_PI_BIN", raising=False)
    monkeypatch.setattr(mod, "_KNOWN_GOOD", {"pi": []})
    monkeypatch.setattr(mod, "_which_all", lambda name: ["/bad/pi"])
    monkeypatch.setattr(mod, "_runs_ok", lambda p: False)  # nothing runs
    # last resort: the bare name (preserves old behavior, no worse than before)
    assert mod.resolve_engine_bin("pi") == "pi"


def test_looks_bad_flags_cometix():
    from dswarm.solver.cli_driver import _looks_bad
    assert _looks_bad("/x/node_modules/@cometix/repackage/cli.js") is True
    assert _looks_bad("/opt/homebrew/bin/pi") is False


def test_driver_bin_is_cached(monkeypatch):
    from dswarm.solver import cli_driver as mod
    calls = []
    monkeypatch.setattr(mod, "resolve_engine_bin",
                        lambda name: calls.append(name) or f"/resolved/{name}")
    d = PiDriver()
    d._bin = None  # ensure a clean resolve
    assert d.bin == "/resolved/pi"
    assert d.bin == "/resolved/pi"  # second access
    assert calls == ["pi"]  # resolved exactly once, then cached


# 鈹€鈹€ output parsing 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _cli_solver(challenge, **kw):
    spec = type("S", (), {"solver_id": "cli-1"})()
    return CliSolver(spec, challenge, **kw)


def test_cli_solver_offline_flag_threads_through():
    # web_access=False on the solver 鈫?the built execute argv denies web tools.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = _cli_solver(ch, web_access=False, kb=False)
    assert s.web_access is False
    argv = s.driver.build_execute(s._build_prompt(), s.driver.new_session(),
                                  web_access=s.web_access, kb_access=s.kb)
    assert "--exclude-tools" in argv
    assert argv[argv.index("--exclude-tools") + 1] == "WebSearch,WebFetch"


def test_cli_solver_kb_off_by_default_when_no_kb_configured():
    # Out of the box (no DSWARM_KB_MCP_NAME) the KB is inert regardless of kb=...:
    # self.kb is False and the prompt teaches no KB tool.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=True, engine="pi")
    assert s.kb is False  # no KB configured 鈫?off even though kb=True was requested
    assert "knowledge-base tool" not in s._build_prompt()


def test_cli_solver_kb_disabled_keeps_prompt_clean():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    assert s.kb is False
    assert "knowledge-base tool" not in s._build_prompt()


def test_ctf_web_bootstrap_prompt_injects_web_first_workflow():
    ch = Challenge(
        id="web-focus",
        name="web-focus",
        category="web",
        target="http://challenge.local/",
        description="Find the flag in the web app",
        flag_format=r"flag\{.*?\}",
    )
    s = _cli_solver(ch, kb=False)

    prompt = s._build_prompt()

    assert "## CTF web focus / workflow" in prompt
    assert "supplied HTTP(S) target" in prompt
    assert "Do NOT spend time on broad host/port scans" in prompt
    assert "SSH/SMB/Redis/MySQL/RPC" in prompt
    assert "robots.txt" in prompt and "JS bundles" in prompt
    assert "Probe web bug classes first" in prompt


def test_ctf_web_focus_prompt_applies_to_explore_and_recon_scope():
    ch = Challenge(id="web-focus", name="web-focus", category="web",
                   target="http://challenge.local/", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)

    s.mode = "explore"
    s.intent_goal = "Map hidden API routes"
    explore_prompt = s._build_explore_prompt()
    assert "## CTF web focus / workflow" in explore_prompt
    assert "Explore ONLY this direction" in explore_prompt

    s.mode = "recon"
    recon_prompt = s._build_prompt()
    assert "CTF scope: do NOT run nmap" in recon_prompt
    assert "Only inspect the supplied URL/port and the web application directly" in recon_prompt
    assert "use PORT_OPEN only for an explicitly authorized, relevant targeted check" in recon_prompt


def test_ctf_web_focus_prompt_not_injected_for_non_web_or_pentest():
    crypto = Challenge(id="crypto", name="crypto", category="crypto",
                       flag_format=r"flag\{.*?\}")
    assert "## CTF web focus / workflow" not in _cli_solver(crypto, kb=False)._build_prompt()

    pentest = Challenge(id="pt", name="pt", category="web", mode="pentest",
                        goal="Assess the web app", flag_format=r"flag\{.*?\}")
    assert "## CTF web focus / workflow" not in _cli_solver(pentest, kb=False)._build_prompt()


def test_direction_prompt_block_injected_when_env_set(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Direction: PWN. Use pwntools.", encoding="utf-8")
    monkeypatch.setenv("DSWARM_DIRECTION_PROMPT", str(prompt))
    ch = Challenge(id="pwn", name="pwn", category="pwn",
                   flag_format=r"flag\{.*?\}")
    built = _cli_solver(ch, kb=False)._build_prompt()
    assert "## Direction tool & environment briefing" in built
    assert "pwntools" in built


def test_direction_prompt_block_empty_when_unset(monkeypatch):
    monkeypatch.delenv("DSWARM_DIRECTION_PROMPT", raising=False)
    ch = Challenge(id="web", name="web", category="web",
                   flag_format=r"flag\{.*?\}")
    built = _cli_solver(ch, kb=False)._build_prompt()
    assert "Direction tool & environment briefing" not in built


def test_explore_prompt_injects_direction_block(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Direction: PWN. Use pwntools and gdb.", encoding="utf-8")
    monkeypatch.setenv("DSWARM_DIRECTION_PROMPT", str(prompt))
    ch = Challenge(id="pwn", name="pwn", category="pwn",
                   flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    s.mode = "explore"
    s.intent_goal = "Leak the canary"
    built = s._build_explore_prompt()
    assert "## Direction tool & environment briefing" in built
    assert "pwntools" in built


def test_web_direction_prompt_documents_no_non_web_port_drift():
    prompt = (
        Path(__file__).resolve().parents[1]
        / "docker" / "worker-pi" / "directions" / "web" / "prompt.md"
    )
    text = prompt.read_text(encoding="utf-8")
    assert "precise single-request probes over noisy full scans" in text
    assert "port/service enumeration scoped to what the challenge text" in text


def test_extract_flag_prefers_found_flag_marker():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    text = "lots of noise\nFOUND_FLAG=flag{real_one}\nmore noise"
    assert s._extract_flag(text) == "flag{real_one}"


def test_extract_flag_ignores_none_marker():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    assert s._extract_flag("FOUND_FLAG=NONE\nno flag here") is None


def test_extract_flag_does_NOT_blind_scan_prose():
    # POLICY CHANGE (run-4305): we no longer blind-scan the transcript for a
    # flag_format-shaped token. A flag mentioned only in prose, with no FOUND_FLAG=
    # marker, is NOT a claim 鈥?extracting it was the source of every false positive
    # (run-1619/run-3613/run-4305). The flag is the worker's explicit claim only.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    assert s._extract_flag("the answer is flag{from_body} somewhere") is None
    # only an explicit marker counts:
    assert s._extract_flag("FOUND_FLAG=flag{from_body}") == "flag{from_body}"


def test_extract_flag_ignores_prose_sentence_run_4305():
    # the exact run-4305 shape: a reasoning sentence whose greedy {鈥 the old scan
    # scooped up as a flag. No marker 鈫?None.
    ch = Challenge(id="t", name="t", category="crypto",
                   flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
    s = _cli_solver(ch)
    prose = ("The output starts with `flag{`, which means the plaintext may be "
             "further transformed after Avali Scratch, not just `flag{...}` directly.")
    assert s._extract_flag(prose) is None


def test_gate_rejects_flag_not_in_output():
    # provenance: a flag the worker only NAMES but isn't in its real output fails.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    assert s._flag_ok("flag{hallucinated}", "the worker rambled, no flag in output") is False
    assert s._flag_ok("flag{seen}", "stdout had flag{seen} in it") is True


def test_launder_still_rejects_grep_from_disk_steal():
    """#3: the run-11551 grep-from-disk launder MUST stay rejected. The worker reads
    another run's persisted log/winner and restates the flag as its own 鈥?a READ action
    next to an internal-storage filename, scanned over the whole output (the restate can
    be arbitrarily far from the read)."""
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    steal = ("$ grep -r flag /workspace/eval_runs/run-11550/winner.json\n"
             "...much later...\n"
             "I recovered the flag: flag{stolen}\n")
    assert s._flag_ok("flag{stolen}", steal) is False
    # the engine's own history dir is an unambiguous tell on its own (no read verb needed)
    assert s._flag_ok("flag{x}", "found it in ~/.pi/sessions/abc flag{x}") is False
    # a sibling-process harvest phrasing
    assert s._flag_ok("flag{y}", "pulled flag{y} from a teammate process title") is False


def test_launder_does_not_reject_genuine_flag_mentioning_path():
    """#3: a GENUINE flag whose transcript merely MENTIONS an internal-storage path
    (a target that serves /winner.json, a forensics blob containing the string) must
    NOT be rejected. The old context-free match false-rejected these (Rivulet-class:
    real flags refused, operator stuck at a false 1/4)."""
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    # target serves a path literally named winner.json 鈥?no read action by the worker
    served = ("GET /winner.json HTTP/1.1 -> 200\n"
              "the response body contained flag{real_recovered}\n")
    assert s._flag_ok("flag{real_recovered}", served) is True
    # a forensics challenge whose artifact string mentions shared_graph.db, flag found
    forensic = ("strings dump mentions a file named shared_graph.db in the pcap\n"
                "but the actual flag decoded from the payload is flag{from_pcap}\n")
    # NOTE: this transcript DOES contain a read verb ("strings") 鈥?but it's reading the
    # CHALLENGE artifact, not internal storage. This is the residual edge the report
    # flags as acceptable (a read verb + an internal filename mention together is rare
    # in a genuine solve); we keep the conservative reject here to preserve the
    # run-11551 catch. Document the trade-off rather than weaken the steal defense.
    assert s._flag_ok("flag{from_pcap}", forensic) is False


# 鈹€鈹€ multi-flag worker layer (Phase 2) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_extract_flags_all_markers_deduped():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    txt = ("FOUND_FLAG=flag{a}\nnoise\nFOUND_FLAG=flag{b}\n"
           "FOUND_FLAG=flag{a}\nFOUND_FLAG=NONE\n")
    assert s._extract_flags(txt) == ["flag{a}", "flag{b}"]  # order kept, dedup, NONE skip
    # _extract_flag (single, back-compat) still returns the LAST marker
    assert s._extract_flag(txt) == "flag{a}"


def test_extract_flags_empty_when_no_markers():
    s = _cli_solver(Challenge(id="t", name="t", category="web"))
    assert s._extract_flags("prose mentioning flag{...} but no marker") == []


def test_accept_flag_dedups_against_already_found():
    s = _cli_solver(Challenge(id="t", name="t", category="web"))
    # first accept is new; the same flag again is a no-op (no double broadcast)
    assert asyncio.run(s._accept_flag("flag{a}")) is True
    assert asyncio.run(s._accept_flag("flag{a}")) is False
    assert asyncio.run(s._accept_flag("flag{b}")) is True
    assert s.graph.flags == ["flag{a}", "flag{b}"] and s.graph.flag == "flag{a}"
    assert s._already_found == {"flag{a}", "flag{b}"}


# 鈹€鈹€ flag provenance gate (run-75379) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Three regressions: (a) a reasoning-only FOUND_FLAG that never appears in any tool
# output is rejected; (b) a flag past char 600 of REAL command output is still
# accepted (the gate sees the untruncated raw, not the deck-truncated chunk); (c) a
# flag in a nested-ssh remote stdout is accepted when that stdout is captured.

def _flag_solver():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    return _cli_solver(ch, bus=_CaptureBus())


def test_reasoning_only_flag_is_rejected_run75379():
    """(a) The exact run-75379 BUG鈶? a worker restates `FOUND_FLAG=flag{x}` in its
    REASONING (its own claim), and that value appears in NO tool output. The old live
    path passed the reasoning chunk to _stream_markers and gated the flag against the
    SAME chunk (`flag in raw_output` where raw_output IS the claim) 鈫?trivially true 鈫?
    hallucinated flag laundered through prose. Now reasoning can't source a flag."""
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    hallucination = ("I confirmed from the real output 2 flags. "
                     "FOUND_FLAG=flag{090099b7-e350-424a-9d68-b5310495403e}")
    asyncio.run(s._emit_step(StreamStep("reasoning", text=hallucination)))
    # NOT accepted: a reasoning chunk is never a flag source.
    assert s._already_found == set()
    assert s._stream_accepted == []
    # and the raw-output corpus stayed empty (reasoning is not command output).
    assert s._raw_tool_outputs == []


def test_tool_result_flag_in_real_output_is_accepted_run75379():
    """The legitimate counterpart to (a): the SAME flag, when it appears in real
    command output (a tool_result), IS accepted 鈥?provenance traces to evidence."""
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    real = "root@dc:~# type flag.txt\nFOUND_FLAG=flag{real-from-output}\n"
    asyncio.run(s._emit_step(StreamStep("tool_result", text=real, raw=real)))
    assert s._already_found == {"flag{real-from-output}"}
    assert s._stream_accepted == ["flag{real-from-output}"]


def test_tool_result_bare_flag_without_marker_is_accepted():
    """The run-0863 shape: pi prints the flag directly at the end of curl output
    without a FOUND_FLAG= marker. The live raw-output seam must gate and accept it,
    instead of only showing 'live flag candidate in real output'."""
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    real = "=== PAYLOAD ===\n...source dump...\nflag{ac1c0b43599e91b7af7e56f5ef2f05aa}\n"
    asyncio.run(s._emit_step(StreamStep("tool_result", text=real, raw=real)))
    assert s._already_found == {"flag{ac1c0b43599e91b7af7e56f5ef2f05aa}"}
    assert s._stream_accepted == ["flag{ac1c0b43599e91b7af7e56f5ef2f05aa}"]


def test_tool_result_css_brace_is_not_accepted_as_bare_flag():
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    css = '<style>#app{position:fixed;top:0;left:0;width:100%;bottom:50px;overflow-y:auto;}</style>\n'
    asyncio.run(s._emit_step(StreamStep("tool_result", text=css, raw=css)))
    assert s._already_found == set()
    assert s._stream_accepted == []
    assert not any(
        "live flag candidate" in str(e.payload.get("text", ""))
        for e in s.bus.events
    )


def test_tool_result_javascript_brace_is_not_reported_as_flag_candidate():
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    js = "else{messageDiv.appendChild(avatarDiv);messageDiv.appendChild(contentDiv);}\n"
    asyncio.run(s._emit_step(StreamStep("tool_result", text=js, raw=js)))
    assert s._already_found == set()
    assert s._stream_accepted == []
    assert not any(
        "live flag candidate" in str(e.payload.get("text", ""))
        for e in s.bus.events
    )


def test_flag_past_char_600_still_accepted_via_untruncated_raw_run75379():
    """(b) the stream driver's hidden killer: the live tool_result chunk is
    truncated to 600 chars. A flag that appears PAST char 600 of a command's output is
    absent from the truncated `text`, but the gate must see the full `raw`. Without the
    raw-output gate this real flag would be silently dropped."""
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    flag = "flag{past-the-600-char-cutoff}"
    # mimic exactly what the drivers now produce: text truncated to 600, raw full.
    full = ("A" * 900) + f"\nFOUND_FLAG={flag}\n"
    step = StreamStep("tool_result", text=full[:600], raw=full)
    assert flag not in step.text          # truncated chunk genuinely lacks the flag
    assert flag in step.raw               # but the raw output carries it
    asyncio.run(s._emit_step(step))
    assert s._already_found == {flag}     # accepted because the gate saw raw


def test_nested_ssh_remote_stdout_flag_accepted_when_captured_run75379():
    """(c) Nested `ssh root@VPS 'cat flag.txt'`: the remote flag is in the REMOTE
    stdout, which the outer ssh forwards into the local tool output. When that output
    is captured (StreamStep.raw), the flag is gateable and accepted 鈥?the run-75379
    flag04 false-negative is fixed."""
    from dswarm.solver.cli_driver import StreamStep
    s = _flag_solver()
    flag = "flag{ebca91d7-from-pivoted-dc}"
    # the outer ssh command's captured output = the remote host's stdout.
    remote = (f"root@workstation:~# ssh root@10.0.0.6 'cat /root/flag.txt'\n"
              f"{flag}\nFOUND_FLAG={flag}\n")
    asyncio.run(s._emit_step(StreamStep("tool_result", text=remote, raw=remote)))
    assert s._already_found == {flag}


def test_stream_markers_allow_flags_false_blocks_flag_but_keeps_facts():
    """Unit: allow_flags=False (the reasoning path) extracts facts/dead-ends but NEVER
    a flag, even if the chunk literally contains FOUND_FLAG= with the value present."""
    s = _flag_solver()
    chunk = "FOUND_FLAG=flag{should-not-take}\nVERIFIED_FACT=port 8080 is open\n"
    asyncio.run(s._stream_markers(chunk, allow_flags=False))
    assert s._already_found == set()           # flag blocked
    # the fact still went out
    assert any(e.payload.get("kind") == "fact_added"
               for e in s.bus.events if e.event_type is EventType.BLACKBOARD_DELTA)


def test_stream_markers_extracts_and_gates_from_flag_provenance():
    """Unit: when flag_provenance is given, flags are BOTH extracted from and gated
    against THAT corpus, not the (possibly truncated) display chunk. The FOUND_FLAG
    marker can sit past char 600 of `text`, so reading it out of `text` would miss it
    entirely 鈥?it must come from the raw provenance."""
    s = _flag_solver()
    # the display chunk has no marker at all; the raw provenance carries the real one
    # (e.g. the marker landed past the 600-char truncation point).
    display = "...output truncated for the deck..."
    raw = "the command printed FOUND_FLAG=flag{from-raw} to stdout\n"
    asyncio.run(s._stream_markers(display, flag_provenance=raw))
    assert s._already_found == {"flag{from-raw}"}

    # and a marker whose value is corroborated by a launder signature is rejected: a
    # FOUND_FLAG= present in prose next to a read-from-disk steal.
    s2 = _flag_solver()
    steal = ("$ grep -r flag /workspace/eval_runs/run-1/winner.json\n"
             "FOUND_FLAG=flag{stolen}\n")
    asyncio.run(s2._stream_markers(steal, flag_provenance=steal))
    assert s2._already_found == set()   # _flag_ok launder gate rejects the disk-steal


def test_surface_unverified_flags_emits_for_untraceable_claim_run75379():
    """A FOUND_FLAG the worker CLAIMED that traces to NO captured output is surfaced to
    the operator as `flag_unverified` (not silently dropped, not auto-solved) 鈥?the
    nested-ssh false-negative guard."""
    s = _flag_solver()
    transcript = "FOUND_FLAG=flag{claimed-no-trace}\nI read it on the DC.\n"
    asyncio.run(s._surface_unverified_flags(transcript))
    unv = [e for e in s.bus.events
           if e.event_type is EventType.BLACKBOARD_DELTA
           and e.payload.get("kind") == "flag_unverified"]
    assert len(unv) == 1
    assert unv[0].payload.get("flag") == "flag{claimed-no-trace}"
    assert unv[0].payload.get("reason")   # operator-facing reason present
    # an ACCEPTED flag is verified, not unverified 鈥?no event for it.
    s2 = _flag_solver()
    asyncio.run(s2._accept_flag("flag{accepted}"))
    asyncio.run(s2._surface_unverified_flags("FOUND_FLAG=flag{accepted}\n"))
    assert not [e for e in s2.bus.events
                if e.event_type is EventType.BLACKBOARD_DELTA
                and e.payload.get("kind") == "flag_unverified"]


def test_persist_raw_tool_output_ring_trims_to_cap():
    """The raw-output corpus is bounded; a chatty run can't balloon memory. The most
    recent output (where a just-found flag lives) is kept when trimming."""
    s = _flag_solver()
    s._RAW_OUTPUT_CHAR_CAP = 1000  # shrink for the test
    for i in range(20):
        s._persist_raw_tool_output("X" * 200)
    assert s._raw_tool_outputs_chars <= 1000
    # the freshest chunk survived
    s._persist_raw_tool_output("FOUND_FLAG=flag{freshest}")
    assert "flag{freshest}" in s._provenance_corpus()


# 鈹€鈹€ driver-level: tool_result carries untruncated raw (run-75379) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_single_flag_prompt_has_no_multiflag_block():
    # expected_flags=1 (default) 鈫?the prompt must NOT carry the multi-flag block,
    # keeping single-flag runs byte-identical.
    ch = Challenge(id="t", name="t", category="web")
    s = _cli_solver(ch)
    p = s._build_prompt()
    assert "find them ALL" not in p and "has 1 flags" not in p


def test_multiflag_prompt_announces_count_and_found():
    ch = Challenge(id="t", name="t", category="web", expected_flags=3)
    s = _cli_solver(ch)
    s._already_found.add("flag{got1}")
    p = s._build_prompt()
    assert "has 3 flags" in p and "find them ALL" in p
    assert "flag{got1}" in p  # already-found list injected so it doesn't re-hunt


def test_expected_flags_helper_clamps():
    assert _cli_solver(Challenge(id="t", name="t", category="web"))._expected_flags() == 1
    assert _cli_solver(Challenge(id="t", name="t", category="web",
                                 expected_flags=0))._expected_flags() == 1
    assert _cli_solver(Challenge(id="t", name="t", category="web",
                                 expected_flags=4))._expected_flags() == 4


# 鈹€鈹€ external-USD cost accounting (shelled CLI bills in dollars) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_add_external_usd_bumps_ledger_and_emits():
    events = []

    class _Bus:
        async def emit(self, ev): events.append(ev)

    cost = CostController(bus=_Bus(), budget=Budget(global_usd=10.0))
    spent = asyncio.run(cost.add_external_usd(
        1.43, run_id="r", solver_id="cli-1", challenge_id="c",
        input_tokens=1000, output_tokens=200))
    assert spent == 1.43
    assert cost.global_usd() == 1.43
    # a COST_UPDATE was emitted for the deck, carrying the token breakdown so the
    # deck's token-usage column has per-scope counts alongside the $ figure.
    cu = [e for e in events if e.event_type is EventType.COST_UPDATE]
    assert cu, "expected a COST_UPDATE"
    p = cu[-1].payload
    assert p["tokens"] == 1200 and p["input_tokens"] == 1000 and p["output_tokens"] == 200
    assert p["usd"] == 1.43


def test_add_external_usd_feeds_budget_breaker():
    cost = CostController(budget=Budget(per_solver_usd=2.0))
    asyncio.run(cost.add_external_usd(2.5, run_id="r", solver_id="cli-1"))
    assert cost.over_budget("solver:cli-1") is True


def test_add_external_usd_records_tokens_at_zero_cost():
    # subscription-backed worker (usd=0) but reports token usage. The tokens
    # must land in the ledger / COST_UPDATE so the deck's token column counts them,
    # while $ stays flat.
    events = []

    class _Bus:
        async def emit(self, ev): events.append(ev)

    cost = CostController(bus=_Bus())
    asyncio.run(cost.add_external_usd(
        0.0, run_id="r", solver_id="cli-pi-1", challenge_id="c",
        input_tokens=27140, output_tokens=30))
    assert cost.global_usd() == 0.0          # no dollars from a subscription-backed worker
    p = [e for e in events if e.event_type is EventType.COST_UPDATE][-1].payload
    assert p["tokens"] == 27170 and p["input_tokens"] == 27140 and p["output_tokens"] == 30


# 鈹€鈹€ blackboard collaboration lifecycle (OneNote board) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class _CaptureBus:
    def __init__(self): self.events = []
    async def emit(self, ev): self.events.append(ev)


class _StubDriver:
    """A CLI driver that returns a canned transcript 鈥?no subprocess."""
    name = "pi"
    def __init__(self, text): self._text = text
    def new_session(self): return "sess-x"
    def build_execute(self, *a, **k): return ["true"]
    def build_resume(self, *a, **k): return ["true"]
    def parse(self, *a, **k): raise AssertionError("parse unused")
    def parse_stream_line(self, *a, **k): return None


def _bb_kinds(events):
    return [e.payload.get("kind") for e in events
            if e.event_type is EventType.BLACKBOARD_DELTA]


def _worker_statuses(events):
    return [e for e in events if e.event_type is EventType.WORKER_STATUS]


def _run_cli_solver(monkeypatch, transcript):
    """Run a CliSolver with the streaming runner stubbed to return `transcript`
    (CliSolver streams when a bus is present). Returns the bus + solver."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    drv = _StubDriver(transcript)
    s = CliSolver(None, ch, bus=bus, driver=drv, engine="pi", kb=False)
    canned = lambda *a, **k: CliResult(text=transcript, session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)  # the no-bus fallback path
    asyncio.run(s.run())
    return bus, s


def test_cli_solver_emits_full_intent_lifecycle_on_solve(monkeypatch):
    bus, s = _run_cli_solver(monkeypatch, "did the thing\nFOUND_FLAG=flag{real}\n")
    kinds = _bb_kinds(bus.events)
    # the OneNote board needs the claim lifecycle, not just loose facts
    assert kinds[0] == "intent_proposed"
    assert "intent_claimed" in kinds
    assert "fact_added" in kinds
    assert "intent_concluded" in kinds
    assert "flag_found" in kinds
    # concluded must say solved
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][0]
    assert concl.payload.get("result") == "solved"
    # the claimed intent's worker is this solver (so the board links its facts)
    claimed = [e for e in bus.events
               if e.event_type is EventType.BLACKBOARD_DELTA
               and e.payload.get("kind") == "intent_claimed"][0]
    assert claimed.payload.get("worker") == s.solver_id


def test_cli_solver_concludes_explored_on_miss_without_dead_end(monkeypatch):
    bus, s = _run_cli_solver(monkeypatch, "poked around, found nothing useful\n")
    kinds = _bb_kinds(bus.events)
    assert "intent_proposed" in kinds and "intent_claimed" in kinds
    assert "dead_end" not in kinds
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][0]
    assert concl.payload.get("result") == "explored"
    assert "found no verified flag" in concl.payload.get("result_detail", "")
    assert "flag_found" not in kinds


def test_record_fact_db_failure_does_not_emit_blackboard_fact():
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")

    class RejectingGraph:
        def add_evidence(self, **kw):
            return -1

    s = CliSolver(
        None,
        ch,
        bus=bus,
        driver=_StubDriver(""),
        engine="pi",
        kb=False,
        shared_graph=RejectingGraph(),
    )

    fact_seq = asyncio.run(
        s._record_fact("fact that failed to persist", verified=True, artifact_id="aid"))

    assert fact_seq == -1
    assert "fact_added" not in _bb_kinds(bus.events)



def test_cli_runtime_stderr_does_not_become_challenge_fact(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        return CliResult(
            text="", session="sess-x",
            raw_stderr='Error: Unknown provider "dswarm-worker". Use --list-models to see available providers/models.',
        )

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)

    outcome = asyncio.run(s.run())
    kinds = _bb_kinds(bus.events)

    assert calls["n"] == 1
    assert outcome.solved is False
    assert "fact_added" not in kinds
    assert "worker_runtime_error" in kinds
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][-1]
    assert concl.payload.get("result") == "runtime_failure"
    assert _worker_statuses(bus.events)[-1].payload["reason"] == "runtime_failure"


def test_cli_runtime_provider_error_emits_diagnostic_and_recovery_directive(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(
        None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False,
        worker_env={
            "DSWARM_LLM_PROVIDER_REF": "deepseek-main",
            "DSWARM_CREDENTIAL_ACCOUNT_ID": "acct-primary",
        },
    )

    def fake_stream(*a, **k):
        return CliResult(
            text="",
            session="sess-x",
            raw_stderr="ConnectTimeout: connection reset by peer while calling provider",
        )

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)

    outcome = asyncio.run(s.run())

    provider_events = [e for e in bus.events if e.event_type is EventType.PROVIDER_ERROR]
    assert provider_events, "runtime provider stderr must be visible to the UI"
    diag = provider_events[-1].payload
    assert diag["category"] == "transient_network"
    assert diag["retryable"] is True
    assert diag["should_pause_dispatch"] is False
    assert diag["provider"] == "deepseek-main"
    assert diag["account_id"] == "acct-primary"
    assert diag["worker_id"] == s.solver_id

    directives = [e.payload for e in bus.events
                  if e.event_type is EventType.BLACKBOARD_DELTA
                  and e.payload.get("kind") == "provider_recovery_directive"]
    assert directives, "next worker must have a blackboard directive to consume"
    assert directives[-1]["recovery_action"] == "retry_next_worker"
    assert directives[-1]["current_worker_interrupted"] is False
    assert "single-shot" in directives[-1]["operator_message"]
    assert outcome.provider_error["category"] == "transient_network"


def test_explore_runtime_stderr_does_not_become_challenge_fact(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(
        None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False,
        mode="explore", intent_goal="try /admin")
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        return CliResult(
            text="", session="sess-x",
            raw_stderr='Error: Unknown provider "dswarm-worker". Use --list-models to see available providers/models.',
        )

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)

    outcome = asyncio.run(s.run())
    kinds = _bb_kinds(bus.events)

    assert calls["n"] == 1
    assert outcome.solved is False
    assert "fact_added" not in kinds
    assert "worker_runtime_error" in kinds
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][-1]
    assert concl.payload.get("result") == "runtime_failure"
    assert _worker_statuses(bus.events)[-1].payload["reason"] == "runtime_failure"

def test_cli_solver_worker_status_reports_timeout(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    drv = _StubDriver("")
    s = CliSolver(None, ch, bus=bus, driver=drv, engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return CliResult(text="still working\n", session="sess-x", timed_out=True)
        return CliResult(text="FOUND_FLAG=NONE\n", session="sess-x")

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())

    statuses = _worker_statuses(bus.events)
    assert statuses[0].payload == {
        "online": True,
        "status": "online",
        "reason": "started",
        "engine": "pi",
        "session": "",
        "worker_role": "bootstrap",
    }
    # once the worker's CLI session id is known it re-emits status carrying it, so
    # the deck can surface a resume command (`pi --session <id>`) for manual attach.
    assert any(s.payload.get("session") == "sess-x" for s in statuses)
    assert statuses[-1].payload["online"] is False
    assert statuses[-1].payload["status"] == "offline"
    assert statuses[-1].payload["reason"] == "timeout"
    kinds = _bb_kinds(bus.events)
    assert "dead_end" not in kinds
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][0]
    assert concl.payload.get("result") == "timed_out"
    assert "timed out" in concl.payload.get("result_detail", "").lower()


def test_cli_streaming_emits_busy_heartbeat_during_silent_turn(monkeypatch, tmp_path):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi",
                  kb=False)

    def silent_stream(*a, **k):
        time.sleep(0.07)
        return CliResult(text="no flag\n", session="sess-x")

    monkeypatch.setattr(mod, "_WORKER_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(mod, "run_cli_streaming", silent_stream)

    asyncio.run(s._run_streaming(["true"], cwd=str(tmp_path), timeout=5))

    statuses = _worker_statuses(bus.events)
    assert any(
        ev.payload.get("online") is True
        and ev.payload.get("status") == "online"
        and ev.payload.get("reason") == "busy"
        for ev in statuses
    )


def test_cli_solver_worker_status_reports_asyncio_cancel(monkeypatch):
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)

    async def cancelled_bootstrap():
        raise asyncio.CancelledError()

    monkeypatch.setattr(s, "_run_bootstrap", cancelled_bootstrap)

    async def drive():
        try:
            await s.run()
        except asyncio.CancelledError:
            return
        raise AssertionError("run() did not propagate cancellation")

    asyncio.run(drive())
    statuses = _worker_statuses(bus.events)
    assert statuses[0].payload["online"] is True
    assert statuses[-1].payload["online"] is False
    assert statuses[-1].payload["reason"] == "cancelled"


# 鈹€鈹€ Explore mode (one intent at a time) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_cli_solver_explore_mode_produces_structured_facts():
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = _cli_solver(ch, mode="explore", intent_goal="try SQLi on /login")
    assert s.mode == "explore"
    assert s.intent_goal == "try SQLi on /login"
    # the explore prompt includes the intent goal
    prompt = s._build_explore_prompt()
    assert "try SQLi on /login" in prompt
    assert "VERIFIED_FACT=" in prompt and "DEADEND=" in prompt


def test_extract_structured_facts_parses_markers():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    text = (
        "probing /login ...\n"
        "VERIFIED_FACT=login form has no CSRF token\n"
        "VERIFIED_FACT=admin:admin returns 302 鈫?/dashboard\n"
        "DEADEND=XSS on search param is sanitized server-side\n"
        "FOUND_FLAG=flag{easy}\n"
    )
    facts, deadends = s._extract_structured_facts(text)
    assert facts == ["login form has no CSRF token",
                     "admin:admin returns 302 鈫?/dashboard"]
    assert deadends == ["XSS on search param is sanitized server-side"]


def test_extract_structured_facts_empty_on_no_markers():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    facts, deadends = s._extract_structured_facts("just prose, no markers")
    assert facts == [] and deadends == []


def test_explore_emits_intent_claimed_and_structured_facts(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    drv = _StubDriver("")
    transcript = (
        "probed /login\n"
        "VERIFIED_FACT=admin:admin works on /login\n"
        "DEADEND=no SQLi on search param\n"
    )
    s = CliSolver(None, ch, bus=bus, driver=drv, engine="pi", kb=False,
                  mode="explore", intent_goal="try credentials on /login",
                  intent_id="I42")
    canned = lambda *a, **k: CliResult(text=transcript, session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    asyncio.run(s.run())
    kinds = _bb_kinds(bus.events)
    assert "intent_claimed" in kinds
    assert "dead_end" in kinds
    assert "intent_concluded" in kinds
    # structured fact was written
    sg_events = [e for e in bus.events
                 if e.event_type is EventType.SHARED_GRAPH_DELTA]
    assert any("admin:admin" in (e.payload.get("fact") or "") for e in sg_events)


def test_explore_prompt_includes_intent_graph_neighborhood(tmp_path):
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=ch)
    root = g.add_evidence(actor="cli-a", source="curl",
                          fact="login leaks admin cookie", verified=True)
    g.propose_intent(actor="reason", intent_id="I-main", goal="use admin cookie",
                     from_fact_seqs=[root])
    g.propose_intent(actor="reason", intent_id="I-sibling", goal="test cookie on /api",
                     from_fact_seqs=[root])
    s = _cli_solver(ch, mode="explore", intent_goal="use admin cookie",
                    intent_id="I-main", shared_graph=g)

    prompt = s._build_explore_prompt()

    assert "Intent graph neighborhood" in prompt
    assert "login leaks admin cookie" in prompt
    assert "I-sibling" in prompt and "test cookie on /api" in prompt
    g.close()


def test_explore_solved_concludes_intent_without_fact_seq(monkeypatch, tmp_path):
    """#13 regression: an explore worker that ACCEPTS a flag but records NO fact-seq
    (only FOUND_FLAG, no VERIFIED_FACT 鈫?_last_fact_seq stays unset) must STILL conclude
    its intent (status='done'). The solved branch used to be gated on `lfs is not None`,
    so such an intent stayed status='claimed'; its lease expired and the already-solved
    direction was re-dispatched (run-11190 churn). The other three exits already
    concluded unconditionally 鈥?this was the last gated one."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    ch, g = _real_graph(tmp_path)
    g.propose_intent(actor="reason", intent_id="I-solve", goal="probe /admin")
    g.claim_intent(worker="cli-1", intent_id="I-solve", lease_s=1000.0)
    assert "I-solve" in {i for i in _open_or_claimed_ids(g)}

    s = _cli_solver(ch, kb=False, shared_graph=g, mode="explore",
                    intent_goal="probe /admin", intent_id="I-solve")
    # transcript has ONLY a flag 鈥?no VERIFIED_FACT 鈫?_last_fact_seq never set 鈫?lfs None
    canned = lambda *a, **k: CliResult(text="FOUND_FLAG=flag{got_it}\n", session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    out = asyncio.run(s.run())
    assert out.solved is True
    # the intent must be concluded (NOT left open/claimed for re-dispatch)
    assert s._last_fact_seq <= 0, "test premise: no fact-seq was recorded"
    assert _intent_status(g, "I-solve") == "done", \
        "a solved explore intent with no fact-seq must still be concluded"


def test_explore_tail_stream_delta_not_recorded_as_fact(monkeypatch, tmp_path):
    """run-3157 seq 16: an explore worker whose only "output" is a pi stream
    envelope (`{"type":"agent_settled"}`) must NOT record it as a candidate fact
    — the tail-summary filter was only wired into the bootstrap path."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    ch, g = _real_graph(tmp_path)
    g.propose_intent(actor="reason", intent_id="I-e", goal="probe /admin")
    s = _cli_solver(ch, kb=False, shared_graph=g, mode="explore",
                    intent_goal="probe /admin", intent_id="I-e", bus=_CaptureBus())
    canned = lambda *a, **k: CliResult(text='{"type":"agent_settled"}', session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    asyncio.run(s.run())
    facts = [e for e in s.bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "fact_added"]
    for e in facts:
        assert "agent_settled" not in str(e.payload.get("fact", "")), e.payload
    # NO worker words in the output (settled envelope only): no fallback fact
    # at all. Placeholder "(no output)" facts flooded churn loops with junk
    # candidates (arena-6826: 240 identical envelope "facts"). Thinking-line
    # backfill still provides board content separately.
    assert not facts


def test_thinking_findings_backfilled_as_candidate_facts(monkeypatch, tmp_path):
    """run-3155..3158: a recon/explore worker that gets cut off before writing
    VERIFIED_FACT markers leaves the board nearly empty. Its analysis lives in the
    persisted pi-session `thinking` blocks — the backfill mines those lines and
    records them as CANDIDATE facts (never verified)."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    sess_dir = tmp_path / ".pi-sessions"
    sess_dir.mkdir()
    (sess_dir / "2026-01-01T00-00-00-000Z_sess-abc.jsonl").write_text(
        "\n".join([
            '{"type":"message","message":{"role":"assistant","content":[{"type":"thinking","thinking":"The PHP app at http://127.0.0.1:80 is reachable via XXE/SSRF from the preview endpoint."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"thinking","thinking":"Let me fuzz harder with bigger wordlists."}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"thinking","thinking":"Should I try SSTI next?"}]}}',
            '{"type":"message","message":{"role":"assistant","content":[{"type":"thinking","thinking":"noise"}]}}',
        ]),
        encoding="utf-8",
    )

    ch, g = _real_graph(tmp_path)
    g.propose_intent(actor="reason", intent_id="I-t", goal="recon")
    s = _cli_solver(ch, kb=False, shared_graph=g, mode="explore",
                    intent_goal="recon", intent_id="I-t", bus=_CaptureBus(),
                    workdir=str(tmp_path))
    canned = lambda *a, **k: CliResult(text="no structured markers", session="sess-abc")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    asyncio.run(s.run())
    facts = [str(e.payload.get("fact", "")) for e in s.bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "fact_added"]
    joined = " | ".join(facts)
    assert "XXE/SSRF" in joined, joined           # the real finding was mined
    assert "fuzz harder" not in joined, joined    # filler/planning line skipped
    assert "Should I try SSTI" not in joined      # question skipped


def _open_or_claimed_ids(g):
    import sqlite3
    con = sqlite3.connect(g.db_path)
    try:
        return [r[0] for r in con.execute(
            "SELECT intent_id FROM intents WHERE status IN ('open','claimed')")]
    finally:
        con.close()


def _intent_status(g, intent_id):
    import sqlite3
    con = sqlite3.connect(g.db_path)
    try:
        row = con.execute("SELECT status FROM intents WHERE intent_id=?",
                          (intent_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def test_explore_conclude_fallback_fires_on_no_markers(monkeypatch):
    """If the main explore pass produces no structured markers, the conclude
    fallback fires (build_resume with EXPLORE_CONCLUDE_PROMPT) and the
    conclude output is parsed for markers."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    drv = _StubDriver("")
    call_count = {"n": 0}
    def fake_stream(*a, **k):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # first call: explore, no markers
            return CliResult(text="just probing around\n", session="sess-x")
        # second call: conclude fallback, produces markers
        return CliResult(text="DEADEND=login page has WAF, gave up\n", session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    s = CliSolver(None, ch, bus=bus, driver=drv, engine="pi", kb=False,
                  mode="explore", intent_goal="probe /login")
    asyncio.run(s.run())
    assert call_count["n"] == 2  # main + conclude fallback
    kinds = _bb_kinds(bus.events)
    assert "dead_end" in kinds


# 鈹€鈹€ live streaming (the deck shows the worker working, not a dead pause) 鈹€鈹€鈹€鈹€鈹€鈹€

@pytest.mark.posix
def test_run_cli_streaming_fires_on_step_per_line(tmp_path):
    # end-to-end: a fake echo command emits two JSONL lines; on_step sees both,
    # and parse() builds the final result from the accumulated stdout.
    from dswarm.solver.cli_driver import run_cli_streaming, StreamStep
    d = PiDriver()
    line1 = '{"type":"assistant","message":{"content":[{"type":"text","text":"step one"}]}}'
    line2 = '{"type":"result","result":"FOUND_FLAG=flag{ok}","session_id":"z"}'
    script = tmp_path / "fake.sh"
    script.write_text(f"#!/bin/sh\necho '{line1}'\necho '{line2}'\n")
    script.chmod(0o755)
    seen = []
    res = run_cli_streaming(d, ["/bin/sh", str(script)], cwd=str(tmp_path),
                            timeout=10, on_step=lambda s: seen.append(s.kind))
    assert "reasoning" in seen  # the live step fired
    assert "FOUND_FLAG=flag{ok}" in res.text  # final result still parsed
    assert res.session == "z"


# 鈹€鈹€ dispatcher control: blackboard context + cancel + pause 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

class _StubGraph:
    """A minimal SharedGraph stand-in: a fixed to_summary() + capture writes."""
    def __init__(self, summary):
        self._summary = summary
        self.facts = []
        self.dead_ends = []
        self.db_path = "/tmp/none.db"
    def to_summary(self, *a, **k): return self._summary
    def add_evidence(self, **kw): self.facts.append(kw); return 1
    def add_dead_end(self, **kw): self.dead_ends.append(kw); return 1


def test_board_context_fallback_inlines_when_no_file_written():
    # Board file-handoff: when the loop hasn't written the board file yet
    # (_board_file_written is falsey), _board_context falls back to an INLINE
    # bounded summary so the worker is never blind. _StubGraph only has to_summary,
    # so this exercises exactly that fallback path.
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    sg = _StubGraph("FACTS:\n- admin panel at /admin\nDEAD ENDS:\n- /login has a WAF")
    s = _cli_solver(ch, kb=False, shared_graph=sg)
    boot = s._build_prompt()
    expl = _cli_solver(ch, kb=False, shared_graph=sg, mode="explore",
                       intent_goal="probe /admin")._build_explore_prompt()
    for prompt in (boot, expl):
        assert "Shared team board" in prompt
        assert "admin panel at /admin" in prompt        # inline fallback carries facts
        assert "/login has a WAF" in prompt


def test_flag_hint_token_mode_does_not_say_brace(monkeypatch):
    # run-11189: a token/collect challenge's prompt must NOT tell the worker the
    # flag is shaped like flag{...} (it has none) 鈥?that suppresses FOUND_FLAG=.
    ch_tok = Challenge(id="t", name="ladder", category="misc", flag_format="token",
                       multi_flag=True, expected_flags=14)
    p = _cli_solver(ch_tok, kb=False)._build_prompt()
    # the token hint tells the worker the flag is a bare token, NOT shaped like flag{...}
    assert "bare token" in p
    assert "shaped like flag{...}" not in p   # the old misleading instruction is gone
    # brace challenge keeps the exact old wording.
    ch_brace = Challenge(id="t", name="web", category="web", flag_format=r"flag\{.*?\}",
                         target="http://x")
    pb = _cli_solver(ch_brace, kb=False)._build_prompt()
    assert "shaped like flag{...}" in pb

    # multi-flag is a collection mode, not a token-format signal. Common CTF
    # challenges can require several ordinary flag{...} values.
    ch_multi_brace = Challenge(
        id="t", name="multi-web", category="web",
        flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}",
        multi_flag=True, expected_flags=3, target="http://x")
    pm = _cli_solver(ch_multi_brace, kb=False)._build_prompt()
    assert "shaped like flag{...}" in pm
    assert "bare token" not in pm


def test_flag_hint_uses_custom_wrapper_hint():
    ch = Challenge(
        id="t",
        name="custom",
        category="web",
        target="http://x",
        flag_format_hint="WMCTF{...}",
    )
    prompt = _cli_solver(ch, kb=False)._build_prompt()
    assert "The flag is shaped like WMCTF{...}" in prompt
    assert "The flag is shaped like flag{...}" not in prompt


def test_board_context_pointer_when_file_written():
    # When the loop HAS written the board file, the prompt carries a POINTER to
    # ./.dswarm_board.md (+ the bounded credential digest), NOT the full inline body.
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    sg = _StubGraph("FACTS:\n- admin panel at /admin")
    s = _cli_solver(ch, kb=False, shared_graph=sg)
    s._board_file_written = True            # simulate the loop's per-turn write
    prompt = s._build_prompt()
    assert "Shared team board" in prompt
    assert ".dswarm_board.md" in prompt     # the pointer
    assert "READ IT FIRST" in prompt
    assert 'python3 "$DSWARM_BLACKBOARD_SCRIPT" read-review' in prompt
    assert 'python3 "$DSWARM_BLACKBOARD_SCRIPT" read-deadends' in prompt
    assert 'python3 "$DSWARM_BLACKBOARD_SCRIPT" read-facts' in prompt
    assert 'python3 "$DSWARM_BLACKBOARD_SCRIPT" write-fact "<fact>" --verified' in prompt
    # the full inline fact body is NOT dumped into the prompt (it's in the file)
    assert "admin panel at /admin" not in prompt


def test_board_context_empty_when_no_graph_or_empty_summary():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    # no shared graph at all 鈫?no board section
    assert "Shared team board" not in _cli_solver(ch, kb=False)._build_prompt()
    # graph present but board empty 鈫?still no section (don't inject noise)
    s = _cli_solver(ch, kb=False, shared_graph=_StubGraph("   "))
    assert "Shared team board" not in s._build_prompt()


# 鈹€鈹€ Board file-handoff (DESIGN_board_file_handoff) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Full board written to a workdir file + pointer/digest in the prompt; the chain
# is derived from VERIFIED fact TEXT (P2A), never from graph edges. These use a
# REAL SQLiteSharedGraph so the file body / extraction is exercised end to end.

import tempfile  # noqa: E402
from pathlib import Path as _P  # noqa: E402

from dswarm.swarm.shared_graph import SQLiteSharedGraph  # noqa: E402
from dswarm.solver.workspace import materialize_shared_artifact  # noqa: E402


def _real_graph(tmp_path, facts=(), deadends=()):
    ch = Challenge(id="t", name="ghost", category="misc", points=0,
                   flag_format=r"flag\{.*?\}")
    g = SQLiteSharedGraph(str(_P(tmp_path) / "sg.db"), ch)
    for actor, fact, verified in facts:
        g.add_evidence(actor=actor, source=actor.split("-")[-1], fact=fact,
                       verified=verified)
    for reason in deadends:
        g.add_dead_end(actor="cli-x", reason=reason)
    return ch, g


def test_write_board_file_full_untruncated(tmp_path):
    # the file holds ALL facts with no [-16]/[:2000] truncation, creds on top.
    facts = [("cli-c", f"ghost{i} login succeeds with password PW{i}aB; whoami ghost{i}",
              True) for i in range(40)]  # 40 > the old 16-fact cap
    ch, g = _real_graph(tmp_path, facts=facts)
    s = _cli_solver(ch, kb=False, shared_graph=g)
    wd = _P(tempfile.mkdtemp())
    assert s._write_board_file(wd) is True
    body = (wd / ".dswarm_board.md").read_text()
    assert "dswarm-team-board" in body            # sentinel
    assert "Recovered credentials" in body         # P2A section on top
    assert "PW0aB" in body and "PW39aB" in body     # FIRST and LAST fact both present
    import shutil; shutil.rmtree(wd, ignore_errors=True)


def test_write_board_file_collision_does_not_clobber(tmp_path):
    ch, g = _real_graph(tmp_path, facts=[("cli-c", "ghost1 password PWaB1xyz works, whoami ghost1", True)])
    s = _cli_solver(ch, kb=False, shared_graph=g)
    wd = _P(tempfile.mkdtemp())
    (wd / ".dswarm_board.md").write_text("CHALLENGE DATA not a board")
    assert s._write_board_file(wd) is False          # refuses to clobber
    assert "CHALLENGE DATA" in (wd / ".dswarm_board.md").read_text()
    import shutil; shutil.rmtree(wd, ignore_errors=True)


def test_write_board_file_rewrites_own_file(tmp_path):
    # our own board file (sentinel present) IS overwritten on the next turn 鈫?fresh.
    ch, g = _real_graph(tmp_path, facts=[("cli-c", "ghost1 password PWaB1xyz works, whoami ghost1", True)])
    s = _cli_solver(ch, kb=False, shared_graph=g)
    wd = _P(tempfile.mkdtemp())
    assert s._write_board_file(wd) is True
    g.add_evidence(actor="cli-c", source="c",
                   fact="ghost2 password PWaB2xyz works, logged in as ghost2", verified=True)
    assert s._write_board_file(wd) is True           # overwrites own file
    assert "ghost2:PWaB2xyz" in (wd / ".dswarm_board.md").read_text()
    import shutil; shutil.rmtree(wd, ignore_errors=True)


def test_write_board_file_is_run_level_single_file_symlinked_to_workers(tmp_path):
    ch, g = _real_graph(tmp_path, facts=[("cli-c", "ghost1 password PWaB1xyz works, whoami ghost1", True)])
    s = _cli_solver(ch, kb=False, shared_graph=g)
    workspace = _P(tmp_path) / "workspace"
    wd1 = workspace / "workers" / "cli-pi-1"
    wd2 = workspace / "workers" / "cli-pi-2"
    assert s._write_board_file(wd1) is True
    assert s._write_board_file(wd2) is True
    root_board = workspace / ".dswarm_board.md"
    assert root_board.exists()
    assert (wd1 / ".dswarm_board.md").is_symlink()
    assert (wd2 / ".dswarm_board.md").is_symlink()
    assert (wd1 / ".dswarm_board.md").resolve() == root_board
    assert (wd2 / ".dswarm_board.md").resolve() == root_board
    assert root_board.stat().st_ino == (wd1 / ".dswarm_board.md").resolve().stat().st_ino


def test_defect0_accept_flag_records_on_shared_graph(tmp_path):
    """P0 defect-0: _accept_flag must write the flag to the SHARED graph (not only
    the worker's local SolveGraph), so reason/board/progress can read real flag
    progress from the graph. Before the fix, shared_graph.flag_found had 0 callers
    and snapshot().flags was always empty (run-11190 RUN_FINISHED empty flags)."""
    ch, g = _real_graph(tmp_path)
    s = _cli_solver(ch, kb=False, shared_graph=g)
    assert g.snapshot().flags == []                       # nothing yet
    assert asyncio.run(s._accept_flag("flag{real_one}")) is True
    assert g.snapshot().flags == ["flag{real_one}"]       # now on the SHARED graph
    # idempotent: same flag again is a no-op (dedup), graph still has exactly one
    assert asyncio.run(s._accept_flag("flag{real_one}")) is False
    assert g.snapshot().flags == ["flag{real_one}"]
    # a second distinct flag accumulates (multi-flag)
    assert asyncio.run(s._accept_flag("flag{second}")) is True
    assert g.snapshot().flags == ["flag{real_one}", "flag{second}"]


def test_defect1_solved_claim_downgraded_not_verified(tmp_path):
    """P0 defect-1: a bare 'solved / 宸茶В / task complete' claim must NOT become a
    VERIFIED evidence fact (run-42599: 28 solved-like verified facts poisoned the
    board). It's downgraded to an unverified candidate; only the flag gate decides
    completion."""
    ch, g = _real_graph(tmp_path)
    s = _cli_solver(ch, kb=False, shared_graph=g)

    # a completion CLAIM with no flag 鈫?recorded UNVERIFIED (downgraded)
    asyncio.run(s._record_fact("the challenge is solved, task complete",
                               verified=True, artifact_id=""))
    snap = g.snapshot()
    claims = [e for e in g.events() if e["kind"] == "fact_added"]
    assert claims and claims[-1]["verified"] is False, \
        "a solved-claim must be downgraded to unverified, not trusted as evidence"

    # a 宸茶В claim (zh) likewise downgraded
    asyncio.run(s._record_fact("宸茶В锛屾湰鏉ュ氨涓嶉渶瑕佹墦 .154", verified=True, artifact_id=""))
    zh = [e for e in g.events() if e["kind"] == "fact_added"][-1]
    assert zh["verified"] is False

    # REAL evidence (no completion claim) stays verified 鈥?not over-broad
    asyncio.run(s._record_fact("admin panel reachable at /admin, returns 200",
                               verified=True, artifact_id="art1"))
    real = [e for e in g.events() if e["kind"] == "fact_added"][-1]
    assert real["verified"] is True, "concrete evidence must stay verified"

    # once this worker holds a real gated flag, its claims are earned 鈫?not downgraded
    asyncio.run(s._accept_flag("flag{got_it}"))
    asyncio.run(s._record_fact("challenge solved 鈥?flag recovered",
                               verified=True, artifact_id="art2"))
    earned = [e for e in g.events() if e["kind"] == "fact_added"][-1]
    assert earned["verified"] is True


def test_defect2_progress_block_reads_shared_graph(tmp_path):
    """P0 defect-2: multi-flag PROGRESS block reads N/total from the SHARED graph
    (defect-0 made it the durable source) 鈥?so a worker sees flags a teammate found,
    not just its own _already_found. Single-flag challenge 鈫?empty (byte-identical)."""
    ch = Challenge(id="t", name="multi", category="web", points=0,
                   flag_format=r"flag\{.*?\}", expected_flags=3)
    _, g = _real_graph(tmp_path)
    s = _cli_solver(ch, kb=False, shared_graph=g)

    # a teammate found one flag 鈫?it's on the shared graph, NOT this worker's set
    g.flag_found(actor="cli-sibling", flag="flag{one}")
    block = s._team_context_block()
    assert "3 flags" in block and "1/3 captured" in block and "2 remaining" in block
    assert "flag{one}" in block                      # teammate's flag surfaced
    # and it's injected into BOTH prompt builders (was only _build_prompt before)
    assert "1/3 captured" in s._build_prompt()
    assert "1/3 captured" in s._build_explore_prompt()


def test_defect2_single_flag_block_empty(tmp_path):
    """defect-2: a single-flag challenge gets NO progress block (byte-identical to
    the pre-multi-flag prompt)."""
    ch = Challenge(id="t", name="single", category="web", points=0,
                   flag_format=r"flag\{.*?\}")  # expected_flags defaults to 1
    _, g = _real_graph(tmp_path)
    s = _cli_solver(ch, kb=False, shared_graph=g)
    assert s._team_context_block() == ""


def test_defect8_unbacked_evidence_downgraded_to_candidate(tmp_path):
    """P0 defect-8: a VERIFIED evidence fact with NO provenance artifact is an
    unbacked assertion (the no-evidence hallucination) 鈫?downgraded to an unverified
    candidate. A fact WITH an artifact stays verified."""
    ch, g = _real_graph(tmp_path)
    s = _cli_solver(ch, kb=False, shared_graph=g)

    # no artifact 鈫?downgraded
    asyncio.run(s._record_fact("the database is sqlite", verified=True, artifact_id=""))
    no_art = [e for e in g.events() if e["kind"] == "fact_added"][-1]
    assert no_art["verified"] is False, "unbacked verified fact must be downgraded"

    # with artifact 鈫?stays verified
    asyncio.run(s._record_fact("admin:hunter2 logs in (HTTP 302 to /dashboard)",
                               verified=True, artifact_id="art-real"))
    art = [e for e in g.events() if e["kind"] == "fact_added"][-1]
    assert art["verified"] is True, "evidence backed by an artifact stays verified"


def test_write_board_file_no_graph_returns_false():
    s = _cli_solver(Challenge(id="t", name="t", category="web",
                              flag_format=r"flag\{.*?\}"), kb=False)
    wd = _P(tempfile.mkdtemp())
    assert s._write_board_file(wd) is False
    assert not (wd / ".dswarm_board.md").exists()
    assert s._board_context() == ""                  # no dangling pointer
    import shutil; shutil.rmtree(wd, ignore_errors=True)


def test_write_board_file_failure_falls_back_to_inline(tmp_path):
    # if the write raises, _board_context must NOT emit a pointer 鈥?it inlines.
    ch, g = _real_graph(tmp_path, facts=[("cli-c", "ghost1 password PWaB1xyz works, whoami ghost1", True)])
    s = _cli_solver(ch, kb=False, shared_graph=g)
    # point the write at a path that can't be created (a file as a parent dir)
    bad_parent = _P(tempfile.mktemp()); bad_parent.write_text("x")
    assert s._write_board_file(bad_parent / "sub") is False
    assert s._board_file_written is False
    prompt = s._build_prompt()
    assert ".dswarm_board.md" not in prompt          # NO dangling pointer
    assert "Shared team board" in prompt             # but inline fallback present
    import os as _os; _os.remove(bad_parent)


def test_canonical_credentials_verified_only_and_skips_failures(tmp_path):
    facts = [
        ("cli-c", "ghost1 login SUCCEEDS with password W3lc0m3T0Gh0st, whoami ghost1", True),
        ("cli-c", "ghost2 password a1e7c9d4f2b8 is DENIED for ghost2 login", True),   # FAIL
        ("cli-c", "ghost3 guess maybe ghost3pw unverified", False),                    # unverified
        ("cli-c", "H1dd3nInSh4dow successfully authenticates as ghost3, whoami ghost3", True),
        ("cli-c", "ghost1 hex file ./- contains a1e7c9d4f2b8, a decoy fragment", True),# decoy
    ]
    ch, g = _real_graph(tmp_path, facts=facts)
    creds = g.canonical_credentials()
    got = {c["entity"]: c["value"] for c in creds}
    assert got.get("ghost1") == "W3lc0m3T0Gh0st"
    assert got.get("ghost3") == "H1dd3nInSh4dow"
    assert "ghost2" not in got                        # the DENIED guess is NOT promoted
    # the failed/decoy token never appears as a recovered credential value
    assert all(c["value"] != "a1e7c9d4f2b8" for c in creds)


def test_canonical_credentials_rejects_ssh_config_flags(tmp_path):
    # run-10070 regression: an SSH-options fact produced a false-positive
    # `ghost0:Authentication=no` row. SSH/config flag assignments are not passwords.
    facts = [
        ("cli-c", "ghost0/ghost1 鐧诲綍鎴愬姛锛涢櫎 -o PubkeyAuthentication=no -o "
                  "StrictHostKeyChecking=no 澶栬繕闇€ IdentitiesOnly=yes", True),
        ("cli-c", "ghost1 login succeeds with password W3lc0m3T0Gh0st; whoami ghost1", True),
    ]
    ch, g = _real_graph(tmp_path, facts=facts)
    creds = {c["entity"]: c["value"] for c in g.canonical_credentials()}
    assert "ghost0" not in creds or "uthentication" not in creds.get("ghost0", "")
    assert all("=no" not in c["value"] and "=yes" not in c["value"]
               for c in g.canonical_credentials())
    assert creds.get("ghost1") == "W3lc0m3T0Gh0st"     # real cred preserved


def test_canonical_credentials_attributes_to_unlocked_entity(tmp_path):
    # run-10067 case: a cred found in ghost2's home that UNLOCKS ghost3 must be
    # attributed to ghost3, not ghost2 (the box it was discovered on). The
    # "authenticates as ghostN" target wins over the leading "found in" entity.
    facts = [
        ("cli-c", "D4shIsN0tAFl4g successfully authenticates as ghost2; whoami ghost2", True),
        ("cli-c", "ghost2 hidden lead .source_omega contains H1dd3nInSh4dow; "
                  "it authenticates as ghost3", True),
    ]
    ch, g = _real_graph(tmp_path, facts=facts)
    creds = {c["entity"]: c["value"] for c in g.canonical_credentials()}
    assert creds.get("ghost2") == "D4shIsN0tAFl4g"
    assert creds.get("ghost3") == "H1dd3nInSh4dow"     # NOT mis-attributed to ghost2


def test_canonical_credentials_newest_wins_per_entity(tmp_path):
    facts = [
        ("cli-c", "ghost2 password OLDvalA1 works, whoami ghost2", True),
        ("cli-c", "correction: ghost2 password NEWvalB2 authenticates, logged in", True),
    ]
    ch, g = _real_graph(tmp_path, facts=facts)
    creds = {c["entity"]: c["value"] for c in g.canonical_credentials()}
    assert creds["ghost2"] == "NEWvalB2"              # newest verified fact wins


def test_board_file_carries_full_untruncated_brief(tmp_path):
    # run-10070 regression: the SSH/target block lives deep in the challenge
    # description (past the 300-char to_summary cap). The board FILE must carry the
    # full brief so a worker reads the target there instead of grepping session
    # files. Build a long description with the connection block near the end.
    head = "Ghost wargame. " + ("filler narrative. " * 60)   # > 300 chars of preamble
    ssh = "SSH Access Host 204.168.229.209 Port 2222 User ghost0 Password ghost0"
    ch = Challenge(id="t", name="ghost", category="misc", points=0,
                   flag_format=r"flag\{.*?\}", description=head + ssh)
    g = SQLiteSharedGraph(str(_P(tmp_path) / "sg.db"), ch)
    body = g.to_board_markdown()
    assert "204.168.229.209" in body          # the host survives (not capped at 300)
    assert "Password ghost0" in body
    assert "Challenge brief (full" in body


def test_poc_save_quarantines_flag_and_secret_literals(tmp_path):
    ch = Challenge(id="poc-q", name="poc-q", category="web",
                   flag_format=r"flag\{[^}]+\}")
    root = _P(tmp_path) / "run" / "workspace"
    wd = root / "workers" / "cli-1"
    wd.mkdir(parents=True)
    g = SQLiteSharedGraph(str(root / "graph" / "shared_graph.db"), ch)
    poc = wd / "poc.py"
    poc.write_text("print('flag{stale_from_prior}')\nAPI_KEY='sk-secret-value-12345'\n")
    s = _cli_solver(ch, kb=False, shared_graph=g, workdir=str(wd))

    asyncio.run(s._stream_markers("POC_SAVE=poc.py|python poc.py|available|first cut\n"))

    rows = g.pocs()
    assert rows[0]["status"] == "quarantined"
    saved = (root / rows[0]["path"]).read_text()
    assert "flag{stale_from_prior}" not in saved
    assert "sk-secret-value-12345" not in saved
    assert "<PRIOR_FLAG>" in saved and "<SECRET>" in saved


def test_inherited_poc_mounts_under_inherited_and_claims(tmp_path):
    ch = Challenge(id="poc-inherit", name="poc-inherit", category="web",
                   flag_format=r"flag\{[^}]+\}")
    root = _P(tmp_path) / "run" / "workspace"
    src_dir = _P(tmp_path) / "src"
    src_dir.mkdir()
    src = src_dir / "poc.py"
    src.write_text("print('hit target')\n")
    g = SQLiteSharedGraph(str(root / "graph" / "shared_graph.db"), ch)
    art = materialize_shared_artifact(root, src, name="poc.py", kind="poc",
                                      status="available")
    g.save_poc(actor="cli-a", poc_id="poc-abc", path=art["path"],
               artifact_id=art["sha256"], entry_command="python poc.py",
               status="available", note="works on /admin", name="poc.py")
    wd = root / "workers" / "cli-2"
    wd.mkdir(parents=True)
    s = _cli_solver(ch, kb=False, shared_graph=g, workdir=str(wd))

    s._stage_attachments(wd)

    inherited = wd / "inherited" / "poc-abc" / "poc.py"
    assert inherited.is_symlink()
    assert inherited.resolve() == (root / art["path"]).resolve()
    assert g.pocs()[0]["status"] == "wip"
    assert "python poc.py" in s._poc_prompt_block()


def test_to_board_markdown_has_creds_facts_and_intents(tmp_path):
    ch, g = _real_graph(
        tmp_path,
        facts=[("cli-c", "ghost1 password PWaB1xyz works, whoami ghost1", True)],
        deadends=["port 9999 closed"])
    g.propose_intent(actor="reason", intent_id="I1", goal="probe ghost2 home dir")
    body = g.to_board_markdown()
    assert "Recovered credentials" in body
    assert "ghost1:PWaB1xyz" in body
    assert "port 9999 closed" in body                 # dead-ends rendered
    assert "Open intents" in body and "probe ghost2 home dir" in body


def test_planner_gets_untruncated_summary(tmp_path):
    # P1.5: to_summary(max_evidence=10**9) shows ALL facts (the planner was capped
    # at 16, the re-work generator was blind on a long chain).
    facts = [("cli-c", f"fact number {i} confirmed", True) for i in range(30)]
    ch, g = _real_graph(tmp_path, facts=facts)
    full = g.to_summary(max_evidence=10**9)
    assert "fact number 0 confirmed" in full          # earliest survives
    assert "fact number 29 confirmed" in full
    capped = g.to_summary()                            # default 16 鈫?early ones dropped
    assert "fact number 0 confirmed" not in capped


def test_run_loop_writes_board_file_into_worker_cwd(monkeypatch, tmp_path):
    # END-TO-END: a real CliSolver.run() loop, real SQLiteSharedGraph pre-seeded
    # with the unlock chain, explicit workdir. Assert .dswarm_board.md actually
    # lands in the worker's cwd with the credential chain 鈥?the full P1 wiring.
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult

    facts = [("cli-c", f"ghost{i} login succeeds with password PWvalue{i}; whoami ghost{i}",
              True) for i in range(20)]   # 20 > old 16-cap 鈫?proves no truncation
    ch, g = _real_graph(tmp_path, facts=facts)
    wd = _P(tempfile.mkdtemp())
    bus = _CaptureBus()
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi",
                  kb=False, shared_graph=g, workdir=str(wd))
    canned = lambda *a, **k: CliResult(text="FOUND_FLAG=NONE\n", session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    asyncio.run(s.run())

    board = wd / ".dswarm_board.md"
    assert board.exists(), "board file was not written into the worker cwd"
    body = board.read_text()
    assert "ghost0:PWvalue0" in body and "ghost19:PWvalue19" in body  # first+last, no truncation
    assert "dswarm-team-board" in body
    import shutil; shutil.rmtree(wd, ignore_errors=True)


def test_bootstrap_extracts_structured_facts(monkeypatch):
    # bug #1 fix: bootstrap workers now contribute structured facts/dead-ends to
    # the board AS THEY GO (via output markers), not just one end-of-run summary.
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    drv = _StubDriver("")
    sg = _StubGraph("")
    transcript = (
        "probing the app\n"
        "VERIFIED_FACT=session cookie is a Flask zlib blob\n"
        "DEADEND=UNION on trackingID errors out\n"
        "still working...\n"
    )
    s = CliSolver(None, ch, bus=bus, driver=drv, engine="pi", kb=False,
                  shared_graph=sg)
    canned = lambda *a, **k: CliResult(text=transcript, session="sess-x")
    monkeypatch.setattr(mod, "run_cli_streaming", canned)
    monkeypatch.setattr(mod, "run_cli", canned)
    asyncio.run(s.run())
    # the verified fact landed on the shared graph + the deck
    assert any("Flask zlib blob" in f.get("fact", "") for f in sg.facts)
    assert any("UNION on trackingID" in d.get("reason", "") for d in sg.dead_ends)
    kinds = _bb_kinds(bus.events)
    assert "fact_added" in kinds and "dead_end" in kinds


def test_cli_solver_cancel_sets_event_and_kills_procs():
    # bug #2 fix: cancel() flips the cancel flag AND force-kills any live subproc.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)

    class _FakeProc:
        def __init__(self): self.killed = False; self.pid = 999999
        def kill(self): self.killed = True

    p = _FakeProc()
    s._on_proc(p)                      # register a live subprocess
    assert not s._cancel_event.is_set()
    s.cancel()
    assert s._cancel_event.is_set()    # the streaming watcher will see this
    assert p.killed is True            # and the subprocess is killed directly


def test_on_proc_kills_immediately_if_already_cancelled():
    # race: cancel fired before the subprocess registered 鈫?kill it on register.
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    s.cancel()                          # cancel BEFORE any proc exists

    class _FakeProc:
        # pid 999999 doesn't exist, so _signal_proc's os.getpgid() raises and it
        # falls through to proc.kill() 鈥?matching the sibling test above. (pid=1 is
        # init: as root os.killpg(getpgid(1), SIGKILL) is permitted and returns early,
        # so proc.kill() is never reached and the test fails ONLY when run as root.)
        def __init__(self): self.killed = False; self.pid = 999999
        def kill(self): self.killed = True

    p = _FakeProc()
    s._on_proc(p)
    assert p.killed is True


@pytest.mark.posix
def test_run_cli_streaming_cancel_event_kills_process(tmp_path):
    # bug #2 fix at the runner level: a long-running subprocess is killed promptly
    # when the cancel_event fires mid-run (not left running until timeout).
    import threading
    import time
    from dswarm.solver.cli_driver import run_cli_streaming

    d = PiDriver()
    # a script that would run for 30s, emitting a line then sleeping
    script = tmp_path / "slow.sh"
    script.write_text('#!/bin/sh\necho \'{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\'\nsleep 30\n')
    script.chmod(0o755)

    cancel = threading.Event()
    # fire cancel shortly after start
    threading.Timer(0.5, cancel.set).start()
    t0 = time.time()
    res = run_cli_streaming(d, ["/bin/sh", str(script)], cwd=str(tmp_path),
                            timeout=30, on_step=lambda s: None, cancel_event=cancel)
    elapsed = time.time() - t0
    assert res.cancelled is True
    assert elapsed < 5  # killed promptly, NOT after the 30s sleep/timeout


def test_run_cli_streaming_bare_timeout_kills_silent_process(tmp_path):
    """Finding #4 regression: a BARE call (no cancel/steer events) with a SILENT,
    long-running process must still hit the timeout and be killed. The watcher used to
    only start when cancel_event/steer_event was present, so this call had NO timeout
    enforcement at all 鈥?and a zero-stdout process blocked `for line in proc.stdout`
    forever. Uses a real subprocess sleep (NOT mocked) and a tiny timeout."""
    import time
    from dswarm.solver.cli_driver import run_cli_streaming

    d = PiDriver()
    t0 = time.time()
    # `sleep 30` emits nothing on stdout 鈥?the only way out is the watcher's timeout.
    res = run_cli_streaming(d, ["sleep", "30"], cwd=str(tmp_path),
                            timeout=1, on_step=lambda s: None)
    elapsed = time.time() - t0
    assert res.timed_out is True, "a silent over-budget bare call must report timed_out"
    assert elapsed < 8, f"timeout must fire promptly, took {elapsed:.1f}s"


@pytest.mark.posix
def test_run_cli_streaming_does_not_hang_on_orphaned_stderr(tmp_path):
    """A CLI may spawn a background sidecar that inherits stderr after the parent
    exits. The streaming runner must not block forever in proc.stderr.read(), or the
    worker stays online and keeps its engine/profile lock."""
    import time
    from dswarm.solver.cli_driver import run_cli_streaming

    d = PiDriver()
    line = '{"type":"result","result":"FOUND_FLAG=flag{ok}","session_id":"z"}'
    script = tmp_path / "orphan_stderr.sh"
    script.write_text(
        "#!/bin/sh\n"
        "python3 - <<'PY' >/dev/null &\n"
        "import sys, time\n"
        "sys.stderr.write('sidecar still owns stderr\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(6)\n"
        "PY\n"
        f"echo '{line}'\n"
    )
    script.chmod(0o755)

    t0 = time.time()
    res = run_cli_streaming(d, ["/bin/sh", str(script)], cwd=str(tmp_path),
                            timeout=30, on_step=lambda s: None)
    elapsed = time.time() - t0

    assert "FOUND_FLAG=flag{ok}" in res.text
    assert elapsed < 3, f"stderr sidecar must not hold the worker lock ({elapsed:.1f}s)"


def test_run_cli_streaming_paused_time_excluded_from_timeout(tmp_path):
    """M7: a worker SIGSTOP-frozen by the operator must NOT be killed as timed_out
    just for being paused. With paused_event set for longer than the timeout, the
    active (unpaused) elapsed stays under budget, so the process is not timed out;
    once we clear the event, the now-running clock trips the timeout normally."""
    import threading
    import time
    from dswarm.solver.cli_driver import run_cli_streaming

    d = PiDriver()
    paused = threading.Event()
    paused.set()                       # frozen from the start
    # Hold the freeze for ~2.5s (> the 1s timeout), then release. While the event is
    # set, active_elapsed() must stay ~0 so the watcher does NOT fire.
    threading.Timer(2.5, paused.clear).start()
    t0 = time.time()
    res = run_cli_streaming(d, ["sleep", "30"], cwd=str(tmp_path),
                            timeout=1, on_step=lambda s: None, paused_event=paused)
    elapsed = time.time() - t0
    # It DID eventually time out (after the freeze lifted), but only AFTER the paused
    # window 鈥?proving the paused interval was excluded from the 1s budget.
    assert res.timed_out is True
    assert elapsed >= 2.5, (
        f"timeout fired during the freeze (elapsed {elapsed:.1f}s) 鈥?paused time was "
        "NOT excluded from the budget")
    assert elapsed < 8, f"timeout must still fire promptly after resume, took {elapsed:.1f}s"


def test_m9_owned_scratch_cleaned_on_cancel(tmp_path):
    """M9: a mkdtemp scratch dir the worker owns is removed even when the run is
    CANCELLED mid-body (the in-method rmtree only ran on the no-flag fall-through;
    cancel/exception used to skip it and leak the dir)."""
    import asyncio
    from dswarm.models.solve_graph import Challenge

    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    scratch = tmp_path / "dswarm-cli-scratch"
    scratch.mkdir()
    (scratch / "junk.txt").write_text("x")

    async def _fake_bootstrap():
        s._owned_scratch = scratch          # as the real mkdtemp branch would
        raise asyncio.CancelledError()

    s._run_bootstrap = _fake_bootstrap

    async def _go():
        try:
            await s.run()
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())
    assert not scratch.exists(), "an owned scratch dir must be cleaned on a cancelled run"


def test_m9_solved_winner_scratch_is_kept(tmp_path):
    """M9 guard: a SOLVED worker's scratch is its winner artifact (the swarm resumes
    the session from it) 鈥?it must NOT be deleted when it's the returned workdir."""
    import asyncio
    from dswarm.models.solve_graph import Challenge
    from dswarm.solver.types import SolveOutcome

    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    scratch = tmp_path / "winner-scratch"
    scratch.mkdir()

    async def _fake_bootstrap():
        s._owned_scratch = scratch
        return SolveOutcome(True, "flag{win}", 1, s.graph, "solved",
                            workdir=str(scratch))

    s._run_bootstrap = _fake_bootstrap
    asyncio.run(s.run())
    assert scratch.exists(), "a solved worker's winner scratch must be preserved"


@pytest.mark.posix
def test_on_proc_freezes_subprocess_registered_while_paused():
    """M8: if the operator paused the worker before a subprocess registered (e.g. the
    pause landed in the gap before the conclude-fallback subprocess started), _on_proc
    must SIGSTOP the new process so the pause doesn't silently leak across the turn
    boundary and let a paused worker keep running."""
    import signal as _signal
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch, kb=False)
    s._paused = True                    # operator paused before this proc exists

    sent = []

    class _FakeProc:
        def __init__(self): self.pid = 4242
        def kill(self): sent.append(_signal.SIGKILL)

    # capture whatever signal _on_proc routes through _signal_proc
    s._signal_proc = lambda p, sig: sent.append(sig)
    s._on_proc(_FakeProc())
    assert _signal.SIGSTOP in sent, "a proc registered while paused must be SIGSTOP'd"
    assert _signal.SIGKILL not in sent, "must freeze, not kill, a paused worker's proc"


@pytest.mark.posix
def test_pause_resume_via_insight_bus_signals_process(monkeypatch):
    # bug #3 fix: a HITL pause GUIDANCE on the InsightBus reaches the live worker
    # and SIGSTOPs its subprocess; resume SIGCONTs it. We capture the signals.
    from dswarm.swarm.insight_bus import InsightBus
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    bus_insight = InsightBus(challenge_id="t")
    s = _cli_solver(ch, kb=False, insight=bus_insight)
    s._insight_inbox = bus_insight.subscribe(s.solver_id)

    class _FakeProc:
        def __init__(self): self.pid = 4242
    s._live_procs.add(_FakeProc())

    signals = []
    monkeypatch.setattr("dswarm.solver.cli_solver.os.kill",
                        lambda pid, sig: signals.append((pid, sig)))

    async def drive():
        await bus_insight.guidance("", action="pause", target="global")
        s._drain_control()
        await bus_insight.guidance("", action="resume", target="global")
        s._drain_control()
    asyncio.run(drive())

    import signal as _sig
    assert (4242, _sig.SIGSTOP) in signals
    assert (4242, _sig.SIGCONT) in signals
    assert s._paused is False  # ended resumed


def test_stop_via_insight_bus_cancels_worker():
    from dswarm.swarm.insight_bus import InsightBus
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    bus_insight = InsightBus(challenge_id="t")
    s = _cli_solver(ch, kb=False, insight=bus_insight)
    s._insight_inbox = bus_insight.subscribe(s.solver_id)
    cancelled = []
    s.cancel = lambda: cancelled.append(True)

    async def drive():
        await bus_insight.guidance("", action="stop", target="global")
        s._drain_control()

    asyncio.run(drive())
    assert cancelled == [True]


def test_live_markers_stream_to_board_and_insight_bus():
    # bug #1 (full fix): a VERIFIED_FACT= seen MID-RUN is pushed to the shared graph
    # AND broadcast on the InsightBus immediately, so a racing teammate sees it now.
    from dswarm.solver.cli_driver import StreamStep
    from dswarm.swarm.insight_bus import InsightBus, InsightKind
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    sg = _StubGraph("")
    insight = InsightBus(challenge_id="t")
    teammate = insight.subscribe("cli-other")     # a racing sibling
    s = _cli_solver(ch, kb=False, shared_graph=sg, insight=insight)

    async def drive():
        # the marker arrives as a live tool-result step
        await s._emit_step(StreamStep("tool_result",
                                      text=("curl showed: admin cookie is base64 JSON\n"
                                            "VERIFIED_FACT=admin cookie is base64 JSON\n")))
        # a second identical marker (echo) must NOT double-write
        await s._emit_step(StreamStep("tool_result",
                                      text=("curl showed: admin cookie is base64 JSON\n"
                                            "VERIFIED_FACT=admin cookie is base64 JSON\n")))
    asyncio.run(drive())

    # written to the shared graph exactly once (deduped)
    assert len(sg.facts) == 1
    assert "admin cookie is base64 JSON" in sg.facts[0]["fact"]
    # and the racing teammate received it on the InsightBus as a FACT
    got = teammate.get_nowait()
    assert got.kind is InsightKind.FACT
    assert "admin cookie is base64 JSON" in got.text


def test_live_verified_fact_without_witness_is_candidate_only():
    from dswarm.solver.cli_driver import StreamStep
    from dswarm.swarm.insight_bus import InsightBus
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    sg = _StubGraph("")
    insight = InsightBus(challenge_id="t")
    teammate = insight.subscribe("cli-other")
    s = _cli_solver(ch, kb=False, shared_graph=sg, insight=insight)

    asyncio.run(s._emit_step(StreamStep(
        "tool_result", text="VERIFIED_FACT=admin cookie is base64 JSON\n")))

    assert len(sg.facts) == 1
    assert sg.facts[0]["verified"] is False
    assert teammate.empty()


def test_worker_fact_witness_metadata_does_not_replace_witness_check():
    from dswarm.solver.cli_driver import StreamStep
    from dswarm.swarm.insight_bus import InsightBus
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    sg = _StubGraph("")
    insight = InsightBus(challenge_id="t")
    teammate = insight.subscribe("cli-other")
    s = _cli_solver(ch, kb=False, shared_graph=sg, insight=insight)

    asyncio.run(s._emit_step(StreamStep(
        "tool_result",
        text=("VERIFIED_FACT=admin cookie is base64 JSON\n"
              "FACT_WITNESS=curl -i /login showed the cookie\n"))))

    assert len(sg.facts) == 1
    assert sg.facts[0]["witness"] == "curl -i /login showed the cookie"
    assert sg.facts[0]["verified"] is False
    assert teammate.empty()


def test_live_dead_end_marker_broadcasts_to_insight_bus():
    from dswarm.solver.cli_driver import StreamStep
    from dswarm.swarm.insight_bus import InsightBus, InsightKind
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    sg = _StubGraph("")
    insight = InsightBus(challenge_id="t")
    teammate = insight.subscribe("cli-other")
    s = _cli_solver(ch, kb=False, shared_graph=sg, insight=insight)

    asyncio.run(s._emit_step(StreamStep(
        "tool_result", text="DEADEND=/login is rate-limited, brute force won't work\n")))

    assert len(sg.dead_ends) == 1
    got = teammate.get_nowait()
    assert got.kind is InsightKind.DEAD_END
    assert "rate-limited" in got.text


# 鈹€鈹€ Operator steering: multi-turn loop + guidance capture + target/standing 鈹€鈹€
# (HITL fusion: hint/redirect/focus reach a live worker via the
#  next resume turn 鈥?headless CLIs can't take input mid-turn.)

def _steer_solver(**kw):
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://old")
    return _cli_solver(ch, kb=False, **kw)


def test_drain_control_nonstanding_hint_does_not_steer_live_worker():
    """A normal operator hint is additive guidance, not an interrupt. It must be
    recorded for future prompts without killing the currently running worker."""
    from dswarm.swarm.insight_bus import InsightBus
    insight = InsightBus(challenge_id="t")
    s = _steer_solver(insight=insight)
    s._insight_inbox = insight.subscribe(s.solver_id)
    s._turn_active = True   # a subprocess turn is running
    asyncio.run(insight.guidance("try /admin", action="hint", target="global"))
    s._drain_control()
    assert not s._steer_event.is_set()     # hint must not kill the current pass
    assert "try /admin" in s._standing_guidance  # recorded for the remainder


def test_drain_control_nonstanding_hint_no_steer_without_active_turn():
    """The steer-kill is gated: a hint replayed from history (no turn running yet)
    must NOT steer-kill a not-yet-started subprocess (the run-40726 regression)."""
    from dswarm.swarm.insight_bus import InsightBus
    insight = InsightBus(challenge_id="t")
    s = _steer_solver(insight=insight)
    s._insight_inbox = insight.subscribe(s.solver_id)
    s._turn_active = False  # no subprocess turn 鈫?steer must be suppressed
    asyncio.run(insight.guidance("try /admin", action="hint", target="global"))
    s._drain_control()
    assert not s._steer_event.is_set()     # gated off 鈥?no premature kill


def test_drain_control_redirect_sets_steer_event_no_buffer():
    """A non-standing redirect ENDS the current pass (intent-level kill) 鈥?gated on
    an active turn 鈥?and no longer buffers guidance (the swarm hands the new target
    to the next spawned worker)."""
    from dswarm.swarm.insight_bus import InsightBus
    insight = InsightBus(challenge_id="t")
    s = _steer_solver(insight=insight)
    s._insight_inbox = insight.subscribe(s.solver_id)
    s._turn_active = True
    asyncio.run(insight.guidance("challenge moved", action="redirect",
                                 url="http://new", target="global"))
    s._drain_control()
    assert s._steer_event.is_set()       # redirect still ends the current pass


def test_drain_control_standing_guidance_does_not_steer_live_worker():
    from dswarm.swarm.insight_bus import InsightBus
    insight = InsightBus(challenge_id="t")
    s = _steer_solver(insight=insight)
    s._insight_inbox = insight.subscribe(s.solver_id)
    s._turn_active = True   # live subprocess is running
    asyncio.run(insight.guidance("ssh root@1.2.3.4", action="hint",
                                 target="global", standing=True))
    s._drain_control()
    assert s._standing_guidance == ["ssh root@1.2.3.4"]
    # Standing guidance is background context (VPS/SSH creds, global constraints):
    # keep it for this/future prompts, but do not kill an otherwise healthy live turn.
    assert not s._steer_event.is_set()


def test_steer_event_independent_of_cancel():
    # cancel() and the steer path are distinct signals: cancel means die, steer
    # (the _steer_event the _drain_control hint/redirect branch sets) means end the
    # current pass without marking the worker dead. Neither implies the other.
    s = _steer_solver()
    s.cancel()
    assert s._cancel_event.is_set()
    assert not s._steer_event.is_set()           # cancel must not steer
    s2 = _steer_solver()
    s2._steer_event.set()
    assert s2._steer_event.is_set()
    assert not s2._cancel_event.is_set()         # steer must not cancel


def test_target_override_used_in_prompt():
    s = _steer_solver()
    assert "Target: http://old" in s._build_prompt()
    s._target_override = "http://new"
    p = s._build_prompt()
    assert "Target: http://new" in p and "http://old" not in p


def test_standing_guidance_injected_into_prompt():
    s = _steer_solver()
    s._standing_guidance = ["use VPS ssh root@1.2.3.4"]
    p = s._build_prompt()
    assert "use VPS ssh root@1.2.3.4" in p
    assert "Operator standing guidance" in p
    # explore prompt too
    s2 = _steer_solver(mode="explore", intent_goal="x")
    s2._standing_guidance = ["use VPS ssh root@1.2.3.4"]
    assert "use VPS ssh root@1.2.3.4" in s2._build_explore_prompt()


# 鈹€鈹€ SINGLE-SHOT migration (DESIGN_single_shot_migration.md, M-1) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# The worker no longer lives across turns accumulating context. The three tests
# below replace the retired multi-turn-loop tests (loops-on-guidance / respects-
# max-turns / steered-continues): a worker now runs ONE execute pass; mid-run
# operator guidance does NOT resume it (intent-level HITL 鈫?the NEXT spawned
# worker absorbs it); the ONLY second subprocess call is the conclude fallback.
def test_single_shot_buffered_guidance_does_not_resume(monkeypatch):
    """Migration: operator guidance dropped mid-run no longer resumes this live
    worker. The worker finishes its one execute pass; guidance reaches the next
    spawned worker, not a resume turn."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(driver, argv, **k):
        calls["n"] += 1
        # operator drops guidance during the execute pass 鈥?recorded for the next
        # spawned worker (single-shot), must NOT trigger a resume of THIS worker.
        s._standing_guidance.append("try /admin")
        return CliResult(text="FOUND_FLAG=flag{got_it_first_pass}\n", session="sess-x")

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 1                        # one execute pass, NO resume
    assert "flag_found" in _bb_kinds(bus.events)


def test_single_shot_one_conclude_fallback_on_timeout(monkeypatch):
    """Migration: a timeout without enough flags triggers AT MOST one conclude
    fallback (the single-shot model) 鈥?exactly two subprocess calls, never a loop."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    s._session_established = True                  # allow the conclude resume
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return CliResult(text="nothing yet\n", session="sess-x", timed_out=True)
        return CliResult(text="VERIFIED_FACT=admin panel at /admin\n", session="sess-x")

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 2                         # execute + ONE conclude, no loop


def test_single_shot_cancel_skips_conclude(monkeypatch):
    """Migration: a cancel (sibling won / stop) ends the worker immediately 鈥?no
    conclude fallback, no resume."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        # cancelled AND timed_out: cancel must win 鈫?no conclude fallback.
        return CliResult(text="killed\n", session="sess-x", cancelled=True, timed_out=True)

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 1                         # cancelled 鈫?die now, no conclude


def test_single_shot_steer_skips_conclude_and_deadend(monkeypatch):
    """A steer ends this pass so the coordinator can spawn a guided worker. It must
    not resume into CONCLUDE or record a misleading no-output dead-end."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        return CliResult(text="", session="sess-x", steered=True)

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 1
    assert "dead_end" not in _bb_kinds(bus.events)
    assert _worker_statuses(bus.events)[-1].payload["reason"] == "steered"


def test_explore_steer_skips_conclude_and_deadend(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(
        None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False,
        mode="explore", intent_goal="try redirected target")
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        return CliResult(text="", session="sess-x", steered=True)

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 1
    assert "dead_end" not in _bb_kinds(bus.events)
    assert _worker_statuses(bus.events)[-1].payload["reason"] == "steered"


def test_m4_no_flag_worker_exits_clean_without_deadend(monkeypatch):
    """M-4 (never-give-up at swarm layer): a single-shot worker that finds no flag
    does NOT keep retrying or rationalize a false 'solved' 鈥?it exits cleanly after
    its one pass, returns unsolved, and concludes a DEAD-END for the board. 'Give
    up' is now a clean swarm-level decision (re-bootstrap), not worker self-hypnosis."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        # honest "couldn't crack it" 鈥?NO flag, NO false-solved claim.
        return CliResult(text="probed /admin, no auth bypass found\n", session="sess-x")

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    outcome = asyncio.run(s.run())
    assert calls["n"] == 1                         # one pass, no retry loop
    assert outcome.solved is False                 # honest: not solved
    kinds = _bb_kinds(bus.events)
    assert "dead_end" not in kinds                 # no explicit DEADEND= marker
    concl = [e for e in bus.events
             if e.event_type is EventType.BLACKBOARD_DELTA
             and e.payload.get("kind") == "intent_concluded"][0]
    assert concl.payload.get("result") == "explored"
    assert "flag_found" not in kinds               # never a false-solved claim


def test_cancelled_turn_does_not_resume(monkeypatch):
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    calls = {"n": 0}

    def fake_stream(*a, **k):
        calls["n"] += 1
        return CliResult(text="killed\n", session="sess-x", cancelled=True)

    monkeypatch.setattr(mod, "run_cli_streaming", fake_stream)
    monkeypatch.setattr(mod, "run_cli", fake_stream)
    asyncio.run(s.run())
    assert calls["n"] == 1                        # cancelled 鈫?no resume


def test_hitl_cmd_url_sets_target_override():
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://old")
    s = _cli_solver(ch, kb=False, mode="respond", resume_session="sess",
                    hitl_cmd={"action": "redirect", "url": "http://new"})
    assert s._target_override == "http://new"
    assert "Target: http://new" in s._build_prompt()


def test_run_cli_streaming_steer_event_kills_and_flags():
    import threading
    import time as _t
    from dswarm.solver.cli_driver import run_cli_streaming, get_driver
    drv = get_driver("pi")
    steer = threading.Event()

    def fire():
        _t.sleep(0.3); steer.set()
    threading.Thread(target=fire, daemon=True).start()
    t0 = _t.time()
    res = run_cli_streaming(drv, ["sleep", "10"], cwd="/tmp", timeout=30,
                            on_step=lambda s: None, steer_event=steer)
    assert res.steered is True
    assert res.cancelled is False
    assert _t.time() - t0 < 5                     # killed promptly, not at timeout


# 鈹€鈹€ _extract_flag must not surface placeholders (run-1619 false-positive) 鈹€鈹€鈹€鈹€鈹€
def test_extract_flag_skips_placeholder_in_prose():
    ch = Challenge(id="t", name="t", category="web",
                   flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
    s = _cli_solver(ch)
    # worker mentions the template in prose but never recovered a flag
    text = "The /admin HTML did not contain flag{...}. scanning for flag{...} more."
    assert s._extract_flag(text) is None


def test_extract_flag_picks_real_flag_over_placeholder():
    ch = Challenge(id="t", name="t", category="web",
                   flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
    s = _cli_solver(ch)
    # placeholder appears first, the real flag later 鈥?must return the real one
    text = "looking for flag{...}\n...\nFOUND_FLAG=dalctf{r3al_one_h3re}"
    assert s._extract_flag(text) == "dalctf{r3al_one_h3re}"


def test_extract_flag_rejects_found_flag_marker_placeholder():
    ch = Challenge(id="t", name="t", category="web",
                   flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
    s = _cli_solver(ch)
    # even an explicit FOUND_FLAG= marker is rejected if it's a placeholder
    assert s._extract_flag("FOUND_FLAG=flag{...}") is None
    assert s._extract_flag("FOUND_FLAG=<flag>") is None


def test_extract_flag_rejects_found_flag_marker_code_expression():
    ch = Challenge(id="t", name="t", category="web",
                   flag_format=r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
    s = _cli_solver(ch)
    text = 'print(f"FOUND_FLAG={out3[i:j].decode()}")'
    assert s._extract_flag(text) is None
    assert s._extract_flags(text) == []


def test_extract_need_inputs_parses_marker():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    text = (
        "trying reverse shell...\n"
        "NEED_INPUT=a public VPS I can SSH to (I'm behind NAT)\n"
        "VERIFIED_FACT=target is behind NAT\n"
    )
    needs = s._extract_need_inputs(text)
    assert needs == ["a public VPS I can SSH to (I'm behind NAT)"]


def test_extract_need_request_preserves_worker_reported_kind():
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}")
    s = _cli_solver(ch)
    text = (
        "NEED_INPUT=I need the operator to pick between two approaches\n"
        "NEED_KIND=operator_directive_needed\n"
    )
    assert s._extract_need_requests(text) == [
        ("I need the operator to pick between two approaches",
         "operator_directive_needed")
    ]


def test_stream_markers_uses_worker_reported_need_kind():
    import asyncio
    from dswarm.core.events import EventType
    bus = _CaptureBus()
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    asyncio.run(s._stream_markers(
        "NEED_INPUT=I need the operator to decide whether to burn the exploit\n"
        "NEED_KIND=operator_directive_needed\n"))
    reqs = [e for e in bus.events if e.event_type is EventType.HITL_REQUEST]
    assert reqs[0].payload.get("need_kind") == "operator_directive_needed"


def test_stream_markers_emits_hitl_request_on_need_input():
    """A NEED_INPUT= marker must surface a HITL_REQUEST event (the worker raising
    its hand) + a need_input blackboard delta 鈥?the dead HITL_REQUEST path is now
    wired."""
    import asyncio
    from dswarm.core.events import EventType
    bus = _CaptureBus()
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    asyncio.run(s._stream_markers(
        "NEED_INPUT=the target is connection-refused, instance may be expired\n"))
    reqs = [e for e in bus.events if e.event_type is EventType.HITL_REQUEST]
    assert len(reqs) == 1
    assert "connection-refused" in reqs[0].payload.get("need", "")
    # env-flavored need is classified env_down for the deck label
    assert reqs[0].payload.get("kind") == "env_down"
    assert reqs[0].payload.get("need_kind") == "external_blocker"
    # also dropped a board marker the coordinator polls
    assert "need_input" in _bb_kinds(bus.events)


def test_need_kind_field_is_separate_from_legacy_kind():
    import asyncio
    from dswarm.core.events import EventType
    bus = _CaptureBus()
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    asyncio.run(s._stream_markers(
        "NEED_INPUT=need exclusive access; another worker is hammering the same target\n"))
    reqs = [e for e in bus.events if e.event_type is EventType.HITL_REQUEST]
    assert reqs[0].payload.get("kind") == "need_input"
    assert reqs[0].payload.get("need_kind") == "lane_lock_request"


def test_need_kind_routes_dead_end_separately():
    import asyncio
    from dswarm.core.events import EventType
    bus = _CaptureBus()
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = CliSolver(None, ch, bus=bus, driver=_StubDriver(""), engine="pi", kb=False)
    asyncio.run(s._stream_markers(
        "NEED_INPUT=this exploit route is a dead end after repeated failures\n"))
    reqs = [e for e in bus.events if e.event_type is EventType.HITL_REQUEST]
    assert reqs[0].payload.get("kind") == "need_input"
    assert reqs[0].payload.get("need_kind") == "route_dead_end"


def test_need_input_in_worker_prompts():
    """Both the bootstrap and explore prompts must teach the worker the NEED_INPUT
    escape hatch."""
    ch = Challenge(id="t", name="t", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    s = _cli_solver(ch)
    assert "NEED_INPUT=" in s._build_prompt()
    s2 = _cli_solver(ch)
    s2.mode = "explore"; s2.intent_goal = "probe"
    assert "NEED_INPUT=" in s2._build_explore_prompt()


def test_review_mode_parses_actions_but_never_accepts_flags(monkeypatch, tmp_path):
    """Review-Arbiter is a control worker: it may emit route/fact/intent actions,
    but it must not solve the run even if its transcript contains FOUND_FLAG."""
    from dswarm.solver import cli_solver as mod
    from dswarm.solver.cli_driver import CliResult
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    bus = _CaptureBus()
    ch = Challenge(id="t", name="login", category="web", flag_format=r"flag\{.*?\}",
                   target="http://x")
    sg = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=ch)
    fseq = sg.add_evidence(actor="cli-a", source="a", fact="JWT likely HS256",
                           verified=True, artifact_id="a1")
    transcript = (
        'REVIEW_FINDING={"kind":"route_loop","severity":"blocker",'
        '"summary":"login SQLi repeated","route_hash":"web:login:sqli"}\n'
        f'FACT_CHALLENGE={{"fact_seq":{fseq},"reason":"no header proof",'
        '"verification_goal":"Decode a real JWT header from captured output."}\n'
        'ROUTE_SUPPRESS={"route_hash":"web:login:sqli","label":"login SQLi",'
        '"reason":"three repeats","matching_intents":[]}\n'
        'NEXT_INTENT={"worker_class":"verifier","goal":"Verify JWT alg from real token"}\n'
        'FOUND_FLAG=flag{review_must_not_win}\n'
    )
    s = CliSolver(None, ch, bus=bus, shared_graph=sg,
                  driver=_StubDriver(transcript), engine="pi", kb=False,
                  mode="review")
    monkeypatch.setattr(
        mod, "run_cli_streaming",
        lambda *a, **k: CliResult(text=transcript, session="sess-review"))

    out = asyncio.run(s.run())

    assert out.solved is False
    assert sg.snapshot().flag is None
    kinds = [e["kind"] for e in sg.events()]
    assert "review_proposal" in kinds
    assert "review_finding" not in kinds
    assert "fact_challenged" not in kinds
    assert "route_suppressed" not in kinds
    bb = _bb_kinds(bus.events)
    assert "review_proposal" in bb



def test_candidate_fact_does_not_replace_verified_conclusion_pointer(tmp_path):
    """A late marker-only candidate must not become a solved intent's evidence."""
    import sqlite3

    ch, graph = _real_graph(tmp_path)
    solver = _cli_solver(ch, kb=False, shared_graph=graph)
    solver._intent_id = "I-proof"
    graph.propose_intent(actor="reason", intent_id="I-proof", goal="recover flag")
    assert graph.claim_intent(worker=solver.solver_id, intent_id="I-proof")

    verified_seq = asyncio.run(solver._record_fact(
        "server command output exposes the FLAG environment variable",
        verified=True, artifact_id="artifact-real",
    ))
    candidate_seq = asyncio.run(solver._record_fact(
        "FLAG env var on server = flag{claimed} verified twice",
        verified=False, artifact_id="artifact-marker-only",
    ))
    assert candidate_seq > verified_seq > 0
    assert solver._last_fact_seq == verified_seq

    solver._conclude_intent_db(
        result="solved", to_fact_seq=solver._last_fact_seq,
        result_detail="Verified flag accepted.",
    )
    with sqlite3.connect(graph.db_path) as conn:
        row = conn.execute(
            "SELECT to_fact_seq FROM intents WHERE intent_id='I-proof'"
        ).fetchone()
    assert row == (verified_seq,)
    graph.close()


def test_bootstrap_intent_lease_covers_execute_and_conclude_timeout(tmp_path):
    import sqlite3
    import time

    ch, graph = _real_graph(tmp_path)
    solver = _cli_solver(
        ch, kb=False, shared_graph=graph, timeout=900, conclude_timeout=120,
    )
    solver._intent_id = "intent:long-worker"
    before = time.time()
    solver._record_intent_db("solve the whole challenge")

    with sqlite3.connect(graph.db_path) as conn:
        worker, lease_until = conn.execute(
            "SELECT worker, lease_until FROM intents WHERE intent_id=?",
            (solver._intent_id,),
        ).fetchone()
    assert worker == solver.solver_id
    assert lease_until >= before + 900 + 120 + 55
    graph.close()


class _ExplodingFlagGraph:
    """Shared-graph stand-in whose flag writes always fail."""

    def invalidated_flags(self):
        return set()

    def flag_found(self, **kw):
        raise RuntimeError("flag graph down")


async def test_flag_db_failure_is_surfaced_once_without_rejecting_real_flag():
    ch = Challenge(id="t", name="t", category="pwn", flag_format=r"flag\{.*?\}")
    solver = _cli_solver(ch, kb=False, shared_graph=_ExplodingFlagGraph())
    deltas: list[tuple[str, dict]] = []

    async def fake_emit_bb(kind, **fields):
        deltas.append((kind, fields))

    solver._emit_bb = fake_emit_bb
    solver._intent_id = "I-flag"

    assert await solver._accept_flag("flag{real}") is True
    await asyncio.sleep(0)
    # A duplicate is a no-op and therefore cannot create a second failure note.
    assert await solver._accept_flag("flag{real}") is False
    await asyncio.sleep(0)

    failures = [(kind, fields) for kind, fields in deltas
                if kind == "flag_db_write_failed"]
    assert len(failures) == 1
    payload = failures[0][1]
    assert payload["flag"] == "flag{real}"
    assert payload["intent_id"] == "I-flag"
    assert payload["op"] == "flag_found"
    assert payload["reason"] == "RuntimeError"
    assert "flag graph down" not in repr(payload)
    assert solver._flag_db_failures_noted == {"flag{real}"}


class _ExplodingFactGraph:
    """Shared-graph stand-in whose evidence writes always fail."""

    def add_evidence(self, **kw):
        raise RuntimeError("fact graph down")


async def test_fact_db_failure_is_surfaced_once_without_leaking_fact():
    ch = Challenge(id="t", name="t", category="pwn", flag_format=r"flag\{.*?\}")
    solver = _cli_solver(ch, kb=False, shared_graph=_ExplodingFactGraph())
    deltas: list[tuple[str, dict]] = []

    async def fake_emit_bb(kind, **fields):
        deltas.append((kind, fields))

    solver._emit_bb = fake_emit_bb
    solver._intent_id = "I-fact"
    fact = "secret evidence that must not be copied into telemetry"

    assert await solver._record_fact(
        fact, verified=False, artifact_id="", witness="tool output"
    ) == -1
    assert await solver._record_fact(
        fact, verified=False, artifact_id="", witness="tool output"
    ) == -1
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    failures = [(kind, fields) for kind, fields in deltas
                if kind == "fact_db_write_failed"]
    assert len(failures) == 1
    payload = failures[0][1]
    assert payload["fact_digest"] == hashlib.sha256(fact.encode()).hexdigest()
    assert payload["fact_length"] == len(fact)
    assert payload["intent_id"] == "I-fact"
    assert payload["op"] == "add_evidence"
    assert payload["reason"] == "RuntimeError"
    assert "fact graph down" not in repr(payload)
    assert fact not in repr(payload)
    assert len(solver._fact_db_failures_noted) == 1


async def test_poc_and_review_db_failures_are_surfaced_once_without_payloads():
    ch = Challenge(id="t", name="t", category="pwn", flag_format=r"flag\{.*?\}")
    solver = _cli_solver(ch, kb=False, shared_graph=object())
    deltas: list[tuple[str, dict]] = []

    async def fake_emit_bb(kind, **fields):
        deltas.append((kind, fields))

    solver._emit_bb = fake_emit_bb
    solver._intent_id = "I-side-effect"
    exc = RuntimeError("graph outage with private=/tmp/secret")

    solver._note_poc_db_failure("poc-123", "save_poc", exc)
    solver._note_poc_db_failure("poc-123", "save_poc", exc)
    solver._note_review_db_failure("REVIEW_FINDING", "add_review_proposal", exc)
    solver._note_review_db_failure("REVIEW_FINDING", "add_review_proposal", exc)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    by_kind = {kind: [fields for k, fields in deltas if k == kind]
               for kind in {k for k, _ in deltas}}
    assert len(by_kind["poc_db_write_failed"]) == 1
    assert len(by_kind["review_db_write_failed"]) == 1
    assert by_kind["poc_db_write_failed"][0]["poc_id"] == "poc-123"
    assert by_kind["review_db_write_failed"][0]["marker"] == "REVIEW_FINDING"
    for fields in (by_kind["poc_db_write_failed"][0],
                   by_kind["review_db_write_failed"][0]):
        assert fields["intent_id"] == "I-side-effect"
        assert fields["reason"] == "RuntimeError"
        assert "/tmp/secret" not in repr(fields)
        assert "payload" not in fields


class _ExplodingIntentGraph:
    """Shared-graph stand-in whose intent writes always fail."""

    db_path = "/tmp/none.db"

    def propose_intent(self, **kw):
        raise RuntimeError("graph down")

    def conclude_intent(self, **kw):
        raise RuntimeError("graph down")


async def test_intent_db_failure_is_surfaced_once_without_disturbing_solve():
    ch = Challenge(id="t", name="t", category="pwn", flag_format=r"flag\{.*?\}")
    solver = _cli_solver(ch, kb=False, shared_graph=_ExplodingIntentGraph())
    deltas: list[tuple[str, dict]] = []

    async def fake_emit_bb(kind, **fields):
        deltas.append((kind, fields))

    solver._emit_bb = fake_emit_bb
    solver._intent_id = "I-x"

    # A failed propose must not raise — best-effort stays best-effort.
    solver._record_intent_db("solve the whole challenge")
    await asyncio.sleep(0)  # let the fire-and-forget task run
    solver._conclude_intent_db(result="explored")  # second failure: deduped
    await asyncio.sleep(0)

    kinds = [k for k, _ in deltas]
    assert kinds.count("intent_db_write_failed") == 1
    payload = next(fields for k, fields in deltas if k == "intent_db_write_failed")
    assert payload["intent_id"] == "I-x"
    assert payload["op"] == "propose"
    assert payload["reason"] == "RuntimeError"
    assert "graph down" not in repr(payload)
    # The bound failure note never leaks unbounded per-op noise across intents.
    assert len(solver._intent_db_failures_noted) == 1


def test_intent_db_failure_degrades_to_silence_without_running_loop():
    ch = Challenge(id="t", name="t", category="pwn", flag_format=r"flag\{.*?\}")
    solver = _cli_solver(ch, kb=False, shared_graph=_ExplodingIntentGraph())
    solver._intent_id = "I-sync"

    # No running loop here (sync test): the RuntimeDegradationMixin contract is
    # to degrade to silence rather than raise or spin a new loop.
    solver._record_intent_db("solve the whole challenge")
    solver._conclude_intent_db(result="explored")
    assert solver._intent_db_failures_noted == {"I-sync"}


def test_extract_closing_prose_prefers_assistant_words_over_envelopes():
    """run-75380: the closing summary fact was a raw pi agent_end envelope —
    the whole conversation snapshot passed the stream-delta filter because it
    carries prose (the harness CONCLUDE directive). The extractor must return
    assistant-authored words only."""
    from dswarm.solver.cli_stream import extract_closing_prose

    conclude_only = (
        '{"type":"agent_end","messages":[{"role":"user","content":'
        '[{"type":"text","text":"CONCLUDE: stop exploring NOW. Do not"}]}]}'
    )
    # harness directive is NOT worker words -> empty (caller writes "(no output)")
    assert extract_closing_prose(conclude_only) == ""

    with_assistant = (
        '{"type":"agent_end","messages":['
        '{"role":"user","content":[{"type":"text","text":"task"}]},'
        '{"role":"assistant","content":[{"type":"text","text":"Found Werkzeug debugger on :8000"}]}]}'
    )
    assert extract_closing_prose(with_assistant) == "Found Werkzeug debugger on :8000"
    # plain prose lines still win, bottom-up
    assert extract_closing_prose(with_assistant + "\nplain output line") == "plain output line"
    # unknown json / non-json handled as before
    assert extract_closing_prose("") == ""
    assert extract_closing_prose('{"type":"tool_execution_end","id":1}') == ""


# ── _run_streaming routing: pool workers must stream (reasoning/tool steps) ───
# Regression for the M9a-era short-circuit: a leased pool executor went through
# plain container.run() with NO on_step, so every pool-dispatched worker ran
# with zero live reasoning/tool events (run-6038: 16 real minutes, 41 model
# calls, 4 reasoning deltas, 0 tool events).

class _FakePoolExecutor(ContainerRuntimeExecutor):
    """Duck executor recording which run entrypoint _run_streaming chose."""

    def __init__(self):  # skip the real pool wiring — the routing is what we test
        self.calls = []

    async def run(self, driver, argv, **kw):
        self.calls.append(("plain_run", kw))
        return CliResult(text="done")

    async def run_streaming(self, driver, argv, **kw):
        self.calls.append(("run_streaming", kw))
        on_step = kw.get("on_step")
        if on_step:
            on_step(StreamStep("reasoning", text="live thought"))
        return CliResult(text="done")


def _stream_routing_solver(bus):
    s = _cli_solver(Challenge(id="t", name="t", category="web"), bus=bus)
    ex = _FakePoolExecutor()
    s.container = ex
    return s, ex


@pytest.mark.parametrize("with_bus", [True, False])
def test_run_streaming_executor_routes_by_bus(monkeypatch, with_bus):
    """With a bus, a leased pool executor MUST take the streaming run (on_step +
    cancel/steer wired) so the deck sees live reasoning/tool output; without a
    bus there is nothing to stream to and the plain run stays."""
    from dswarm.solver import cli_solver as cs_mod

    s, ex = _stream_routing_solver(_CaptureBus() if with_bus else None)
    monkeypatch.setattr(cs_mod, "_WORKER_HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(type(s), "_worker_env", lambda self: {})
    monkeypatch.setattr(type(s), "_apply_runtime_argv",
                        lambda self, argv, env: list(argv))

    res = asyncio.run(s._run_streaming(["pi"], cwd=".", timeout=5))

    assert res.text == "done"
    chosen = [name for name, _ in ex.calls]
    if with_bus:
        assert chosen == ["run_streaming"]
        kw = ex.calls[0][1]
        assert callable(kw["on_step"])
        assert kw["cancel_event"] is s._cancel_event
        assert kw["steer_event"] is s._steer_event
    else:
        assert chosen == ["plain_run"]
        assert "on_step" not in ex.calls[0][1]


# ── missing-attachment prompt guard (run-6427) ────────────────────────────────

def test_missing_attachment_guard_warns_when_declared_but_absent():
    """A brief that declares 附件 while none staged must tell the worker the
    files are missing — the wander that followed is how a run scraped other
    runs' flags off the control-plane API instead of solving."""
    ch = Challenge(id="t", name="t", category="reverse",
                   description="- 题目类型：Reverse\n- 附件：re1，大小约 87.5 KB")
    s = _cli_solver(ch)
    s._staged_files = []
    prompt = s._build_prompt()
    assert "NO attachment files were staged" in prompt
    assert "Do NOT fabricate" in prompt


def test_missing_attachment_guard_silent_when_files_staged():
    ch = Challenge(id="t", name="t", category="reverse",
                   description="- 附件：re1")
    s = _cli_solver(ch)
    s._staged_files = ["/home/kali/workspace/inputs/by-name/re1"]
    assert "NO attachment files were staged" not in s._build_prompt()


def test_missing_attachment_guard_silent_without_attachment_mention():
    ch = Challenge(id="t", name="t", category="web",
                   description="attack http://target/ and capture the flag")
    s = _cli_solver(ch)
    s._staged_files = []
    assert "NO attachment files were staged" not in s._build_prompt()
