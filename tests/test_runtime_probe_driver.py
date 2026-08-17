from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

import dswarm.solver.cli_driver as cli_driver
from dswarm.solver.cli_driver import (
    CliDriver,
    CliProbeResult,
    CliProbeSpec,
    EndpointDriver,
    PiDriver,
    ProfileDriver,
    ProbeContractError,
)


class UnsafeDriver(CliDriver):
    name = "unsafe"

    def build_execute(self, prompt, session, *, web_access=True, kb_access=True, stream=False):
        return ["unsafe", prompt]

    def build_resume(self, prompt, session, *, web_access=True, kb_access=True, stream=False):
        return ["unsafe", prompt]

    def parse(self, stdout, stderr):
        raise NotImplementedError


def _completed_stdout(*, text: str = "OK", usage: dict | None = None) -> str:
    event = {
        "type": "agent_end",
        "messages": [{"role": "assistant", "text": text}],
    }
    if usage is not None:
        event["usage"] = usage
    return "\n".join([
        json.dumps({"type": "session", "id": "probe-session"}),
        json.dumps(event),
        json.dumps({"type": "agent_settled"}),
    ])


def test_pi_probe_spec_is_real_one_turn_and_explicitly_disables_every_builtin_tool():
    spec = PiDriver().probe_spec(model="deepseek-chat", session_dir="/private/probe/session")

    assert isinstance(spec, CliProbeSpec)
    assert spec.argv[:3] == ("pi", "--mode", "json")
    assert spec.prompt == "Reply with exactly: OK"
    assert spec.non_agentic is True
    assert spec.requires_closed_stdin is True
    assert spec.session_dir == "/private/probe/session"
    assert spec.max_output_bytes > 0
    denied = set(spec.disabled_tools)
    assert {"read", "bash", "edit", "write", "grep", "find", "ls"} <= denied
    assert {"WebSearch", "WebFetch"} <= denied
    assert "--exclude-tools" in spec.argv
    assert ".pi-sessions" not in spec.argv
    assert "--session" not in spec.argv
    assert spec.argv.count(spec.prompt) == 1
    assert spec.argv[spec.argv.index("--exclude-tools") + 1].split(",") == list(spec.disabled_tools)


