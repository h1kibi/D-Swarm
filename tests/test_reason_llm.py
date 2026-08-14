from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.web.llm_test import test_llm_endpoint as run_llm_endpoint_test
from apps.web.reason_llm import resolve_reason_llm_endpoint
from apps.web.worker_config import DEFAULT_DEEPSEEK_BASE_URL
from dswarm.core.llm import LLMClient
from dswarm.models.solve_graph import Challenge
from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root
from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root
from dswarm.swarm.reason_scheduler import ReasonSwarm


def test_reason_resolver_uses_account_relay_when_profile_is_default_deepseek(tmp_path):
    store = CredentialAccountStore(account_store_root(tmp_path))
    store.upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="relay-token",
        base_url="https://relay.example/v1",
        target_engine="pi",
    )

    resolved = resolve_reason_llm_endpoint(
        sessions_root=tmp_path,
        worker_profiles=[{"name": "pi-worker", "credential_account": "pi-main", "enabled": True}],
        profile={
            "model": "deepseek-v4-pro",
            "base_url": DEFAULT_DEEPSEEK_BASE_URL,
            "credential_source": "auto",
            "credential_account": "pi-main",
        },
        env={},
    )

    assert resolved["api_key"] == "relay-token"
    assert resolved["base_url"] == "https://relay.example/v1"
    assert resolved["base_url_source"] == "account"
    assert resolved["credential_source"] == "account"
    assert resolved["credential_account"] == "pi-main"
    assert resolved["has_api_key"] is True


def test_reason_resolver_keeps_explicit_non_default_planner_base(tmp_path):
    store = CredentialAccountStore(account_store_root(tmp_path))
    store.upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="relay-token",
        base_url="https://relay.example/v1",
        target_engine="pi",
    )

    resolved = resolve_reason_llm_endpoint(
        sessions_root=tmp_path,
        worker_profiles=[{"name": "pi-worker", "credential_account": "pi-main", "enabled": True}],
        profile={
            "model": "gpt-5.5",
            "base_url": "https://planner.example/v1",
            "credential_source": "account",
            "credential_account": "pi-main",
        },
        env={},
    )

    assert resolved["api_key"] == "relay-token"
    assert resolved["base_url"] == "https://planner.example/v1"
    assert resolved["base_url_source"] == "profile"
    assert resolved["credential_source"] == "account"


def test_llm_client_honors_provider_auth_modes():
    x_api_key = LLMClient(api_key="secret", auth_mode="x-api-key")
    assert x_api_key._headers()["x-api-key"] == "secret"
    assert "Authorization" not in x_api_key._headers()

    custom = LLMClient(
        api_key="secret",
        auth_mode="custom",
        auth_header="X-Relay-Key",
        auth_prefix="Token",
    )
    assert custom._headers()["X-Relay-Key"] == "Token secret"


@pytest.mark.asyncio
async def test_reason_llm_probe_uses_provider_auth_mode(tmp_path, monkeypatch):
    provider_store = LLMProviderSecretStore(provider_secret_root(tmp_path))
    provider_store.upsert_secret("relay", "relay-secret")

    seen = {}

    class FakeResponse:
        content = "ok"
        finish_reason = "stop"

    class FakeLLM:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def chat(self, **kwargs):
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr("dswarm.core.llm.LLMClient", FakeLLM)

    result = await run_llm_endpoint_test(
        which="planner",
        base_url=None,
        model="relay-model",
        sessions_root=tmp_path,
        worker_profiles=[],
        llm_providers=[
            {
                "id": "relay",
                "label": "Relay",
                "base_url": "https://relay.example/v1",
                "wire_api": "openai-chat",
                "auth_mode": "x-api-key",
                "auth_header": "x-api-key",
                "auth_prefix": "",
                "models": [],
            }
        ],
        provider_ref="relay",
        credential_account="relay",
        credential_source="provider",
        wire_api="auto",
    )

    assert result["ok"] is True
    assert seen["auth_mode"] == "x-api-key"
    assert seen["auth_header"] == "x-api-key"
    assert seen["auth_prefix"] == ""


@pytest.mark.asyncio
async def test_reason_llm_probe_returns_structured_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DSWARM_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DSWARM_DEEPSEEK_BASE_URL", raising=False)

    result = await run_llm_endpoint_test(
        which="planner",
        base_url=DEFAULT_DEEPSEEK_BASE_URL,
        model="deepseek-v4-pro",
        sessions_root=tmp_path,
        worker_profiles=[],
        credential_source="auto",
        credential_account="pi-main",
        wire_api="auto",
    )

    assert result["ok"] is False
    assert result["code"] == "missing_api_key"
    assert result["credential_source"] == "auto"
    assert result["credential_account"] == "pi-main"
    assert result["layers"][-1]["name"] == "auth"
    assert result["layers"][-1]["ok"] is False


@pytest.mark.asyncio
async def test_reason_scheduler_emits_planner_unavailable_diagnostic(monkeypatch):
    monkeypatch.setenv("DSWARM_REASON_MAX_PLANNER_FAILURES", "1")
    events = []

    class Bus:
        async def emit(self, event):
            events.append(event)

    async def worker_factory(_decision, _profile):
        return SimpleNamespace(flag=None, flags=[], engine="test")

    swarm = ReasonSwarm(
        Challenge(id="c1", name="demo", category="web"),
        llm=None,
        bus=Bus(),
        run_id="run-1",
        wall_clock_budget=5,
        poll_interval=0.01,
        worker_factory=worker_factory,
        planner_diagnostic={
            "code": "missing_api_key",
            "detail": "Planner API key is missing.",
            "planner": "deepseek-v4-pro",
            "base_url_host": "api.deepseek.com",
            "credential_source": "auto",
            "credential_account": "pi-main",
        },
    )

    out = await swarm.run()

    assert out["solved"] is False
    payloads = [event.payload for event in events]
    unavailable = [p for p in payloads if p.get("kind") == "reason_planner_unavailable"]
    assert unavailable
    payload = unavailable[-1]
    assert payload["code"] == "missing_api_key"
    assert payload["detail"] == "Planner API key is missing."
    assert payload["planner"] == "deepseek-v4-pro"
    assert payload["base_url_host"] == "api.deepseek.com"
    assert payload["credential_source"] == "auto"
    assert payload["credential_account"] == "pi-main"
    assert payload["failures"] == 1
