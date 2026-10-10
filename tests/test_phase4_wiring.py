from __future__ import annotations

import asyncio

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


def test_run_manager_resolves_native_provider_into_private_gateway_binding(tmp_path, monkeypatch):
    from apps.web.run_manager import RunManager
    from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root

    mgr = RunManager(sessions_root=tmp_path)
    monkeypatch.setattr(mgr.worker_config, "get", lambda: {"llm_providers": [{
        "id": "anthropic", "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1", "wire_api": "anthropic",
        "auth_mode": "x-api-key", "auth_header": "x-api-key", "auth_prefix": "",
        "models": ["claude-sonnet-4-5"],
    }]})
    LLMProviderSecretStore(provider_secret_root(tmp_path)).upsert_secret(
        "anthropic", "sk-private-manager-key",
    )

    binding = mgr._resolve_worker_provider_binding("anthropic")

    assert binding is not None
    assert binding.provider_id == "anthropic"
    assert binding.wire_api == "anthropic"
    assert binding.headers()["x-api-key"] == "sk-private-manager-key"
    assert "sk-private-manager-key" not in repr(binding)
    assert mgr._resolve_worker_provider_binding("missing") is None


def test_binding_snapshot_freezes_safe_provider_transport_metadata(tmp_path):
    from apps.web.run_manager import build_binding_snapshot

    class Config:
        def get(self):
            return {
                "worker_profiles": [{
                    "id": "web-worker", "name": "web-worker", "engine": "pi", "enabled": True,
                    "provider_ref": "relay", "model": "relay-model",
                }],
                "llm_providers": [{
                    "id": "relay",
                    "base_url": "https://user:password@relay.example:8443/v1?api_key=bad#fragment",
                    "wire_api": "anthropic", "auth_mode": "x-api-key",
                    "auth_header": "x-api-key", "auth_prefix": "",
                }],
            }

    snapshot = build_binding_snapshot({"engines": ["web-worker"]}, Config())
    binding = snapshot["roles"]["worker"]
    assert binding["base_url"] == "https://relay.example:8443/v1"
    assert binding["wire_api"] == "anthropic"
    assert "api_key" not in str(snapshot).lower()
    assert "bad" not in str(snapshot)
    assert "password" not in str(snapshot)
    assert "fragment" not in str(snapshot)


def test_binding_snapshot_selective_overrides_and_settings_only_roles(tmp_path):
    """docs/17 §4.1: an explicit per-run override replaces ONLY the named roles;
    Observer and Titler always stay on the settings default."""
    from apps.web.run_manager import build_binding_snapshot

    class Config:
        def get(self):
            return {
                "worker_profiles": [{
                    "id": "web-worker", "name": "web-worker", "engine": "pi",
                    "enabled": True, "model": "settings-worker-model",
                }],
                "llm_profiles": {
                    "reason": {"model": "settings-reason"},
                    "worker": {"model": "settings-worker"},
                    "review": {"model": "settings-review"},
                    "observer": {"model": "settings-observer"},
                    "observer_fallback": {"model": "settings-observer-fb"},
                    "titler": {"model": "settings-titler"},
                },
            }

    snapshot = build_binding_snapshot({
        "engines": ["web-worker"],
        "llm_mode": "per_run_override",
        "llm_overrides": {
            "reason": {"model": "run-reason"},
            "reviewer": {"model": "run-review"},
            # These must be ignored: Observer/Titler are settings-only.
            "observer": {"model": "run-observer"},
            "titler": {"model": "run-titler"},
        },
    }, Config())

    assert snapshot["llm_mode"] == "per_run_override"
    assert snapshot["role_sources"] == {
        "reason": "run_override",
        "worker": "settings_default",
        "review": "run_override",
        "observer": "settings_default",
        "titler": "settings_default",
    }
    roles = snapshot["roles"]
    assert roles["reason"]["model"] == "run-reason"
    assert roles["review"]["model"] == "run-review"
    assert roles["worker"]["model"] == "settings-worker"
    assert roles["observer"]["model"] == "settings-observer"
    assert roles["titler"]["model"] == "settings-titler"
    # The Observer fallback candidate is the explicit lightweight Observer
    # binding, never the primary Observer or Titler.
    assert snapshot["fallback_candidates"]["observer"]["model"] == "settings-observer-fb"


