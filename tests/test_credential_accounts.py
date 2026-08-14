from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dswarm.solver.credential_accounts import (
    CONTAINER_ACCOUNTS_ROOT,
    CredentialAccountStore,
    account_store_root,
    ensure_pi_account_from_env,
    runtime_env_for_engine,
)
from apps.web.run_manager import RunManager
from dswarm.models.solve_graph import Challenge
from dswarm.solver.cli_solver import CliSolver
from dswarm.swarm.swarm import Swarm


def test_account_store_root_is_sessions_secret_side_table(tmp_path):
    assert account_store_root(tmp_path) == tmp_path / "_secrets" / "accounts"


def test_ensure_pi_account_from_env_writes_pi_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")

    assert ensure_pi_account_from_env(tmp_path) is True
    acct = CredentialAccountStore(account_store_root(tmp_path)).inspect("pi-main")
    assert acct is not None and acct.present
    key = (account_store_root(tmp_path) / "pi-main" / "API_KEY").read_text(
        encoding="utf-8"
    )
    assert key.strip() == "env-key"


def test_ensure_pi_account_from_env_creates_direction_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")

    assert ensure_pi_account_from_env(tmp_path) is True
    root = account_store_root(tmp_path)
    for account_id in (
        "pi-main",
        "pi-web-main",
        "pi-pwn-main",
        "pi-rev-main",
        "pi-crypto-main",
        "pi-misc-main",
        "pi-forensics-main",
        "pi-aisec-main",
    ):
        acct = CredentialAccountStore(root).inspect(account_id)
        assert acct is not None and acct.present


def test_ensure_pi_account_from_env_does_not_overwrite_existing_api_key(
    tmp_path, monkeypatch
):
    root = account_store_root(tmp_path)
    CredentialAccountStore(root).upsert_secret(
        account_id="pi-main", engine="pi", secret="custom-key"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")

    ensure_pi_account_from_env(tmp_path)

    key = (root / "pi-main" / "API_KEY").read_text(encoding="utf-8").strip()
    assert key == "custom-key"


def test_ensure_pi_account_from_env_respects_disable_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "0")

    assert ensure_pi_account_from_env(tmp_path) is False
    assert not (account_store_root(tmp_path) / "pi-main" / "API_KEY").exists()


def test_ensure_pi_account_from_env_never_overwrites_custom_endpoint(
    tmp_path, monkeypatch
):
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    store.upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="custom-key",
        base_url="https://custom.example/v1",
        target_engine="pi",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")

    assert ensure_pi_account_from_env(tmp_path) is True
    assert (root / "pi-main" / "API_KEY").read_text(encoding="utf-8").strip() == "custom-key"
    assert (root / "pi-main" / "BASE_URL").read_text(encoding="utf-8").strip() == "https://custom.example/v1"
    assert (root / "pi-web-main" / "API_KEY").exists()


def test_run_manager_auto_binds_pi_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "run-manager-key")
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "1")

    RunManager(sessions_root=tmp_path)

    key = (account_store_root(tmp_path) / "pi-main" / "API_KEY").read_text(
        encoding="utf-8"
    )
    assert key.strip() == "run-manager-key"


