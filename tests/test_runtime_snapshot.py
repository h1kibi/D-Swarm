from __future__ import annotations

import pytest

from dswarm.solver.runtime_snapshot import (
    DockerImageInspector,
    ResolvedWorkerImage,
    RuntimeSnapshotBuildError,
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