def test_binding_snapshot_settings_default_mode_ignores_overrides(tmp_path):
    """`llm_mode=settings_default` means every role resolves from settings even
    if stray override values are attached to the request."""
    from apps.web.run_manager import build_binding_snapshot

    class Config:
        def get(self):
            return {
                "worker_profiles": [{
                    "id": "web-worker", "name": "web-worker", "engine": "pi",
                    "enabled": True,
                }],
                "llm_profiles": {"reason": {"model": "settings-reason"}},
            }

    snapshot = build_binding_snapshot({
        "engines": ["web-worker"],
        "llm_mode": "settings_default",
        "llm_overrides": {"reason": {"model": "should-not-apply"}},
    }, Config())

    assert snapshot["role_sources"]["reason"] == "settings_default"
    assert snapshot["roles"]["reason"]["model"] == "settings-reason"


def test_frozen_reviewer_binding_wins_over_mutable_settings(tmp_path):
    """A Run's frozen Reviewer binding is what the Reviewer role uses — later
    settings edits cannot silently re-point an in-flight Run (docs/17 §4.3)."""
    from dswarm.models.solve_graph import Challenge
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.swarm.swarm import Swarm

    swarm = Swarm(
        Challenge(id="frozen-review", name="frozen-review", category="web"),
        llm=None,
        review_llm=object(),
        sandbox=SandboxManager(root=tmp_path / "sandbox"),
        binding_snapshot={
            "roles": {"review": {"provider": "deepseek", "model": "frozen-review-model"}},
            "fallback": {"review": {"provider": "deepseek", "model": "settings-review-model"}},
        },
        llm_profiles={"review": {"provider": "deepseek", "model": "mutable-review-model"}},
    )
    assert swarm._reviewer_profile()["model"] == "frozen-review-model"
    # The settings default remains the recorded fallback candidate.
    assert swarm.binding_snapshot["fallback"]["review"]["model"] == "settings-review-model"


def test_gateway_binding_uses_frozen_transport_after_provider_registry_change(tmp_path):
    from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root
    from dswarm.swarm.worker_runtime_mixin import WorkerRuntimeMixin

    store = LLMProviderSecretStore(provider_secret_root(tmp_path))
    store.upsert_secret("relay", "secret-that-stays-host-side")
    runtime = object.__new__(WorkerRuntimeMixin)
    runtime.workspace_root = tmp_path / "run" / "workspace"
    runtime.credential_accounts_root = tmp_path / "_secrets" / "accounts"
    runtime.llm_providers = []  # provider definition was deleted after Run launch

    binding = runtime._gateway_upstream_binding_for_profile({
        "provider_ref": "relay", "base_url": "https://frozen.example/v1",
        "wire_api": "anthropic", "auth_mode": "x-api-key",
        "auth_header": "x-api-key", "auth_prefix": "",
    })
    assert binding is not None
    assert binding.base_url == "https://frozen.example/v1"
    assert binding.headers()["x-api-key"] == "secret-that-stays-host-side"
    assert "secret-that-stays-host-side" not in repr(binding)


def test_runtime_pool_manager_uses_frozen_provider_transport(tmp_path, monkeypatch):
    """A restored run must not resolve its probe through edited global settings."""
    from apps.web.run_manager import RunManager
    from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root
    from dswarm.solver.runtime_policy import (
        PoolSpec,
        RuntimeNetworkSpec,
        RuntimeResourceSpec,
        RuntimeSnapshot,
        build_runtime_policy,
    )

    manager = RunManager(sessions_root=tmp_path)
    provider_store = LLMProviderSecretStore(provider_secret_root(tmp_path))
    provider_store.upsert_secret("relay", "host-private-provider-key")
    monkeypatch.setattr(
        manager.worker_config,
        "get",
        lambda: {
            "worker_profiles": [],
            "overrides": {},
            "llm_providers": [{
                "id": "relay",
                "base_url": "https://mutable.example/v1",
                "wire_api": "openai-chat",
            }],
        }
    )

    run = manager.create("frozen-provider-runtime")
    run.binding_snapshot = {
        "runtime_worker_profiles": [{
            "name": "worker",
            "provider_ref": "relay",
            "base_url": "",
            "api_key_ref": "",
        }],
        "roles": {
            "worker": {
                "provider_ref": "relay",
                "base_url": "https://frozen.example/v1",
                "wire_api": "anthropic",
                "auth_mode": "x-api-key",
                "auth_header": "x-api-key",
                "auth_prefix": "",
            },
        },
    }
    pool_spec = PoolSpec.with_computed_id(
        profile_id="worker",
        runtime_kind="pi",
        resolved_image_id="sha256:" + "a" * 64,
        requested_image_ref="worker:test",
        network=RuntimeNetworkSpec("bridge"),
        resources=RuntimeResourceSpec("1", "1g", 128, 1024),
        credential_binding_id="relay",
        provider_binding_id="relay",
        model="frozen-model",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2",),
        protocol_version=2,
        pool_max_concurrent_workers=1,
    )
    snapshot = RuntimeSnapshot(
        version=1,
        run_id=run.run_id,
        created_at=1.0,
        runtime_policy=build_runtime_policy(mode="docker"),
        shared_uid=1000,
        shared_gid=1000,
        pools=(pool_spec,),
    )
    captured = {}

    class Pool:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.run_id = kwargs["run_id"]
            self.snapshot = kwargs["snapshot"]

    monkeypatch.setattr(
        "dswarm.solver.runtime_factory.ContainerPoolManager",
        Pool,
    )
    pool = manager._default_runtime_pool_manager_factory(
        run_id=run.run_id,
        snapshot=snapshot,
    )

    resolver = captured["probe"].gateway_binding_resolver
    binding = resolver("relay")
    assert pool.run_id == run.run_id
    assert binding is not None
    assert binding.base_url == "https://frozen.example/v1"
    assert binding.wire_api == "anthropic"
    assert binding.headers()["x-api-key"] == "host-private-provider-key"

    modes = captured["credential_modes"]
    assert modes[pool_spec.pool_id] == "gateway"


