from __future__ import annotations

import copy
import json

import httpx
import pytest

from apps.web.run_manager import RunManager
from apps.web.server import create_app
from apps.web.llm_providers import LLMProviderSecretStore, provider_secret_root
from apps.web.worker_config import DEFAULT_RUNTIME_PROFILES, WorkerConfigStore
from apps.web.worker_settings import (
    apply_workspace_draft,
    derive_routing,
    validate_workspace_draft,
    workspace_revision,
    workspace_snapshot,
)
from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root


def _stores(tmp_path):
    config = WorkerConfigStore(root=tmp_path)
    accounts = CredentialAccountStore(account_store_root(tmp_path))
    return config, accounts


def _profile(draft: dict, label: str) -> dict:
    return next(
        row for row in draft["worker_profiles"]
        if (row.get("label") or row.get("name") or row.get("id")) == label
    )


def _valid_web_draft(config: WorkerConfigStore) -> dict:
    draft = copy.deepcopy(config.get())
    web = _profile(draft, "pi-web")
    web.update({
        "enabled": True,
        "base_url": "https://api.example.test/v1",
        "model": "example-model",
        "runtime": "docker-web",
        "image": "ctf-swarm-pi-web:test",
        "max_running": 2,
    })
    return draft


def test_workspace_snapshot_never_exposes_raw_credentials(tmp_path):
    config, accounts = _stores(tmp_path)
    accounts.upsert_secret(
        account_id="pi-web-main",
        engine="api",
        secret="top-secret-value",
        base_url="https://api.example.test/v1",
        target_engine="pi",
    )

    snapshot = workspace_snapshot(config, accounts)

    encoded = json.dumps(snapshot)
    assert "top-secret-value" not in encoded
    account = snapshot["accounts"][0]
    assert account["details"]["has_secret"] is True
    assert account["details"]["base_url_value"] == "https://api.example.test/v1"
    assert "secret_value" not in account["details"]


def test_disabled_incomplete_worker_warns_but_enabled_worker_errors(tmp_path):
    config, accounts = _stores(tmp_path)
    draft = copy.deepcopy(config.get())
    web = _profile(draft, "pi-web")
    web.update({"model": "", "base_url": "", "credential_account": "missing"})

    disabled = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list()
    )
    assert disabled["ok"] is True
    assert disabled["issues"]
    assert {row["severity"] for row in disabled["issues"]} == {"warning"}

    web["enabled"] = True
    enabled = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list()
    )
    assert enabled["ok"] is False
    assert any(row["severity"] == "error" for row in enabled["issues"])


def test_builtin_runtime_is_immutable_but_direction_private_clone_is_valid(tmp_path):
    config, accounts = _stores(tmp_path)
    draft = copy.deepcopy(config.get())
    docker_web = next(row for row in draft["runtime_profiles"] if row["id"] == "docker-web")
    docker_web["memory"] = "99g"

    invalid = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list()
    )
    assert invalid["ok"] is False
    assert any(row["code"] == "builtin_runtime_immutable" for row in invalid["issues"])

    docker_web["memory"] = next(
        row["memory"] for row in DEFAULT_RUNTIME_PROFILES if row["id"] == "docker-web"
    )
    private_runtime = copy.deepcopy(docker_web)
    private_runtime.update({"id": "direction-web-custom", "label": "Web private", "memory": "16g"})
    draft["runtime_profiles"].append(private_runtime)
    _profile(draft, "pi-web")["runtime"] = "direction-web-custom"

    valid = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list()
    )
    assert valid["ok"] is True
    assert not any(row["severity"] == "error" for row in valid["issues"])


def test_derive_routing_excludes_disabled_and_custom_workers():
    profiles = [
        {"id": "pi-worker", "name": "pi-worker", "enabled": True},
        {"id": "pi-web", "name": "pi-web", "enabled": True, "max_running": 3},
        {"id": "pi-pwn", "name": "pi-pwn", "enabled": False, "max_running": 2},
        {"id": "manual-specialist", "name": "manual-specialist", "enabled": True},
    ]

    engines, overrides, system_ref = derive_routing(profiles)

    assert engines == ["pi-worker", "pi-web"]
    assert overrides == {"web": {"engines": ["pi-web"], "start_workers": 2}}
    assert system_ref == "pi-worker"


