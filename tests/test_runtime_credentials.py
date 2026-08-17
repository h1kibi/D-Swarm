from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from dswarm.solver.credential_accounts import CredentialAccountStore
from dswarm.solver.runtime_credentials import (
    CONTAINER_CREDENTIAL_ROOT,
    CredentialProjectionCleanupError,
    CredentialProjectionError,
    CredentialProjector,
)


def make_accounts(tmp_path: Path, values: dict[str, str]) -> CredentialAccountStore:
    store = CredentialAccountStore(tmp_path / "accounts")
    for account_id, secret in values.items():
        store.upsert_secret(account_id=account_id, engine="pi", secret=secret)
    return store


def projector(tmp_path: Path) -> CredentialProjector:
    store = make_accounts(tmp_path, {"pi-main": "main-key"})
    return CredentialProjector(store.root, tmp_path / "sessions")


def test_direct_projection_contains_only_selected_binding(tmp_path):
    store = make_accounts(
        tmp_path, {"pi-web-main": "web-key", "pi-pwn-main": "pwn-key"}
    )
    lease = CredentialProjector(store.root, tmp_path / "sessions").project(
        run_id="r",
        pool_id="pool-web",
        worker_instance_id="worker-1",
        binding_id="pi-web-main",
        credential_mode="direct",
    )

    assert lease.root is not None
    assert sorted(path.name for path in lease.root.iterdir()) == ["pi-web-main"]
    assert "web-key" in (
        lease.root / "pi-web-main" / "API_KEY"
    ).read_text(encoding="utf-8")
    projected_text = "".join(
        path.read_text(encoding="utf-8")
        for path in lease.root.rglob("*")
        if path.is_file()
    )
    assert "pwn-key" not in projected_text


def test_missing_binding_never_falls_back_to_env_or_another_account(
    tmp_path, monkeypatch
):
    store = make_accounts(tmp_path, {"pi-main": "other-key"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "host-key")

    with pytest.raises(CredentialProjectionError) as exc:
        CredentialProjector(store.root, tmp_path / "sessions").project(
            run_id="r",
            pool_id="p",
            worker_instance_id="w",
            binding_id="deleted",
            credential_mode="direct",
        )

    assert exc.value.code == "credential_binding_unavailable"
    assert "host-key" not in repr(exc.value)
    assert "other-key" not in repr(exc.value)


def test_gateway_projection_has_no_provider_secret_files(tmp_path):
    lease = projector(tmp_path).project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w",
        binding_id="pi-main",
        credential_mode="gateway",
    )

    assert lease.root is None
    assert lease.env == {}
    assert lease.credential_version_digest


def test_projection_uses_private_unique_per_worker_roots_and_0600_files(tmp_path):
    projected = projector(tmp_path)
    first = projected.project(
        run_id="r",
        pool_id="p",
        worker_instance_id="worker-1",
        binding_id="pi-main",
        credential_mode="direct",
    )
    second = projected.project(
        run_id="r",
        pool_id="p",
        worker_instance_id="worker-2",
        binding_id="pi-main",
        credential_mode="direct",
    )

    assert first.root is not None and second.root is not None
    assert first.root != second.root
    assert first.root == (
        tmp_path
        / "sessions"
        / "r"
        / ".runtime"
        / "pools"
        / "p"
        / "workers"
        / "worker-1"
        / "credentials"
    )
    secret_mode = stat.S_IMODE((first.root / "pi-main" / "API_KEY").stat().st_mode)
    if os.name == "nt":
        # Windows chmod exposes only the read-only bit through st_mode; the
        # private parent directory remains the isolation boundary.
        assert secret_mode & stat.S_IRUSR
        assert secret_mode & stat.S_IWUSR
    else:
        assert secret_mode == 0o600
    assert first.env == {
        "DSWARM_CREDENTIAL_ROOT": CONTAINER_CREDENTIAL_ROOT,
        "DSWARM_CREDENTIAL_BINDING_ID": "pi-main",
    }


def test_projection_close_is_idempotent_and_removes_operation_root(tmp_path):
    lease = projector(tmp_path).project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w",
        binding_id="pi-main",
        credential_mode="direct",
    )
    assert lease.root is not None
    worker_root = lease.root.parent

    lease.close()
    lease.close()

    assert not worker_root.exists()
    assert lease.closed is True


def test_projection_cleanup_failure_is_structured_and_retryable(tmp_path, monkeypatch):
    lease = projector(tmp_path).project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w",
        binding_id="pi-main",
        credential_mode="direct",
    )
    real_rmtree = __import__("shutil").rmtree
    calls = 0

    def fail_once(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private path")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("dswarm.solver.runtime_credentials.shutil.rmtree", fail_once)

    with pytest.raises(CredentialProjectionCleanupError) as exc:
        lease.close()
    assert exc.value.code == "credential_cleanup_failed"
    assert "private path" not in repr(exc.value)
    assert lease.closed is False

    lease.close()
    assert lease.closed is True


def test_projection_metadata_does_not_expose_host_paths_or_secret(tmp_path):
    lease = projector(tmp_path).project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w",
        binding_id="pi-main",
        credential_mode="direct",
    )
    public = repr((lease.env, lease.credential_version_digest, lease.binding_id))
    assert str(Path.home()) not in public
    assert str(tmp_path) not in public
    assert "main-key" not in public
    assert all(value.startswith("/run/dswarm/") or value == "pi-main" for value in lease.env.values())


def test_secret_rotation_changes_version_digest_without_changing_binding(tmp_path):
    store = make_accounts(tmp_path, {"pi-main": "first-key"})
    projected = CredentialProjector(store.root, tmp_path / "sessions")
    first = projected.project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w1",
        binding_id="pi-main",
        credential_mode="direct",
    )
    first.close()

    store.upsert_secret(account_id="pi-main", engine="pi", secret="second-key")
    second = projected.project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w2",
        binding_id="pi-main",
        credential_mode="direct",
    )

    assert first.binding_id == second.binding_id == "pi-main"
    assert first.credential_version_digest != second.credential_version_digest


@pytest.mark.parametrize(
    ("field", "value"),
    [("run_id", "../r"), ("pool_id", "p/q"), ("worker_instance_id", "..")],
)
def test_projection_rejects_unsafe_private_path_identity(tmp_path, field, value):
    kwargs = {
        "run_id": "r",
        "pool_id": "p",
        "worker_instance_id": "w",
        "binding_id": "pi-main",
        "credential_mode": "direct",
    }
    kwargs[field] = value

    with pytest.raises(CredentialProjectionError) as exc:
        projector(tmp_path).project(**kwargs)
    assert exc.value.code == "invalid_projection_identity"


def test_custom_projection_preserves_only_allowlisted_binding_files(tmp_path):
    store = CredentialAccountStore(tmp_path / "accounts")
    store.upsert_secret(
        account_id="custom-main",
        engine="api",
        secret="custom-key",
        base_url="https://provider.example/v1",
        target_engine="pi",
    )
    (store.root / "custom-main" / "UNRELATED").write_text("do-not-copy", encoding="utf-8")

    lease = CredentialProjector(store.root, tmp_path / "sessions").project(
        run_id="r",
        pool_id="p",
        worker_instance_id="w",
        binding_id="custom-main",
        credential_mode="custom",
    )

    assert lease.root is not None
    assert sorted(path.name for path in (lease.root / "custom-main").iterdir()) == [
        "API_KEY",
        "BASE_URL",
        "ENGINE",
    ]