def test_binding_snapshot_keeps_nonsecret_runtime_worker_profiles(tmp_path):
    from apps.web.run_manager import build_binding_snapshot

    class Config:
        def get(self):
            return {
                "worker_profiles": [{
                    "id": "pi-zhipu",
                    "name": "pi-zhipu",
                    "engine": "pi",
                    "runtime": "docker-web",
                    "image": "worker:zhipu",
                    "provider_ref": "zhipu",
                    "credential_account": "",
                    "model": "glm-5.3-flash",
                    "enabled": True,
                }],
                "llm_providers": [{
                    "id": "zhipu",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "wire_api": "openai-chat",
                }],
            }

    snapshot = build_binding_snapshot({"engines": ["pi-zhipu"]}, Config())

    assert snapshot["runtime_worker_profiles"] == [{
        "id": "pi-zhipu",
        "name": "pi-zhipu",
        "engine": "pi",
        "runtime": "docker-web",
        "image": "worker:zhipu",
        "provider_ref": "zhipu",
        "model": "glm-5.3-flash",
        "runtime_credential_mode": "gateway",
    }]


def test_gateway_binding_fails_closed_when_frozen_provider_secret_is_missing(tmp_path):
    from dswarm.solver.runtime_policy import RuntimePolicyError
    from dswarm.swarm.worker_runtime_mixin import WorkerRuntimeMixin

    runtime = object.__new__(WorkerRuntimeMixin)
    runtime.workspace_root = tmp_path / "run" / "workspace"
    runtime.credential_accounts_root = tmp_path / "_secrets" / "accounts"
    runtime.llm_providers = []

    with pytest.raises(RuntimePolicyError, match="gateway_provider_binding_unavailable"):
        runtime._gateway_upstream_binding_for_profile({
            "provider_ref": "deleted", "base_url": "https://frozen.example/v1",
            "wire_api": "openai-chat",
        })


def test_run_manager_rehydrates_target_bound_frozen_transport_after_restart(tmp_path):
    """A target-owned draft keeps its immutable routing snapshot across restart."""
    from apps.web.run_manager import RunManager

    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({
        "name": "restart target",
        "category": "web",
        "description": "target-owned draft",
    })
    run = manager.create_new()
    manager.target_store.attach_run(target["id"], run.run_id)

    profile = manager.worker_config.get()["worker_profiles"][0]
    profile_name = str(profile.get("name") or profile.get("id"))
    manager.remember_dispatch(run.run_id, {
        "challenge": {"name": "restart target", "category": "web"},
        "engines": [profile_name],
        "llm_profiles": {
            "worker": {
                "provider_ref": "relay-a",
                "base_url": "https://relay-a.example/v1?api_key=should-not-persist",
                "wire_api": "openai-chat",
                "model": "worker-v1",
                "api_key": "sk-live-secret",
            },
        },
    })
    original_snapshot = dict(run.binding_snapshot)

    restarted = RunManager(sessions_root=tmp_path)
    restored = restarted.get(run.run_id)

    assert restored is not None
    assert restored.target_id == target["id"]
    assert restored.binding_snapshot == original_snapshot
    persisted = restarted.dispatch_for(run.run_id)
    assert persisted["binding_snapshot"] == original_snapshot
    assert "sk-live-secret" not in str(persisted)
    assert "api_key=should-not-persist" not in str(persisted)

    # Settings edits after restart must not mutate an existing Run's routing.
    restarted.worker_config.set(llm_providers=[{
        "id": "relay-a",
        "label": "Relay A (edited)",
        "base_url": "https://relay-new.example/v1",
        "wire_api": "openai-chat",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
    }])
    assert restarted.get(run.run_id).binding_snapshot == original_snapshot