def test_apply_retains_replaces_and_removes_write_only_secret(tmp_path):
    config, accounts = _stores(tmp_path)
    accounts.upsert_secret(
        account_id="pi-web-main",
        engine="api",
        secret="original-key",
        base_url="https://api.example.test/v1",
        target_engine="pi",
    )
    draft = _valid_web_draft(config)

    retained = apply_workspace_draft(
        config_store=config,
        account_store=accounts,
        base_revision=workspace_revision(config, accounts),
        draft=draft,
        secret_updates=[],
    )
    key_path = accounts.root / "pi-web-main" / "API_KEY"
    assert key_path.read_text(encoding="utf-8").strip() == "original-key"
    assert "original-key" not in json.dumps(retained)
    resolved = config.resolve("web")
    assert len(resolved["engines"]) == 1
    routed = next(p for p in resolved["worker_profiles"] if p["name"] == resolved["engines"][0])
    assert (routed.get("label") or routed.get("name")) == "pi-web"

    draft = copy.deepcopy(config.get())
    web = _profile(draft, "pi-web")
    web["base_url"] = "https://new.example.test/v1"
    replaced = apply_workspace_draft(
        config_store=config,
        account_store=accounts,
        base_revision=retained["revision"],
        draft=draft,
        secret_updates=[{
            "account_id": "pi-web-main",
            "action": "replace",
            "value": "replacement-key",
            "base_url": "https://new.example.test/v1",
        }],
    )
    assert key_path.read_text(encoding="utf-8").strip() == "replacement-key"
    assert "replacement-key" not in json.dumps(replaced)

    draft = copy.deepcopy(config.get())
    _profile(draft, "pi-web")["enabled"] = False
    removed = apply_workspace_draft(
        config_store=config,
        account_store=accounts,
        base_revision=replaced["revision"],
        draft=draft,
        secret_updates=[{"account_id": "pi-web-main", "action": "remove"}],
    )
    assert not key_path.exists()
    assert all(row["account_id"] != "pi-web-main" for row in removed["accounts"])


def test_apply_rejects_stale_revision_without_mutation(tmp_path):
    config, accounts = _stores(tmp_path)
    before = config.raw_snapshot()

    with pytest.raises(RuntimeError, match="settings_revision_conflict"):
        apply_workspace_draft(
            config_store=config,
            account_store=accounts,
            base_revision="stale-revision",
            draft=copy.deepcopy(config.get()),
        )

    assert config.raw_snapshot() == before


def test_apply_rolls_back_config_and_account_material_on_failure(tmp_path, monkeypatch):
    config, accounts = _stores(tmp_path)
    accounts.upsert_secret(account_id="pi-web-main", engine="pi", secret="old-key")
    before_config = config.raw_snapshot()
    before_key = (accounts.root / "pi-web-main" / "API_KEY").read_bytes()
    draft = copy.deepcopy(config.get())

    def fail_set(**_kwargs):
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(config, "set", fail_set)
    with pytest.raises(OSError, match="simulated persistence failure"):
        apply_workspace_draft(
            config_store=config,
            account_store=accounts,
            base_revision=workspace_revision(config, accounts),
            draft=draft,
            secret_updates=[{
                "account_id": "pi-web-main",
                "action": "replace",
                "value": "new-key",
            }],
        )

    assert config.raw_snapshot() == before_config
    assert (accounts.root / "pi-web-main" / "API_KEY").read_bytes() == before_key


def test_apply_persists_identity_projection_and_custom_worker_stays_manual(tmp_path):
    config, accounts = _stores(tmp_path)
    draft = copy.deepcopy(config.get())
    custom = copy.deepcopy(_profile(draft, "pi-web"))
    custom.update({
        "id": "manual-specialist",
        "name": "manual-specialist",
        "label": "Manual Specialist",
        "enabled": True,
        "base_url": "https://api.example.test/v1",
        "credential_account": "manual-main",
    })
    draft["worker_profiles"].append(custom)

    result = apply_workspace_draft(
        config_store=config,
        account_store=accounts,
        base_revision=workspace_revision(config, accounts),
        draft=draft,
        secret_updates=[{
            "account_id": "manual-main",
            "action": "replace",
            "value": "manual-key",
            "base_url": "https://api.example.test/v1",
        }],
    )

    raw = json.loads(config.path.read_text(encoding="utf-8"))
    assert raw["seats"] and raw["credentials"] and raw["environments"]
    assert result["config"]["overrides"] == {}
    assert config.resolve("unknown")["engines"] == []
    assert config.get()["engines"] == []