def test_pi_container_prefers_key_file_without_reading_secret(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-main"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("fake-key\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")

    resolved = runtime_env_for_engine("pi", account_root=root, container=True)

    assert resolved.account_id == "pi-main"
    assert resolved.env == {
        "DEEPSEEK_API_KEY_FILE": (
            f"{CONTAINER_ACCOUNTS_ROOT}/pi-main/API_KEY"
        )
    }


def test_pi_local_reads_account_key_for_subprocess_env(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-main"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("local-key\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")

    resolved = runtime_env_for_engine("pi", account_root=root, container=False)

    assert resolved.env == {"DEEPSEEK_API_KEY": "local-key"}


def test_pi_blank_account_id_skips_default_account(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-main"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("k\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    resolved = runtime_env_for_engine(
        "pi", account_root=root, account_id="", container=False)

    assert resolved.account_id == ""
    assert resolved.env == {}


def test_pi_api_key_file_and_env_fallback(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-main"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("key-secret\n")
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-secret")

    resolved = runtime_env_for_engine("pi", account_root=root, container=True)

    assert resolved.env == {
        "DEEPSEEK_API_KEY_FILE": f"{CONTAINER_ACCOUNTS_ROOT}/pi-main/API_KEY"
    }


def test_engine_account_id_can_be_overridden(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "team-pi"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("token\n")
    monkeypatch.setenv("DSWARM_PI_ACCOUNT_ID", "team-pi")
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")

    resolved = runtime_env_for_engine("pi", account_root=root, container=True)

    assert resolved.account_id == "team-pi"
    assert "team-pi" in resolved.env["DEEPSEEK_API_KEY_FILE"]


def test_runtime_env_accepts_explicit_profile_account_id(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-team"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("token\n")
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")

    resolved = runtime_env_for_engine(
        "pi", account_root=root, account_id="pi-team", container=True)

    assert resolved.account_id == "pi-team"
    assert resolved.env == {
        "DEEPSEEK_API_KEY_FILE": (
            f"{CONTAINER_ACCOUNTS_ROOT}/pi-team/API_KEY"
        )
    }


def test_credential_account_store_masks_and_replaces_material(tmp_path):
    store = CredentialAccountStore(account_store_root(tmp_path))

    pi_acct = store.upsert_secret(
        account_id="shared-main", engine="pi", secret="pi-secret")
    assert pi_acct["account_id"] == "shared-main"
    assert pi_acct["engine"] == "pi"
    assert pi_acct["details"]["has_secret"] is True
    assert "secret_value" not in pi_acct["details"]
    assert "pi-secret" not in json.dumps(pi_acct)
    # Trusted runtime consumers can still inspect the raw material in process.
    assert store.inspect("shared-main").details["secret_value"] == "pi-secret"

    # a custom-endpoint re-save replaces the key material in place.
    api = store.upsert_secret(
        account_id="shared-main", engine="api", secret="api-secret",
        base_url="https://api.deepseek.example/v1", target_engine="pi")
    assert api["engine"] == "pi"
    assert api["mode"] == "custom_endpoint"
    base = account_store_root(tmp_path) / "shared-main"
    assert (base / "API_KEY").read_text(encoding="utf-8").strip() == "api-secret"
    assert store.list()[0]["mode"] == "custom_endpoint"


def test_invalid_update_does_not_destroy_existing_account(tmp_path):
    store = CredentialAccountStore(account_store_root(tmp_path))
    store.upsert_secret(account_id="pi-main", engine="pi", secret="old-key")
    with pytest.raises(ValueError):
        store.upsert_secret(account_id="pi-main", engine="bogus", secret="x")

    key = account_store_root(tmp_path) / "pi-main" / "API_KEY"
    assert "old-key" in key.read_text(encoding="utf-8")


def test_custom_endpoint_account_maps_to_engine_specific_env(tmp_path, monkeypatch):
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    acct = store.upsert_secret(
        account_id="deepseek-main",
        engine="api",
        secret="deepseek-key",
        base_url="https://api.deepseek.example/v1",
        target_engine="pi",
    )
    assert acct["engine"] == "pi"
    assert acct["details"]["has_secret"] is True
    assert "secret_value" not in acct["details"]

    monkeypatch.setenv("DSWARM_PI_PROVIDER", "custom")
    pi_env = runtime_env_for_engine(
        "pi", account_root=root, account_id="deepseek-main", container=False)
    assert pi_env.env["OPENAI_API_KEY"] == "deepseek-key"
    assert pi_env.env["OPENAI_BASE_URL"] == "https://api.deepseek.example/v1"


def test_custom_endpoint_records_target_engine_for_binding(tmp_path, monkeypatch):
    """A custom endpoint registered FOR pi reports pi (not "api") so the panel can
    bind/display it — while runtime injection stays engine-agnostic."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    acct = store.upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="endpoint-key",
        base_url="https://openai.example/v1",
        target_engine="pi",
    )
    assert acct["engine"] == "pi"
    assert acct["mode"] == "custom_endpoint"
    assert acct["details"]["target_engine"] == "pi"
    assert acct["details"]["custom_endpoint"] is True
    # inspect() agrees, and accountForEngine-style lookup now matches pi.
    assert store.inspect("pi-main").engine == "pi"
    assert [a for a in store.list() if a["account_id"] == "pi-main"][0]["engine"] == "pi"

    # Engine-agnostic injection is preserved: the same dir still drives pi's env.
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "custom")
    env = runtime_env_for_engine(
        "pi", account_root=root, account_id="pi-main", container=False).env
    assert env["OPENAI_BASE_URL"] == "https://openai.example/v1"
    assert env["OPENAI_API_KEY"] == "endpoint-key"


def test_custom_endpoint_exposes_base_url_but_keeps_secret_write_only(tmp_path):
    """Public account metadata includes endpoint state, never raw credentials."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    acct = store.upsert_secret(
        account_id="deepseek-main", engine="api", secret="sk-super-secret",
        base_url="https://api.deepseek.com/v1", target_engine="pi")

    assert acct["details"]["base_url_value"] == "https://api.deepseek.com/v1"
    assert acct["details"]["base_url"] is True
    assert acct["details"]["has_secret"] is True
    assert "secret_value" not in acct["details"]
    # inspect() remains the trusted in-process path used by runtime code.
    assert store.inspect("deepseek-main").details["secret_value"] == "sk-super-secret"
    listed = [a for a in store.list() if a["account_id"] == "deepseek-main"][0]
    assert listed["details"]["base_url_value"] == "https://api.deepseek.com/v1"
    assert listed["details"]["has_secret"] is True
    assert "secret_value" not in listed["details"]
    assert "sk-super-secret" not in json.dumps(listed)


def test_custom_endpoint_without_base_url_reports_api_key_mode(tmp_path):
    """No BASE_URL on disk → a plain api_key account (not custom_endpoint), with
    no endpoint fields to mislead the panel."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    acct = store.upsert_secret(account_id="bare-main", engine="api", secret="k")
    assert acct["mode"] == "api_key"
    assert "base_url" not in acct["details"]
    assert "base_url_value" not in acct["details"]


def test_pi_account_secret_is_write_only_in_public_metadata(tmp_path):
    """Public writes report presence while inspect() retains trusted secret access."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)

    pi_acct = store.upsert_secret(account_id="pi-main", engine="pi", secret="pi-key")
    assert pi_acct["details"]["has_secret"] is True
    assert "secret_value" not in pi_acct["details"]
    assert store.inspect("pi-main").details["secret_value"] == "pi-key"

    # an empty account (no material) has no secret_value key at all
    (root / "ghost").mkdir()
    ghost = store.inspect("ghost")
    assert ghost is not None and ghost.present is False
    assert "secret_value" not in (ghost.details or {})


def test_custom_endpoint_without_target_engine_stays_api(tmp_path):
    """Back-compat: no target agent → engine "api" (legacy/programmatic accounts)."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    acct = store.upsert_secret(
        account_id="shared-endpoint", engine="api", secret="k", base_url="https://x/v1")
    assert acct["engine"] == "api"
    assert acct["details"]["target_engine"] is None


def test_custom_endpoint_invalid_target_engine_rejected(tmp_path):
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    with pytest.raises(ValueError):
        store.upsert_secret(
            account_id="x-main", engine="api", secret="k", target_engine="gpt")


def test_resaving_account_as_pi_keeps_pi_marker_and_drops_endpoint(tmp_path):
    """Switching a custom-endpoint account back to a plain pi key account must
    drop the endpoint material (BASE_URL) while the pi ENGINE marker stays."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    store.upsert_secret(
        account_id="pi-main", engine="api", secret="k",
        base_url="https://x/v1", target_engine="pi")
    assert (root / "pi-main" / "ENGINE").exists()
    again = store.upsert_secret(
        account_id="pi-main", engine="pi", secret="pi-token")
    assert not (root / "pi-main" / "BASE_URL").exists()
    assert again["mode"] == "api_key"
    assert again["engine"] == "pi"


def test_blank_secret_edit_preserves_custom_endpoint_key(tmp_path):
    """Metadata-only edit: re-saving a custom endpoint with a blank secret keeps the
    stored API_KEY while updating base_url — the UI never reads the key back, so a
    base_url change must not require re-pasting it."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    store.upsert_secret(
        account_id="deepseek-main", engine="api", secret="kept-key",
        base_url="https://old.example/v1", target_engine="pi")

    edited = store.upsert_secret(
        account_id="deepseek-main", engine="api", secret="",
        base_url="https://new.example/v1")

    base = root / "deepseek-main"
    assert (base / "API_KEY").read_text(encoding="utf-8").strip() == "kept-key"
    assert (base / "BASE_URL").read_text(encoding="utf-8").strip() == "https://new.example/v1"
    # target_engine left blank on edit → preserved from the prior save.
    assert edited["details"]["target_engine"] == "pi"
    assert (base / "ENGINE").read_text(encoding="utf-8").strip() == "pi"


def test_blank_secret_edit_preserves_target_engine_when_base_url_unchanged(tmp_path):
    """Re-saving with a blank secret and blank base_url keeps both the key and
    the base_url, and the stored pi target survives."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    store.upsert_secret(
        account_id="ep-main", engine="api", secret="kept",
        base_url="https://x/v1", target_engine="pi")

    edited = store.upsert_secret(
        account_id="ep-main", engine="api", secret="", target_engine="pi")

    base = root / "ep-main"
    assert (base / "API_KEY").read_text(encoding="utf-8").strip() == "kept"
    assert (base / "BASE_URL").read_text(encoding="utf-8").strip() == "https://x/v1"
    assert edited["details"]["target_engine"] == "pi"


def test_blank_secret_edit_preserves_pi_and_api_keys(tmp_path):
    """Blank-secret re-save of a pi/api account keeps the key rather than erroring
    on the required-secret guard."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)

    store.upsert_secret(account_id="pi-main", engine="pi", secret="pi-x")
    store.upsert_secret(account_id="pi-main", engine="pi", secret="")
    assert (root / "pi-main" / "API_KEY").read_text(
        encoding="utf-8").strip() == "pi-x"

    store.upsert_secret(account_id="api-main", engine="api", secret="api-x",
                        base_url="https://x/v1", target_engine="pi")
    store.upsert_secret(account_id="api-main", engine="api", secret="",
                        target_engine="pi")
    assert (root / "api-main" / "API_KEY").read_text(
        encoding="utf-8").strip() == "api-x"


def test_blank_secret_on_new_account_still_errors(tmp_path):
    """The preserve path must NOT weaken account creation: a blank secret with no
    prior account on disk still raises the required-secret error."""
    root = account_store_root(tmp_path)
    store = CredentialAccountStore(root)
    for engine in ("pi", "api"):
        with pytest.raises(ValueError):
            store.upsert_secret(account_id=f"fresh-{engine}", engine=engine, secret="")


def test_local_runtime_does_not_override_host_home(tmp_path):
    ch = Challenge(
        id="home-local",
        name="home-local",
        category="misc",
        description="local home",
        flag_format="flag{...}",
    )
    swarm = Swarm(ch, [], llm=None, sandbox=None, worker_root=tmp_path / "workers")

    env = swarm._runtime_env_for("pi", "cli-pi", container=None)

    assert "HOME" not in env


def test_swarm_worker_profile_selects_credential_account_and_runtime(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts"
    acct = root / "pi-team"
    acct.mkdir(parents=True)
    (acct / "API_KEY").write_text("token\n")
    ch = Challenge(
        id="profile-runtime",
        name="profile-runtime",
        category="misc",
        description="profile runtime",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_backend="local",
        credential_accounts_root=root,
        runtime_profiles=[
            {"id": "local", "backend": "local"},
            {"id": "docker-web", "backend": "container"},
        ],
        worker_profiles=[{
            "id": "pi-sub-container",
            "engine": "pi",
            "runtime": "docker-web",
            "credential_account": "pi-team",
            "enabled": True,
        }],
    )

    class FakeHandle:
        def to_container_path(self, path: str) -> str:
            return "/home/kali/workspace/" + path.rsplit("/", 1)[-1]

    profile = swarm._profile_for_engine("pi")
    assert profile["credential_account"] == "pi-team"
    assert swarm._backend_for_engine("pi", profile) == "container"
    env = swarm._runtime_env_for("pi", "cli-pi", container=FakeHandle(), profile=profile)
    assert env["ANTHROPIC_API_KEY_FILE"].endswith(
        "/pi-team/API_KEY")
    assert env["DSWARM_WORKER_PROFILE_ID"] == "pi-sub-container"
    assert env["DSWARM_CREDENTIAL_ACCOUNT_ID"] == "pi-team"
    assert env["HOME"].startswith("/home/kali/workspace/")


def test_pi_provider_env_overrides_host_provider_in_argv():
    """route A P3: the per-worker env (DSWARM_PI_PROVIDER=ctf-gateway for gateway
    workers) must REPLACE the host-built --provider flag — otherwise pi calls the
    real deepseek endpoint with the task token as its key (instant 401)."""
    ch = Challenge(
        id="pi-provider",
        name="pi-provider",
        category="web",
        description="pi provider",
        flag_format="flag{...}",
    )
    solver = CliSolver(None, ch, engine="pi")
    # argv built by PiDriver from the HOST env (which said "deepseek")
    argv = ["/usr/local/bin/pi", "--mode", "json", "--session-dir", ".pi-sessions",
            "--provider", "deepseek", "PROMPT"]

    out = solver._apply_runtime_argv(
        argv, {"DSWARM_PI_PROVIDER": "ctf-gateway", "DEEPSEEK_API_KEY": "tok"})

    i = out.index("--provider")
    assert out[i + 1] == "ctf-gateway"
    assert out[-1] == "PROMPT"  # prompt stays last, nothing else shifted


def test_pi_provider_env_inserts_provider_when_argv_has_none():
    """When the host env never set a provider, the worker env still decides."""
    ch = Challenge(
        id="pi-provider-insert",
        name="pi-provider-insert",
        category="web",
        description="pi provider insert",
        flag_format="flag{...}",
    )
    solver = CliSolver(None, ch, engine="pi")
    argv = ["/usr/local/bin/pi", "--mode", "json", "--session-dir", ".pi-sessions", "PROMPT"]

    out = solver._apply_runtime_argv(argv, {"DSWARM_PI_PROVIDER": "ctf-gateway"})

    assert out[-3:] == ["--provider", "ctf-gateway", "PROMPT"]


def test_pi_provider_env_absent_keeps_host_argv_untouched():
    """No worker-env provider → byte-identical argv (local workers keep their
    host default; the P2 non-gateway path is unchanged)."""
    ch = Challenge(
        id="pi-provider-none",
        name="pi-provider-none",
        category="web",
        description="pi provider none",
        flag_format="flag{...}",
    )
    solver = CliSolver(None, ch, engine="pi")
    argv = ["/usr/local/bin/pi", "--mode", "json", "--session-dir", ".pi-sessions",
            "--provider", "deepseek", "PROMPT"]

    out = solver._apply_runtime_argv(argv, {"DSWARM_WORKER_MODEL": ""})

    assert out == argv


def test_profile_model_is_inserted_before_prompt_for_cli_drivers():
    ch = Challenge(
        id="profile-model",
        name="profile-model",
        category="misc",
        description="profile model",
        flag_format="flag{...}",
    )
    solver = CliSolver(None, ch, engine="pi")
    argv = ["/usr/local/bin/pi", "--mode", "json", "--session-dir", ".pi-sessions", "PROMPT"]

    out = solver._apply_runtime_argv(argv, {"DSWARM_WORKER_MODEL": "deepseek-reasoner"})

    assert out[-3:] == ["--model", "deepseek-reasoner", "PROMPT"]


def test_swarm_profile_roles_and_capacity_are_hard_limits(tmp_path):
    ch = Challenge(
        id="profile-capacity",
        name="profile-capacity",
        category="misc",
        description="profile capacity",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-main",
                "engine": "pi",
                "runtime": "local",
                "credential_account": "pi-main",
                "auth": "subscription",
                "roles": ["bootstrap", "explore"],
                "race": False,
                "max_running": 1,
                "enabled": True,
            }
        ],
    )

    assert swarm._profile_for_engine("pi", role="race", advance=False) is None
    profile = swarm._profile_for_engine("pi", role="bootstrap")
    assert profile is not None
    swarm._claim_worker_account("cli-pi", "pi", profile)

    assert swarm._profile_for_engine("pi", role="bootstrap", advance=False) is None
    assert swarm._engine_available_for_role("pi", "bootstrap") is False
    with pytest.raises(RuntimeError):
        swarm._make_cli_worker("pi", mode="bootstrap")

    class Done:
        solver_id = "cli-pi"

    swarm._release_worker_account(Done())
    assert swarm._engine_available_for_role("pi", "bootstrap") is True


def test_review_profile_capacity_is_isolated_from_explore_capacity(tmp_path):
    ch = Challenge(
        id="profile-review-capacity",
        name="profile-review-capacity",
        category="misc",
        description="profile capacity",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-main",
                "engine": "pi",
                "runtime": "local",
                "credential_account": "pi-main",
                "auth": "subscription",
                "roles": ["bootstrap", "explore", "review"],
                "race": False,
                "max_running": 1,
                "enabled": True,
            }
        ],
        stage_policy={"coordinator": {"review": {"enabled": True, "max_concurrent": 1}}},
    )

    review_profile = swarm._profile_for_engine("pi", role="review")
    assert review_profile is not None
    swarm._claim_worker_account(
        "cli-pi-review", "pi", review_profile, role="review")

    assert swarm._profile_for_engine("pi", role="explore", advance=False) is not None
    assert swarm._engine_available_for_role("pi", "explore") is True
    assert swarm._profile_for_engine("pi", role="review", advance=False) is None


@pytest.mark.asyncio
async def test_done_review_task_keeps_slot_until_profile_release(tmp_path):
    ch = Challenge(
        id="profile-review-reap-window",
        name="profile-review-reap-window",
        category="misc",
        description="profile capacity",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-main",
                "engine": "pi",
                "runtime": "local",
                "credential_account": "pi-main",
                "roles": ["review"],
                "enabled": True,
            }
        ],
        stage_policy={
            "coordinator": {
                "review": {
                    "enabled": True,
                    "engine": "pi-main",
                    "max_concurrent": 1,
                    "allow_review_fallback": False,
                }
            }
        },
    )
    profile = swarm._profile_for_engine("pi-main", role="review")
    assert profile is not None
    swarm._claim_worker_account(
        "cli-pi-review", "pi", profile, role="review")

    async def finished_review():
        return None

    task = asyncio.create_task(finished_review())
    await task
    assert task.done()
    swarm._active_review_tasks.add(task)

    assert swarm._review_capacity_available() is False
    assert task in swarm._active_review_tasks

    class Done:
        solver_id = "cli-pi-review"

    swarm._release_worker_account(Done())
    swarm._active_review_tasks.discard(task)
    assert swarm._review_capacity_available() is True
    assert swarm._select_review_engine(["pi"]) == "pi-main"


def test_review_engine_profile_id_uses_base_engine_health(tmp_path):
    ch = Challenge(
        id="profile-review-id-health",
        name="profile-review-id-health",
        category="misc",
        description="profile review id health",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-sub-container",
                "engine": "pi",
                "runtime": "docker-web",
                "credential_account": "pi-main",
                "auth": "subscription",
                "roles": ["review"],
                "race": False,
                "max_running": 1,
                "enabled": True,
            }
        ],
        stage_policy={
            "coordinator": {
                "review": {
                    "enabled": True,
                    "engine": "pi-sub-container",
                    "max_concurrent": 1,
                    "allow_review_fallback": False,
                }
            }
        },
    )

    assert swarm._healthy_matches("pi-sub-container", ["pi"]) is True
    assert swarm._healthy_matches("pi-sub-container", ["pi-sub-container"]) is True
    assert swarm._select_review_engine(["pi"]) == "pi-sub-container"
    assert swarm._select_review_engine(["pi-sub-container"]) == "pi-sub-container"


def test_pick_engine_uses_configured_profile_roster_with_base_health(tmp_path):
    ch = Challenge(
        id="profile-pick-base-health",
        name="profile-pick-base-health",
        category="misc",
        description="profile pick base health",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-web-sub",
                "engine": "pi",
                "runtime": "docker-web",
                "roles": ["bootstrap", "explore", "review"],
                "enabled": True,
            },
            {
                "id": "pi-pwn-sub",
                "engine": "pi",
                "runtime": "docker-web",
                "roles": ["bootstrap", "explore", "review"],
                "enabled": True,
            },
        ],
    )

    assert swarm.engines == ["pi-pwn-sub", "pi-web-sub"]  # sorted by profile name
    # healthy-role candidates come back sorted by profile name
    assert swarm._healthy_role_candidates(["pi"], role="bootstrap") == [
        "pi-pwn-sub",
        "pi-web-sub",
    ]
    assert swarm._pick_engine([], ["pi"], role="bootstrap") == "pi-pwn-sub"
    # pi-only: a running pi worker marks every pi profile as running (the running
    # match falls back to the base engine), so the least-loaded fallback returns
    # the first candidate — still a valid pi profile, never a dead engine.
    assert swarm._pick_engine(["pi-pwn-sub"], ["pi"], role="bootstrap") == "pi-pwn-sub"


@pytest.mark.asyncio
async def test_worker_cmd_spawn_base_engine_resolves_to_configured_profile(
    tmp_path, monkeypatch,
):
    ch = Challenge(
        id="profile-worker-cmd-base",
        name="profile-worker-cmd-base",
        category="misc",
        description="profile worker cmd base",
        flag_format="flag{...}",
    )
    queue: asyncio.Queue = asyncio.Queue()
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-sub-container",
                "engine": "pi",
                "runtime": "docker-web",
                "roles": ["bootstrap", "explore", "review"],
                "enabled": True,
            }
        ],
        worker_cmds=queue,
    )
    spawned: list[str] = []
    emitted: list[tuple[str, dict]] = []

    class FakeWorker:
        solver_id = "cli-pi"

        async def run(self):
            await asyncio.sleep(3600)

    def fake_make(engine, **kwargs):
        spawned.append(engine)
        return FakeWorker()

    async def emit_bb(kind, **fields):
        emitted.append((kind, fields))

    monkeypatch.setattr(swarm, "_make_cli_worker", fake_make)
    await queue.put({"action": "spawn", "engine": "pi"})
    tasks: dict[asyncio.Task, str] = {}
    task_solvers: dict[asyncio.Task, FakeWorker] = {}

    try:
        await swarm._apply_worker_cmds(
            tasks=tasks,
            task_solvers=task_solvers,
            healthy=["pi"],
            running_engines_fn=lambda: [],
            emit_bb=emit_bb,
        )
        assert spawned == ["pi-sub-container"]
        assert list(tasks.values()) == ["pi-sub-container"]
        assert emitted[-1][0] == "worker_spawned"
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.keys(), return_exceptions=True)


def test_ordinary_profile_capacity_is_isolated_from_review_capacity(tmp_path):
    ch = Challenge(
        id="profile-ordinary-capacity",
        name="profile-ordinary-capacity",
        category="misc",
        description="profile capacity",
        flag_format="flag{...}",
    )
    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_profiles=[
            {
                "id": "pi-main",
                "engine": "pi",
                "runtime": "local",
                "credential_account": "pi-main",
                "auth": "subscription",
                "roles": ["bootstrap", "explore", "review"],
                "race": False,
                "max_running": 1,
                "max_review_running": 2,
                "enabled": True,
            }
        ],
        stage_policy={"coordinator": {"review": {"enabled": True, "max_concurrent": 1}}},
    )

    ordinary_profile = swarm._profile_for_engine("pi", role="explore")
    assert ordinary_profile is not None
    swarm._claim_worker_account(
        "cli-pi-explore", "pi", ordinary_profile, role="explore")

    assert swarm._profile_for_engine("pi", role="explore", advance=False) is None
    first_review = swarm._profile_for_engine("pi", role="review", advance=False)
    assert first_review is not None
    swarm._claim_worker_account(
        "cli-pi-review-1", "pi", first_review, role="review")
    second_review = swarm._profile_for_engine("pi", role="review", advance=False)
    assert second_review is not None
    swarm._claim_worker_account(
        "cli-pi-review-2", "pi", second_review, role="review")
    assert swarm._profile_for_engine("pi", role="review", advance=False) is None


def test_swarm_runtime_profile_options_reach_container_create(tmp_path, monkeypatch):
    ch = Challenge(
        id="runtime-options",
        name="runtime-options",
        category="misc",
        description="runtime options",
        flag_format="flag{...}",
    )
    seen = {}

    class FakeHandle:
        def to_container_path(self, path: str) -> str:
            return path

    def fake_ensure_container(*args, **kwargs):
        seen.update(kwargs)
        return FakeHandle()

    import dswarm.solver.container_exec as ce
    monkeypatch.setattr(ce, "ensure_container", fake_ensure_container)

    swarm = Swarm(
        ch, [], llm=None, sandbox=None,
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_backend="local",
        runtime_profiles=[{
            "id": "docker-web",
            "backend": "container",
            "network": "bridge",
            "memory": "10g",
            "cpus": "3",
            "pids_limit": 1024,
        }],
        worker_profiles=[{
            "id": "pi-api",
            "engine": "pi",
            "runtime": "docker-web",
            "credential_account": "pi-main",
            "enabled": True,
        }],
    )
    profile = swarm._profile_for_engine("pi")
    swarm._container_for_engine("pi", profile)

    assert seen["network"] == "bridge"
    assert seen["memory"] == "10g"
    assert seen["cpus"] == "3"
    assert seen["pids_limit"] == 1024


def test_swarm_container_failure_emits_runtime_degraded_and_falls_back(tmp_path, monkeypatch):
    ch = Challenge(id="runtime-degraded", name="runtime-degraded", category="misc",
                   description="", flag_format="flag{...}")
    events = []

    class FakeBus:
        async def emit(self, ev):
            events.append(ev)

    def boom(*args, **kwargs):
        raise RuntimeError("docker unavailable")

    import dswarm.solver.container_exec as ce
    monkeypatch.setattr(ce, "ensure_container", boom)

    swarm = Swarm(
        ch, [], llm=None, sandbox=None, bus=FakeBus(),
        worker_root=tmp_path / "run" / "workspace" / "workers",
        worker_backend="container",
    )

    async def go():
        assert swarm._container_for_engine("pi") is None
        await asyncio.sleep(0)

    asyncio.run(go())
    assert swarm._backend_for_engine("pi") == "local"
    assert swarm._runtime_degraded[0]["status"] == "degraded"
    assert events[0].payload["kind"] == "runtime_degraded"
    assert "docker unavailable" in events[0].payload["reason"]


# ── detect_system_login (DESIGN §2.3 補強B) ──────────────────────────────────

def test_detect_system_login_pi_key_env(monkeypatch):
    """pi's host-side login is a present provider key (DEEPSEEK_API_KEY etc.),
    checked directly — no keychain/file probing."""
    from dswarm.solver import credential_accounts as ca
    assert ca.detect_system_login("pi", env={"DEEPSEEK_API_KEY": "x"}) == "present"
    # a truthy env WITHOUT the provider key reads as absent (env={} would fall
    # back to the process environment, which may legitimately have a key)
    assert ca.detect_system_login("pi", env={"PATH": "/usr/bin"}) == "absent"


def test_detect_system_login_pi_provider_env_key_wins(monkeypatch):
    from dswarm.solver import credential_accounts as ca
    # pi's provider env keys decide the login: deepseek key present → present
    assert ca.detect_system_login(
        "pi", env={"DEEPSEEK_API_KEY": "x"}) == "present"
    # an unrelated key does not count
    assert ca.detect_system_login(
        "pi", env={"OPENAI_API_KEY": "x"}) == "absent"


def test_detect_system_login_never_raises_on_probe_failure(monkeypatch):
    from dswarm.solver import credential_accounts as ca
    # unknown engines are reported as "unknown", never raised
    assert ca.detect_system_login("bogus-engine", env={}) == "unknown"
    # a bad env mapping still never raises
    assert ca.detect_system_login("pi", env=None) in ("present", "absent")



