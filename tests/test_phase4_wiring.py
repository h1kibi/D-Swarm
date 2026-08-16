from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_run_manager_registers_gateway_bridge_for_each_live_run(tmp_path, monkeypatch):
    from apps.web.run_manager import RunManager

    class FakeGateway:
        def __init__(self):
            self.calls = []
            self.account_root = None
            self.sessions_root = None

        def configure_usage_bridge(self, **kwargs):
            self.calls.append(kwargs)

    gateway = FakeGateway()
    monkeypatch.setattr(
        "dswarm.solver.modelgateway.ModelGateway.instance",
        classmethod(lambda cls: gateway),
    )

    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-bridge")

    assert gateway.sessions_root == str(tmp_path)
    assert gateway.calls == [{
        "bus": run.bus,
        "loop": asyncio.get_running_loop(),
        "run_id": "run-bridge",
    }]


def test_run_manager_builds_run_scoped_internal_usage_writer(tmp_path):
    from apps.web.run_manager import RunManager

    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-usage")
    writer = mgr.internal_usage_writer(
        run, solver_id="reason", profile_id="planner",
        configured_account_id="acct-1",
    )

    assert writer.journal.path == tmp_path / "run-usage-usage-journal.jsonl"
    assert writer.context is not None
    assert writer.context.run_id == "run-usage"
    assert writer.context.challenge_id == "run-usage"
    assert writer.context.solver_id == "reason"
    assert writer.context.profile_id == "planner"
    assert writer.context.configured_account_id == "acct-1"
    assert writer.context.producer == "internal"


@pytest.mark.asyncio
async def test_summarizer_forwards_solver_identity_to_injected_llm():
    from dswarm.solver.summarizer import summarize_node

    seen = []

    class FakeResponse:
        content = "简短摘要"

    class FakeLLM:
        async def chat(self, **kwargs):
            seen.append(kwargs)
            return FakeResponse()

    result = await summarize_node(
        "A sufficiently long finding that should be summarized by the internal producer.",
        node_kind="intent", intent_id="intent-1",
        llm=FakeLLM(), run_id="run-1", challenge_id="ch-1",
        solver_id="summarizer",
    )

    assert result == "简短摘要"
    assert seen[0]["solver_id"] == "summarizer"


@pytest.mark.asyncio
async def test_cli_solver_forwards_internal_usage_writer_to_summary(monkeypatch):
    from dswarm.models.solve_graph import Challenge
    from dswarm.solver.cli_solver import CliSolver

    writer = object()
    seen = {}

    async def fake_summarize_node(text, **kwargs):
        seen.update(kwargs)
        return "summary"

    monkeypatch.setattr(
        "dswarm.solver.summarizer.summarize_node", fake_summarize_node
    )
    challenge = Challenge(id="ch-1", name="demo", category="web")
    solver = CliSolver(
        None, challenge, bus=object(), shared_graph=object(),
        run_id="run-1", usage_writer=writer,
    )

    solver._summarize_async(
        "A sufficiently long fact that should be summarized by the internal producer.",
        node_kind="fact", fact_seq=3,
    )
    await asyncio.sleep(0)

    assert solver.usage_writer is writer
    assert seen["usage_writer"] is writer
    assert seen["solver_id"] == "summarizer"


@pytest.mark.asyncio
async def test_swarm_signature_accepts_internal_usage_writer():
    from inspect import signature
    from dswarm.swarm.swarm import Swarm

    assert "usage_writer" in signature(Swarm.__init__).parameters

@pytest.mark.asyncio
async def test_usage_writer_supports_fallback_invocation_aggregate(tmp_path):
    from dswarm.core.usage_journal import UsageContext, UsageJournal, UsageWriter

    writer = UsageWriter(
        UsageJournal(tmp_path / "fallback.jsonl"),
        context=UsageContext(run_id="run-1", producer="fallback"),
    )
    invocation = await writer.start(provider_call_id="invocation-1")
    record = await writer.finish(
        invocation,
        call_outcome="succeeded",
        usage_status="estimated",
        usage={"prompt_tokens": 8, "completion_tokens": 2, "usd": 0.001},
    )

    assert invocation.invocation_id == "invocation-1"
    assert record.record_kind == "invocation_aggregate"
    assert record.provider_call_id is None
    assert record.usage_id == "usage::run-1::fallback::invocation-1"

@pytest.mark.asyncio
async def test_cli_solver_records_fallback_unknown_invocation(monkeypatch):
    from dswarm.models.solve_graph import Challenge
    from dswarm.solver.cli_driver import CliResult
    from dswarm.solver.cli_solver import CliSolver

    class FakeWriter:
        context = None

        async def start(self, provider_call_id=None):
            self.started = provider_call_id
            return type("Invocation", (), {"invocation_id": provider_call_id})()

        async def finish(self, call, **kwargs):
            self.finished = (call, kwargs)
            return None

    writer = FakeWriter()
    solver = CliSolver(
        None, Challenge(id="ch-1", name="demo", category="web"),
        run_id="run-1", fallback_usage_writer=writer,
    )

    await solver._stream_cost(CliResult(text="", invocation_id="inv-1"))

    assert writer.started == "inv-1"
    assert writer.finished[1]["call_outcome"] == "succeeded"
    assert writer.finished[1]["usage_status"] == "unknown"
    assert writer.finished[1]["usage"] == {}


def test_cli_result_exposes_invocation_id():
    from dswarm.solver.cli_driver import CliResult

    assert CliResult(text="", invocation_id="inv-1").invocation_id == "inv-1"


def test_swarm_signature_accepts_fallback_usage_writer():
    from inspect import signature
    from dswarm.swarm.swarm import Swarm

    assert "fallback_usage_writer" in signature(Swarm.__init__).parameters

def test_run_cli_assigns_invocation_id(monkeypatch, tmp_path):
    from dswarm.solver.cli_driver import CliResult, run_cli

    class Driver:
        close_stdin = False

        def parse(self, stdout, stderr):
            return CliResult(text=stdout)

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: type(
        "Completed", (), {"stdout": "ok", "stderr": ""}
    )())

    result = run_cli(Driver(), ["fake"], cwd=str(tmp_path), timeout=1)

    assert result.invocation_id

def test_run_manager_builds_run_scoped_fallback_usage_writer(tmp_path):
    from apps.web.run_manager import RunManager

    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-fallback")
    writer = mgr.fallback_usage_writer(run, solver_id="cli-pi", profile_id="pi-worker")

    assert writer.context is not None
    assert writer.context.producer == "fallback"
    assert writer.context.solver_id == "cli-pi"
    assert writer.context.profile_id == "pi-worker"
    assert writer.journal.path == tmp_path / "run-fallback-usage-journal.jsonl"