@pytest.mark.asyncio
async def test_worker_settings_http_workspace_and_conflict_are_safe(tmp_path):
    manager = RunManager(sessions_root=tmp_path / "sessions")
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workspace = await client.get("/api/settings/workers")
        assert workspace.status_code == 200
        body = workspace.json()
        assert body["revision"]
        assert len(body["config"]["worker_profiles"]) >= 7

        conflict = await client.put(
            "/api/settings/workers/apply",
            json={
                "base_revision": "stale",
                "draft": body["config"],
                "secret_updates": [],
            },
        )
        assert conflict.status_code == 409

@pytest.mark.asyncio
async def test_worker_test_route_projects_computed_health_ok(tmp_path, monkeypatch):
    from dswarm.solver.profile_health import ProfileHealth

    manager = RunManager(sessions_root=tmp_path / "sessions")
    app = create_app(manager)

    def fake_health(profile, *, backend, sessions_root, depth, llm_providers=None):
        return ProfileHealth(
            profile_id=str(profile.get("id") or "pi-web"),
            engine="pi",
            backend=backend,
            status="ok",
            layer=None,
            blocker=None,
            detail="模型验证成功（openai-chat）。",
            model=str(profile.get("model") or "example-model"),
            account_id=str(profile.get("credential_account") or "pi-web-main"),
            binding_kind="explicit",
            effective_credential_id=str(profile.get("credential_account") or "pi-web-main"),
        )

    monkeypatch.setattr("dswarm.solver.profile_health.evaluate_profile_health", fake_health)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/settings/workers/test",
            json={"worker_ids": ["pi-web"]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["results"] == [{
        "profile_id": "pi-web",
        "engine": "pi",
        "backend": "container",
        "status": "ok",
        "layer": None,
        "blocker": None,
        "detail": "模型验证成功（openai-chat）。",
        "model": "deepseek-v4-flash",
        "account_id": "pi-web-main",
        "binding_kind": "explicit",
        "effective_credential_id": "pi-web-main",
        "ok": True,
        "worker_id": "pi-web",
    }]


def test_provider_bound_worker_validates_with_central_secret_update(tmp_path):
    config, accounts = _stores(tmp_path)
    draft = _valid_web_draft(config)
    web = _profile(draft, "pi-web")
    web.update({
        "provider_ref": "relay-main",
        "base_url": "",
        "credential_account": "legacy-unused",
        "model": "relay-model",
    })
    draft["llm_providers"] = [{
        "id": "relay-main",
        "label": "Relay Main",
        "base_url": "https://relay.example.test/v1",
        "wire_api": "openai-chat",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
        "models": ["relay-model"],
        "default_model": "relay-model",
    }]

    missing = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list(),
        provider_secrets=[], provider_secret_updates=[],
    )
    assert missing["ok"] is False
    assert any(row["code"] == "missing_provider_secret" for row in missing["issues"])
    assert not any(row["code"] == "missing_account" for row in missing["issues"])

    staged = validate_workspace_draft(
        current=config.get(), draft=draft, accounts=accounts.list(),
        provider_secrets=[],
        provider_secret_updates=[{"provider_id": "relay-main", "action": "replace", "value": "sk-provider"}],
    )
    assert staged["ok"] is True
    assert not any(row["severity"] == "error" for row in staged["issues"])
    assert any(row["scope"] == "provider_secret" and row["id"] == "relay-main" for row in staged["changes"])


