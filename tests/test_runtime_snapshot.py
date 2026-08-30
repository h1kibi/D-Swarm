from __future__ import annotations

from pathlib import Path

import pytest

from dswarm.solver.runtime_policy import build_runtime_policy
from dswarm.solver.runtime_snapshot import (
    DockerImageInspector,
    ResolvedWorkerImage,
    RuntimeSnapshotBuilder,
    RuntimeSnapshotBuildError,
    RuntimeSnapshotStore,
    validate_shared_worker_identity,
)


class FakeDocker:
    def __init__(self):
        self.calls: list[tuple] = []
        self.images: dict[str, str] = {}
        self.pull_fail = False
        self.identity: tuple[int, int] | None = (1000, 1000)

    def resolve_image(self, ref):
        self.calls.append(("resolve", ref))
        image_id = self.images.get(ref)
        return {"image_id": image_id} if image_id else None

    def pull_image(self, ref):
        self.calls.append(("pull", ref))
        if self.pull_fail:
            return False
        self.images[ref] = "sha256:" + ref[-1] * 8
        return True

    def query_user(self, image_id, user, *, network, mounts, env):
        self.calls.append(("identity", image_id, user, network, mounts, env))
        return self.identity


def test_identity_probe_has_no_network_mount_or_secret():
    docker = FakeDocker()
    docker.images["worker:a"] = "sha256:" + "a" * 8
    image = DockerImageInspector(docker).resolve("worker:a")
    assert (image.uid, image.gid) == (1000, 1000)
    identity_call = docker.calls[-1]
    assert identity_call[3:] == ("none", (), {})


def test_all_run_images_must_have_the_same_numeric_identity():
    images = [
        ResolvedWorkerImage("a", "sha256:a", 1000, 1000),
        ResolvedWorkerImage("b", "sha256:b", 1001, 1000),
    ]
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        validate_shared_worker_identity(images)
    assert exc.value.code == "worker_identity_mismatch"


def test_missing_image_is_pulled_once_then_inspected_again():
    docker = FakeDocker()
    inspector = DockerImageInspector(docker, allow_pull=True)
    first = inspector.resolve("worker:a")
    second = inspector.resolve("worker:a")
    assert first is second
    assert [call[0] for call in docker.calls] == [
        "resolve",
        "pull",
        "resolve",
        "identity",
    ]


def test_image_resolution_failure_is_structured_and_safe():
    docker = FakeDocker()
    docker.pull_fail = True
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        DockerImageInspector(docker, allow_pull=True).resolve("private/worker:secret-tag")
    assert exc.value.code == "image_resolution_failed"
    assert "private/worker" not in exc.value.safe_detail


def test_pull_is_not_attempted_when_policy_disallows_it():
    docker = FakeDocker()
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        DockerImageInspector(docker, allow_pull=False).resolve("worker:a")
    assert exc.value.code == "image_resolution_failed"
    assert docker.calls == [("resolve", "worker:a")]


@pytest.mark.parametrize("identity", [None, (0, 1000), (1000, 0), (-1, 1000)])
def test_missing_or_invalid_kali_identity_is_rejected(identity):
    docker = FakeDocker()
    docker.images["worker:a"] = "sha256:a"
    docker.identity = identity
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        DockerImageInspector(docker).resolve("worker:a")
    assert exc.value.code == "worker_identity_mismatch"


def test_identity_preflight_has_no_long_lived_or_provider_operation():
    docker = FakeDocker()
    docker.images["worker:a"] = "sha256:a"
    DockerImageInspector(docker).resolve("worker:a")
    assert {call[0] for call in docker.calls} == {"resolve", "identity"}
    assert all(call[0] not in {"create", "start", "provider"} for call in docker.calls)


def test_shared_identity_returns_the_only_numeric_pair():
    images = [
        ResolvedWorkerImage("a", "sha256:a", 1000, 1000),
        ResolvedWorkerImage("b", "sha256:b", 1000, 1000),
    ]
    assert validate_shared_worker_identity(images) == (1000, 1000)


def test_empty_worker_image_set_is_rejected():
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        validate_shared_worker_identity([])
    assert exc.value.code == "worker_identity_mismatch"



WEB_PROFILE = {
    "id": "pi-web",
    "name": "pi-web",
    "engine": "pi",
    "runtime": "docker-web",
    "image": "worker:a",
    "credential_account": "pi-web-main",
    "provider_ref": "deepseek",
    "model": "deepseek-chat",
}
PWN_PROFILE = {
    "id": "pi-pwn",
    "name": "pi-pwn",
    "engine": "pi",
    "runtime": "docker-pwn",
    "image": "worker:b",
    "credential_account": "pi-pwn-main",
    "provider_ref": "deepseek",
    "model": "deepseek-chat",
}
WEB_RUNTIME = {
    "id": "docker-web",
    "backend": "container",
    "network": "bridge",
    "cpus": "2.0",
    "memory": "2G",
    "pids_limit": 256,
    "tmpfs_bytes": 67108864,
}
PWN_RUNTIME = {
    "id": "docker-pwn",
    "backend": "container",
    "network": "none",
    "cpus": "4",
    "memory": "4g",
    "pids_limit": 512,
    "tmpfs_bytes": 134217728,
}


def snapshot_builder(*, image_ids=None):
    docker = FakeDocker()
    docker.images.update(image_ids or {"worker:a": "sha256:a", "worker:b": "sha256:b"})
    return RuntimeSnapshotBuilder(DockerImageInspector(docker, allow_pull=False))


def build_snapshot(run_id="run-1", **changes):
    values = {
        "run_id": run_id,
        "policy": build_runtime_policy(env={}),
        "worker_profiles": [WEB_PROFILE, PWN_PROFILE],
        "runtime_profiles": [WEB_RUNTIME, PWN_RUNTIME],
        "run_max_workers": 6,
    }
    values.update(changes)
    return snapshot_builder().build(**values)


