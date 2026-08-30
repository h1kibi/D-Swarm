from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimeResourceSpec,
)
from dswarm.core.usage_journal import UsageCall, UsageRecord


@dataclass
class FakeProjection:
    env: dict[str, str]
    binding_id: str = "binding-1"
    credential_version_digest: str = "cred-v1"


class AllowBudget:
    def authorize(self, *, profile_id, account_id):
        return SimpleNamespace(allowed=True, reason=None)


class DenyBudget:
    def __init__(self, reason="budget_cap:profile:p1"):
        self.reason = reason

    def authorize(self, *, profile_id, account_id):
        return SimpleNamespace(allowed=False, reason=self.reason)


class OrderedWriter:
    def __init__(self, calls):
        self.calls = calls
        self.started = None
        self.finished = None

    async def start(self, *, context=None, provider_call_id=None):
        self.calls.append("usage_started")
        self.started = UsageCall(
            provider_call_id=provider_call_id or "probe-call",
            producer="internal",
            run_id="run-1",
            challenge_id=None,
            worker_instance_id=(context.worker_instance_id if context else None),
            solver_id="runtime-probe",
            profile_id="p1",
            configured_account_id="acct-1",
            billing_account_id="acct-1",
        )
        return self.started

    async def finish(self, call, *, call_outcome, usage_status, usage=None):
        self.calls.append("usage_finished")
        self.finished = SimpleNamespace(
            call=call,
            call_outcome=call_outcome,
            usage_status=usage_status,
            usage=usage or {},
            operation_kind="runtime_probe",
        )
        return self.finished


class FailingStartWriter:
    async def start(self, **kwargs):
        raise RuntimeError("accounting_unavailable")


class FakeExecutor:
    def __init__(self, calls, *, reply="OK", input_tokens=11, output_tokens=7, error=None, delay=0):
        self.calls = calls
        self.reply = reply
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.error = error
        self.delay = delay
        self.last_request = None
        self.run_id = "run-1"
        self.pool_instance_id = "instance-1"
        self.run_root = Path(".")

    async def run(self, driver, argv, **kwargs):
        self.calls.append("provider_request")
        self.last_request = {"argv": list(argv), "kwargs": kwargs}
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        stdout = "\n".join(
            [
                json.dumps({"type": "session", "id": "s"}),
                json.dumps(
                    {
                        "type": "agent_end",
                        "messages": [{"role": "assistant", "text": self.reply}],
                        "usage": {
                            "input_tokens": self.input_tokens,
                            "output_tokens": self.output_tokens,
                        },
                    }
                ),
                json.dumps({"type": "agent_settled"}),
            ]
        )
        return SimpleNamespace(
            text=stdout,
            raw_stderr="",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            timed_out=False,
            cancelled=False,
            runtime_status={"rc": 0},
        )


def pool_spec() -> PoolSpec:
    return PoolSpec.with_computed_id(
        profile_id="p1",
        runtime_kind="pi",
        resolved_image_id="sha256:image-1",
        requested_image_ref="dswarm/pi:latest",
        network=RuntimeNetworkSpec("none"),
        resources=RuntimeResourceSpec("1", "512MiB", 128, 1024),
        credential_binding_id="binding-1",
        provider_binding_id="provider-1",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("kali",),
        protocol_version=2,
        pool_max_concurrent_workers=1,
    )


def projection() -> FakeProjection:
    return FakeProjection(env={"SAFE_PROBE_ENV": "1"})


@pytest.mark.asyncio
async def test_probe_is_accounted_before_upstream_and_never_receives_challenge_inputs():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    writer = OrderedWriter(calls)
    executor = FakeExecutor(calls)
    result = await RuntimeProbe(usage_writer=writer, budget_gate=AllowBudget()).run(
        executor=executor,
        pool_spec=pool_spec(),
        credential_projection=projection(),
        generation=1,
        timeout=5,
    )
    assert calls[:2] == ["usage_started", "provider_request"]
    assert result.ready is True
    serialized = json.dumps(executor.last_request)
    for forbidden in ("DSWARM_BLACKBOARD_DB", "challenge", "target", "player_files", "FOUND_FLAG"):
        assert forbidden not in serialized
    assert writer.finished.operation_kind == "runtime_probe"
    assert writer.finished.usage_status == "measured"


@pytest.mark.asyncio
async def test_accounting_start_failure_makes_zero_upstream_calls():
    from dswarm.solver.runtime_probe import RuntimeProbe, RuntimeProbeError

    executor = FakeExecutor([])
    with pytest.raises(RuntimeProbeError, match="accounting_unavailable"):
        await RuntimeProbe(usage_writer=FailingStartWriter(), budget_gate=AllowBudget()).run(
            executor=executor,
            pool_spec=pool_spec(),
            credential_projection=projection(),
            generation=1,
            timeout=5,
        )
    assert executor.calls == []