def test_probe_spec_does_not_resolve_host_binary(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("host binary resolution must not run for a container probe")

    monkeypatch.setattr(cli_driver, "resolve_engine_bin", fail)
    spec = PiDriver().probe_spec(model="m", session_dir="/container/probe")
    assert spec.argv[0] == "pi"


def test_probe_spec_includes_configured_kb_mcp_prefix(monkeypatch):
    monkeypatch.setenv("DSWARM_KB_MCP_NAME", "security-index")
    monkeypatch.setattr(PiDriver, "KB_TOOL_PREFIX", "mcp__security-index")
    spec = PiDriver().probe_spec(model="m", session_dir="/probe")
    assert "mcp__security-index" in spec.disabled_tools
    assert "mcp__security-index" in spec.argv[spec.argv.index("--exclude-tools") + 1]


def test_probe_spec_rejects_invalid_model_and_session_dir():
    driver = PiDriver()
    with pytest.raises(ValueError, match="model"):
        driver.probe_spec(model="", session_dir="/probe")
    with pytest.raises(ValueError, match="session_dir"):
        driver.probe_spec(model="m", session_dir="")
    with pytest.raises(ValueError, match="session_dir"):
        driver.probe_spec(model="m", session_dir="relative/session")


def test_driver_without_a_provable_tool_disabled_mode_is_rejected():
    with pytest.raises(ProbeContractError, match="tool_disabled_unprovable"):
        UnsafeDriver().probe_spec(model="m", session_dir="/tmp/s")


def test_probe_spec_is_frozen():
    spec = PiDriver().probe_spec(model="m", session_dir="/probe")
    with pytest.raises(FrozenInstanceError):
        spec.prompt = "changed"


def test_profile_driver_probe_uses_profile_model_and_delegates_to_base():
    driver = ProfileDriver(PiDriver(), {"model": "profile-model"})
    spec = driver.probe_spec(model="caller-model", session_dir="/probe")
    assert spec.model == "profile-model"
    assert "--model" in spec.argv
    assert spec.argv[spec.argv.index("--model") + 1] == "profile-model"


def test_endpoint_driver_keeps_the_same_safe_probe_contract():
    driver = EndpointDriver(PiDriver(), {"model": "endpoint-model", "base_url": "https://example.test"})
    spec = driver.probe_spec(model="caller-model", session_dir="/probe")
    assert spec.model == "endpoint-model"
    assert spec.argv[0:3] == ("pi", "--mode", "json")
    assert set(("read", "bash", "edit", "write", "grep", "find", "ls")) <= set(spec.disabled_tools)


def test_parse_probe_result_accepts_only_completed_model_turn_and_returns_usage_separately():
    result = PiDriver().parse_probe_result(
        _completed_stdout(usage={"input_tokens": 11, "output_tokens": 7}),
        "",
        0,
    )
    assert isinstance(result, CliProbeResult)
    assert result.ok is True
    assert result.classification == "success"
    assert result.code == "completed"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.diagnostics == ""
    assert "OK" not in result.diagnostics

    incomplete = PiDriver().parse_probe_result('{"type":"agent_start"}\n', "", 0)
    assert incomplete.ok is False
    assert incomplete.code == "incomplete_turn"


def test_parse_probe_result_rejects_empty_completed_reply_without_leaking_content():
    result = PiDriver().parse_probe_result(_completed_stdout(text=""), "secret prompt and credential", 0)
    assert result.ok is False
    assert result.classification == "empty_reply"
    assert result.code == "empty_reply"
    assert len(result.diagnostics) <= 160
    assert "secret" not in result.diagnostics
    assert "prompt" not in result.diagnostics


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "classification", "code"),
    [
        ('{"type":"turn.failed","error":{"message":"401 invalid api key"}}', "", 1, "auth", "auth_failed"),
        ('{"type":"turn.failed","error":{"message":"model not found"}}', "", 1, "model_config", "model_or_config_failed"),
        ("", "request timed out", 124, "timeout", "timeout"),
        ("", "connection reset by peer", 1, "transport", "transport_error"),
        ("", "permission denied", 1, "non_zero_exit", "nonzero_exit"),
    ],
)
def test_parse_probe_result_classifies_failures_without_raw_diagnostics(
    stdout, stderr, returncode, classification, code
):
    result = PiDriver().parse_probe_result(stdout, stderr, returncode)
    assert result.ok is False
    assert result.classification == classification
    assert result.code == code
    assert len(result.diagnostics) <= 160
    for secret in ("401 invalid api key", "model not found", "request timed out", "connection reset by peer", "permission denied"):
        assert secret not in result.diagnostics


def test_parse_probe_result_is_bounded_and_rejects_non_finite_usage():
    huge = "x" * 100_000
    result = PiDriver().parse_probe_result(_completed_stdout(text=huge), "", 0)
    assert result.ok is True
    assert result.diagnostics == ""

    bad_usage = _completed_stdout(usage={"input_tokens": -1, "output_tokens": 3})
    result = PiDriver().parse_probe_result(bad_usage, "", 0)
    assert result.ok is False
    assert result.code == "invalid_usage"
    assert result.classification == "protocol"


def test_health_detail_keeps_legacy_behavior_separate_from_probe_contract(monkeypatch):
    called = []

    def fake_run(*args, **kwargs):
        called.append(args[0])
        return type("Completed", (), {"returncode": 0, "stdout": "version", "stderr": ""})()

    monkeypatch.setattr(cli_driver.subprocess, "run", fake_run)
    driver = UnsafeDriver()
    assert driver.health_detail()[0] is True
    assert called and called[0][-1] == "--version"