def test_snapshot_freezes_image_id_pool_limit_and_binding_identity(tmp_path):
    snapshot = build_snapshot()
    assert len(snapshot.pools) == 2
    assert {pool.pool_max_concurrent_workers for pool in snapshot.pools} == {6}
    assert all(pool.resolved_image_id.startswith("sha256:") for pool in snapshot.pools)
    assert {pool.credential_binding_id for pool in snapshot.pools} == {
        "pi-web-main",
        "pi-pwn-main",
    }
    serialized = RuntimeSnapshotStore(tmp_path).create(snapshot).read_text("utf-8")
    for forbidden in ("API_KEY", "secret", str(Path.home()), ".pi"):
        assert forbidden not in serialized


def test_provider_bound_profile_falls_back_to_provider_ref_as_binding_id():
    profile = {
        "id": "pi-glm",
        "name": "pi-glm",
        "engine": "pi",
        "runtime": "docker-web",
        "image": "worker:a",
        # normalize_worker_profile clears credential_account whenever a
        # provider_ref is set; the provider binding owns the secret.
        "credential_account": "",
        "provider_ref": "zhipu",
        "model": "glm-5.3-flash",
    }
    snapshot = build_snapshot(
        worker_profiles=[profile], run_max_workers=2,
    )
    assert len(snapshot.pools) == 1
    pool = snapshot.pools[0]
    assert pool.provider_binding_id == "zhipu"
    assert pool.credential_binding_id == "zhipu"


def test_profile_without_any_credential_identity_is_rejected():
    profile = {
        "id": "pi-orphan",
        "name": "pi-orphan",
        "engine": "pi",
        "runtime": "docker-web",
        "image": "worker:a",
        "credential_account": "",
        "model": "glm-5.3-flash",
    }
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        build_snapshot(worker_profiles=[profile], run_max_workers=2)
    assert exc.value.code == "invalid_credential_binding_id"


def test_snapshot_is_create_once_and_tag_drift_does_not_rewrite_existing_run(tmp_path):
    store = RuntimeSnapshotStore(tmp_path)
    original = snapshot_builder(image_ids={"worker:a": "sha256:old"}).build(
        run_id="run-1",
        policy=build_runtime_policy(env={}),
        worker_profiles=[WEB_PROFILE],
        runtime_profiles=[WEB_RUNTIME],
        run_max_workers=3,
    )
    store.create(original)
    changed = snapshot_builder(image_ids={"worker:a": "sha256:new"}).build(
        run_id="run-1",
        policy=build_runtime_policy(env={}),
        worker_profiles=[WEB_PROFILE],
        runtime_profiles=[WEB_RUNTIME],
        run_max_workers=3,
    )
    with pytest.raises(RuntimeSnapshotBuildError, match="snapshot_already_exists"):
        store.create(changed)
    assert store.load("run-1").pools[0].resolved_image_id == "sha256:old"


def test_snapshot_rejects_pool_count_over_policy_cap():
    policy = build_runtime_policy(env={}, max_pools_per_run=1)
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        build_snapshot(policy=policy)
    assert exc.value.code == "max_pools_per_run_exceeded"


def test_snapshot_rejects_duplicate_profile_mapping():
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        build_snapshot(worker_profiles=[WEB_PROFILE, dict(WEB_PROFILE)])
    assert exc.value.code == "duplicate_profile_mapping"


def test_snapshot_pool_order_is_stable_by_profile_then_pool_id():
    snapshot = build_snapshot(worker_profiles=[PWN_PROFILE, WEB_PROFILE])
    assert [pool.profile_id for pool in snapshot.pools] == ["pi-pwn", "pi-web"]


def test_snapshot_store_fsyncs_and_uses_private_runtime_path(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("dswarm.solver.runtime_snapshot.os.fsync", lambda fd: calls.append(fd))
    path = RuntimeSnapshotStore(tmp_path).create(build_snapshot())
    assert path == tmp_path / "run-1" / ".runtime" / "pool-snapshot.v1.json"
    assert len(calls) >= 1
    assert all(".runtime" not in field for pool in build_snapshot().pools for field in pool.__dataclass_fields__)


def test_snapshot_store_rejects_path_traversal(tmp_path):
    store = RuntimeSnapshotStore(tmp_path)
    with pytest.raises(RuntimeSnapshotBuildError, match="invalid_run_id"):
        store.path_for("../other-run")


def test_snapshot_has_no_credential_version_or_secret_fields():
    snapshot = build_snapshot()
    assert "credential_version" not in snapshot.pools[0].__dataclass_fields__
    assert "secret" not in snapshot.pools[0].__dataclass_fields__


def test_host_and_named_networks_are_normalized():
    host = dict(WEB_RUNTIME, network="host")
    named = dict(PWN_RUNTIME, network="competition_net")
    snapshot = build_snapshot(runtime_profiles=[host, named])
    by_profile = {pool.profile_id: pool.network for pool in snapshot.pools}
    assert (by_profile["pi-web"].kind, by_profile["pi-web"].name) == ("host", "")
    assert (by_profile["pi-pwn"].kind, by_profile["pi-pwn"].name) == (
        "named",
        "competition_net",
    )


def test_snapshot_store_replace_failure_leaves_no_partial_final_file(tmp_path, monkeypatch):
    store = RuntimeSnapshotStore(tmp_path)
    monkeypatch.setattr(
        "dswarm.solver.runtime_snapshot.os.replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(RuntimeSnapshotBuildError, match="snapshot_write_failed"):
        store.create(build_snapshot())
    assert not store.path_for("run-1").exists()