@pytest.mark.asyncio
async def test_budget_gate_rejects_before_provider_call():
    from dswarm.solver.runtime_probe import RuntimeProbe, RuntimeProbeError

    executor = FakeExecutor([])
    with pytest.raises(RuntimeProbeError, match="budget_cap"):
        await RuntimeProbe(usage_writer=OrderedWriter([]), budget_gate=DenyBudget()).run(
            executor=executor,
            pool_spec=pool_spec(),
            credential_projection=projection(),
            generation=1,
            timeout=5,
        )
    assert executor.calls == []


@pytest.mark.asyncio
async def test_probe_unknown_usage_never_becomes_zero():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    writer = OrderedWriter(calls)
    executor = FakeExecutor(calls, input_tokens=None, output_tokens=None)
    await RuntimeProbe(usage_writer=writer, budget_gate=AllowBudget()).run(
        executor=executor,
        pool_spec=pool_spec(),
        credential_projection=projection(),
        generation=1,
        timeout=5,
    )
    assert writer.finished.usage_status == "unknown"
    assert writer.finished.usage == {}


@pytest.mark.asyncio
async def test_probe_has_independent_identity_and_private_probe_session():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    executor = FakeExecutor(calls)
    result = await RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget()).run(
        executor=executor,
        pool_spec=pool_spec(),
        credential_projection=projection(),
        generation=1,
        timeout=5,
    )
    assert result.probe_id
    assert executor.last_request["kwargs"]["worker_instance_id"] != "worker-1"
    assert executor.last_request["kwargs"]["operation_kind"] == "runtime_probe"
    assert "probe" in executor.last_request["kwargs"]["host_cwd"]
    assert "--session-dir" in executor.last_request["argv"]
    assert "--session" not in executor.last_request["argv"]


@pytest.mark.asyncio
async def test_probe_prepares_materialized_home_inside_workspace(tmp_path):
    """The probe hello runs in the pool container where the runtime agent's
    baseEnv HOME has NO pi provider config. The probe must materialize a probe
    HOME (same mechanism as worker spawn) and forward HOME/PI_CODING_AGENT_DIR,
    or the hello fails with "Unknown provider" regardless of the credential."""
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    executor = FakeExecutor(calls)
    executor.run_root = tmp_path
    result = await RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget()).run(
        executor=executor,
        pool_spec=pool_spec(),
        credential_projection=projection(),
        generation=1,
        timeout=5,
    )
    assert result.ready
    env = executor.last_request["kwargs"]["env"]
    home = env["HOME"]
    assert home.startswith("/home/kali/workspace/homes/probe-")
    assert env["PI_CODING_AGENT_DIR"] == f"{home}/.pi/agent"
    # the materialized config landed on the shared bind mount under the
    # workspace (container-visible), not just an ephemeral host path
    label = home.rsplit("/", 1)[-1]
    materialized = tmp_path / "workspace" / "homes" / label
    assert materialized.is_dir()


@pytest.mark.asyncio
async def test_probe_success_is_cached_and_identity_changes_invalidate_cache():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    probe = RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget())
    executor = FakeExecutor(calls)
    first = await probe.run(executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5)
    second = await probe.run(executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5)
    assert first.probe_id == second.probe_id
    assert calls.count("provider_request") == 1
    changed = FakeProjection(env={}, credential_version_digest="cred-v2")
    third = await probe.run(executor=executor, pool_spec=pool_spec(), credential_projection=changed, generation=1, timeout=5)
    assert third.probe_id != first.probe_id
    assert calls.count("provider_request") == 2


@pytest.mark.asyncio
async def test_concurrent_waiters_share_one_paid_probe():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    probe = RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget())
    executor = FakeExecutor(calls, delay=0.02)
    results = await asyncio.gather(
        probe.run(executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5),
        probe.run(executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5),
    )
    assert results[0].probe_id == results[1].probe_id
    assert calls.count("provider_request") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "category"),
    [
        (RuntimeError("401 unauthorized"), "auth_failed", "auth"),
        (RuntimeError("model not found"), "model_or_config_failed", "configuration"),
    ],
)
async def test_auth_and_configuration_failures_are_classified_without_retry(error, code, category):
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    executor = FakeExecutor(calls, error=error)
    result = await RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget()).run(
        executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5
    )
    assert result.ready is False
    assert result.failure.code == code
    assert result.failure.category == category
    assert calls.count("provider_request") == 1


@pytest.mark.asyncio
async def test_infrastructure_failure_retries_at_most_once():
    from dswarm.solver.runtime_probe import RuntimeProbe

    calls = []
    executor = FakeExecutor(calls, error=RuntimeError("connection reset"))
    result = await RuntimeProbe(usage_writer=OrderedWriter(calls), budget_gate=AllowBudget()).run(
        executor=executor, pool_spec=pool_spec(), credential_projection=projection(), generation=1, timeout=5
    )
    assert result.ready is False
    assert result.failure.category == "infrastructure"
    assert calls.count("provider_request") == 2