def test_apply_persists_provider_config_and_write_only_secret(tmp_path):
    config, accounts = _stores(tmp_path)
    provider_store = LLMProviderSecretStore(provider_secret_root(tmp_path))
    draft = _valid_web_draft(config)
    web = _profile(draft, "pi-web")
    web.update({
        "provider_ref": "relay-main",
        "base_url": "",
        "credential_account": "legacy-unused",
        "model": "relay-model",
    })
    draft["llm_providers"] = [{
        "id": "relay-main",
        "label": "Relay Main",
        "base_url": "https://relay.example.test/v1",
        "wire_api": "openai-chat",
        "auth_mode": "bearer",
        "models": ["relay-model"],
        "default_model": "relay-model",
    }]

    result = apply_workspace_draft(
        config_store=config,
        account_store=accounts,
        provider_store=provider_store,
        base_revision=workspace_revision(config, accounts, provider_store),
        draft=draft,
        provider_secret_updates=[{"provider_id": "relay-main", "action": "replace", "value": "sk-provider"}],
    )

    assert config.get()["llm_providers"][0]["id"] == "relay-main"
    assert provider_store.read_secret("relay-main") == "sk-provider"
    encoded = json.dumps(result)
    assert "sk-provider" not in encoded
    assert result["provider_secrets"] == [provider_store.inspect("relay-main")]


@pytest.mark.asyncio
async def test_legacy_worker_settings_put_preserves_llm_providers(tmp_path):
    manager = RunManager(sessions_root=tmp_path / "sessions")
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        workspace = await client.get("/api/settings/workers")
        assert workspace.status_code == 200
        config_body = workspace.json()["config"]
        config_body["llm_providers"] = [{
            "id": "relay-main",
            "label": "Relay Main",
            "base_url": "https://relay.example.test/v1",
            "wire_api": "openai-chat",
            "auth_mode": "bearer",
            "models": ["relay-model"],
            "default_model": "relay-model",
        }]
        response = await client.put("/api/settings/workers", json={"llm_providers": config_body["llm_providers"]})
        assert response.status_code == 200
        saved = response.json()["config"]
        assert saved["llm_providers"][0]["id"] == "relay-main"


def test_provider_creation_does_not_report_spurious_worker_changes(tmp_path):
    config, accounts = _stores(tmp_path)
    current = config.get()
    draft = json.loads(json.dumps(current))
    draft["llm_providers"] = [{
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "wire_api": "auto",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "default_model": "deepseek-v4-pro",
    }]

    result = validate_workspace_draft(
        current=current,
        draft=draft,
        accounts=accounts.list(),
        provider_secrets=[],
        provider_secret_updates=[],
    )

    worker_changes = [row for row in result["changes"] if row["scope"] == "worker"]
    assert worker_changes == []
    assert any(row["scope"] == "provider" for row in result["changes"])

def test_provider_bound_reason_profiles_validate_with_provider_secret(tmp_path):
    config, accounts = _stores(tmp_path)
    current = config.get()
    draft = copy.deepcopy(current)
    draft["llm_providers"] = [{
        "id": "relay-main",
        "label": "Relay Main",
        "base_url": "https://relay.example.test/v1",
        "wire_api": "openai-chat",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
        "models": ["relay-model"],
        "default_model": "relay-model",
    }]
    for key in ("planner", "titler"):
        draft["llm_profiles"][key].update({
            "provider_ref": "relay-main",
            "provider": "registry",
            "base_url": "",
            "credential_source": "provider",
            "credential_account": "relay-main",
            "wire_api": "openai-chat",
            "model": "relay-model",
        })

    missing = validate_workspace_draft(
        current=current, draft=draft, accounts=accounts.list(),
        provider_secrets=[], provider_secret_updates=[],
    )
    assert missing["ok"] is True
    assert not any(row["code"] == "invalid_llm_source" for row in missing["issues"])
    assert sum(row["code"] == "missing_provider_secret" for row in missing["issues"]) == 2

    staged = validate_workspace_draft(
        current=current, draft=draft, accounts=accounts.list(),
        provider_secrets=[],
        provider_secret_updates=[{"provider_id": "relay-main", "action": "replace", "value": "sk-provider"}],
    )
    assert staged["ok"] is True
    assert not any(row["severity"] == "error" for row in staged["issues"])
    assert any(row["scope"] == "reason" and row["id"] == "LLM" for row in staged["changes"])
