from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dswarm.solver.container_runtime import ContainerInspection, ContainerMount
from dswarm.solver.runtime_cleanup import (
    RuntimeCleanupExpectation,
    RuntimeCleanupInspector,
    cleanup_pool_generation,
)


INSTANCE = "123e4567-e89b-42d3-a456-426614174000"


def expected_state(**overrides: Any) -> RuntimeCleanupExpectation:
    values = dict(
        container_id="container-1",
        run_id="run-a",
        pool_id="pool-a",
        pool_instance_id=INSTANCE,
        generation=2,
        image_id="sha256:immutable",
        network="bridge",
        mounts=(
            ContainerMount("/sessions/run-a/workspace", "/home/kali/workspace", False),
            ContainerMount("/sessions/run-a/private", "/home/kali/private", True),
        ),
        private_state_mounts=(
            ContainerMount("/sessions/run-a/private", "/home/kali/private", True),
        ),
        worker_token_ids=("worker-token-a", "worker-token-b"),
    )
    values.update(overrides)
    return RuntimeCleanupExpectation(**values)


def managed_inspect(**overrides: Any) -> ContainerInspection:
    expected = expected_state(**overrides)
    labels = {
        "com.dswarm.managed": "true",
        "com.dswarm.run_id": expected.run_id,
        "com.dswarm.pool_id": expected.pool_id,
        "com.dswarm.pool_instance_id": expected.pool_instance_id,
        "com.dswarm.generation": str(expected.generation),
    }
    return ContainerInspection(
        container_id=expected.container_id,
        image_id=expected.image_id,
        labels=labels,
        mounts=expected.mounts,
        network=expected.network,
        uid=1001,
        gid=1002,
        running=True,
    )


class FakeDocker:
    def __init__(self, inspection: ContainerInspection | None = None) -> None:
        self.inspection = inspection or managed_inspect()
        self.inspect_calls: list[str] = []
        self.list_calls: list[dict[str, str]] = []
        self.remove_calls: list[tuple[str, bool]] = []
        self.remove_result = True
        self.post_remove: ContainerInspection | None = None
        self.inspect_error: Exception | None = None
        self.list_result: list[str] = []

    def inspect(self, container_id: str) -> ContainerInspection:
        self.inspect_calls.append(container_id)
        if self.inspect_error is not None:
            raise self.inspect_error
        if self.remove_calls and self.post_remove is None:
            raise LookupError("container not found")
        return self.post_remove or self.inspection

    def list(self, **filters: str) -> list[str]:
        self.list_calls.append(filters)
        return list(self.list_result)

    def remove(self, container_id: str, *, force: bool) -> bool:
        self.remove_calls.append((container_id, force))
        return self.remove_result


class FakeReceiver:
    def __init__(self) -> None:
        self.pool_revoked: list[str] = []
        self.worker_revoked: list[str] = []

    def revoke_pool_instance(self, pool_instance_id: str) -> None:
        self.pool_revoked.append(pool_instance_id)

    def revoke_token(self, token: str) -> None:
        self.worker_revoked.append(token)


def test_managed_container_requires_every_exact_label_and_private_state_match():
    inspected = managed_inspect()
    verdict = RuntimeCleanupInspector().inspect_candidate(
        inspected, expected=expected_state()
    )
    assert verdict.safe_to_remove is True
    assert verdict.reasons == ()


@pytest.mark.parametrize("field", ["run_id", "pool_id", "pool_instance_id", "generation"])
def test_label_mismatch_is_never_removed(field: str):
    value = "other" if field != "generation" else 99
    expected = expected_state()
    inspected = managed_inspect(**{field: value})
    docker = FakeDocker(inspection=inspected)
    result = cleanup_pool_generation(docker=docker, expected=expected)
    assert result.removed is False
    assert result.proven is False
    assert docker.remove_calls == []


def test_name_substring_alone_is_not_cleanup_evidence():
    docker = FakeDocker()
    docker.inspection = replace(docker.inspection, labels={})
    result = cleanup_pool_generation(docker=docker, expected=expected_state())
    assert result.removed is False
    assert docker.remove_calls == []


def test_mount_image_network_and_private_state_mismatch_are_not_removable():
    expected = expected_state()
    for inspected in (
        replace(managed_inspect(), image_id="sha256:other"),
        replace(managed_inspect(), network="other-network"),
        replace(managed_inspect(), mounts=()),
    ):
        result = cleanup_pool_generation(
            docker=FakeDocker(inspection=inspected), expected=expected
        )
        assert result.proven is False
        assert result.removed is False

    expected_private = expected
    verdict = RuntimeCleanupInspector().inspect_candidate(
        replace(managed_inspect(), mounts=(expected.mounts[0],)), expected=expected_private
    )
    assert verdict.safe_to_remove is False
    assert "private_state_mount_mismatch" in verdict.reasons


def test_invalid_identity_is_not_removable():
    inspected = managed_inspect(pool_instance_id="bad instance")
    result = cleanup_pool_generation(docker=FakeDocker(inspection=inspected), expected=expected_state())
    assert result.proven is False
    assert result.removed is False


def test_success_requires_post_remove_absence_and_revokes_all_exact_tokens():
    docker = FakeDocker()
    receiver = FakeReceiver()
    result = cleanup_pool_generation(
        docker=docker, expected=expected_state(), receiver=receiver
    )
    assert result.removed is True
    assert result.absence_proven is True
    assert result.proven is True
    assert receiver.pool_revoked == [INSTANCE]
    assert receiver.worker_revoked == ["worker-token-a", "worker-token-b"]
    assert docker.remove_calls == [("container-1", True)]


def test_already_absent_container_has_explicit_absence_evidence():
    docker = FakeDocker()
    docker.inspect_error = LookupError("no such container")
    receiver = FakeReceiver()
    result = cleanup_pool_generation(
        docker=docker, expected=expected_state(), receiver=receiver
    )
    assert result.removed is True
    assert result.absence_proven is True
    assert result.proven is True
    assert docker.remove_calls == []
    assert receiver.pool_revoked == [INSTANCE]


def test_remove_failure_is_unproven_but_revocation_still_happens():
    docker = FakeDocker()
    docker.remove_result = False
    receiver = FakeReceiver()
    result = cleanup_pool_generation(
        docker=docker, expected=expected_state(), receiver=receiver
    )
    assert result.removed is False
    assert result.proven is False
    assert "remove_failed" in result.failures
    assert receiver.pool_revoked == [INSTANCE]
    assert receiver.worker_revoked == ["worker-token-a", "worker-token-b"]


def test_post_remove_absence_failure_is_unproven():
    docker = FakeDocker()
    docker.post_remove = managed_inspect()
    result = cleanup_pool_generation(docker=docker, expected=expected_state())
    assert result.removed is True
    assert result.absence_proven is False
    assert result.proven is False
    assert "absence_unproven" in result.failures


def test_inspect_failure_never_removes_but_revokes_tokens():
    docker = FakeDocker()
    docker.inspect_error = RuntimeError("secret docker detail")
    receiver = FakeReceiver()
    result = cleanup_pool_generation(
        docker=docker, expected=expected_state(), receiver=receiver
    )
    assert result.removed is False
    assert result.proven is False
    assert "inspect_failed" in result.failures
    assert receiver.pool_revoked == [INSTANCE]
    assert receiver.worker_revoked == ["worker-token-a", "worker-token-b"]
    assert all("secret" not in failure for failure in result.failures)


def test_cleanup_does_not_touch_other_run_container():
    expected = expected_state()
    other = managed_inspect(run_id="other-run")
    docker = FakeDocker(inspection=other)
    result = cleanup_pool_generation(docker=docker, expected=expected)
    assert result.proven is False
    assert docker.remove_calls == []


def test_cleanup_failure_is_local_and_other_pool_can_be_processed():
    first = FakeDocker(inspection=managed_inspect(run_id="run-a", pool_id="pool-a"))
    first.inspection = replace(first.inspection, labels={})
    second_expected = expected_state(pool_id="pool-b", container_id="container-2")
    second = FakeDocker(inspection=managed_inspect(pool_id="pool-b", container_id="container-2"))
    first_result = cleanup_pool_generation(docker=first, expected=expected_state())
    second_result = cleanup_pool_generation(docker=second, expected=second_expected)
    assert first_result.proven is False
    assert second_result.proven is True
    assert second.remove_calls == [("container-2", True)]


def test_worker_token_revoke_failure_is_retained_and_blocks_proof():
    class FailingReceiver(FakeReceiver):
        def revoke_token(self, token: str) -> None:
            super().revoke_token(token)
            raise RuntimeError("secret token detail")

    receiver = FailingReceiver()
    result = cleanup_pool_generation(
        docker=FakeDocker(), expected=expected_state(), receiver=receiver
    )
    assert result.proven is False
    assert "worker_token_revoke_failed" in result.failures
    assert all("secret" not in failure for failure in result.failures)


def test_adapter_inspect_error_can_be_resolved_by_exact_empty_list():
    class InspectFailure(RuntimeError):
        code = "container_inspect_failed"

    docker = FakeDocker()
    docker.inspect_error = InspectFailure("sanitized")
    result = cleanup_pool_generation(docker=docker, expected=expected_state())
    assert result.proven is True
    assert result.absence_proven is True
    assert docker.list_calls == [{"container_id": "container-1"}]


def test_sanitized_inspect_failure_without_list_is_not_absence_proof():
    class InspectFailure(RuntimeError):
        code = "container_inspect_failed"

    class NoListDocker:
        def inspect(self, container_id: str):
            raise InspectFailure("sanitized")

        def remove(self, container_id: str, *, force: bool) -> bool:
            raise AssertionError("remove must not be called")

    result = cleanup_pool_generation(docker=NoListDocker(), expected=expected_state())
    assert result.proven is False
    assert result.absence_proven is False
    assert "inspect_failed" in result.failures


def test_pool_and_worker_token_revokers_can_be_independent():
    class PoolReceiver:
        def __init__(self):
            self.revoked = []

        def revoke_pool_instance(self, pool_instance_id: str) -> None:
            self.revoked.append(pool_instance_id)

    class WorkerRevoker:
        def __init__(self):
            self.revoked = []

        def revoke_token(self, token: str) -> None:
            self.revoked.append(token)

    pool_receiver = PoolReceiver()
    worker_revoker = WorkerRevoker()
    result = cleanup_pool_generation(
        docker=FakeDocker(),
        expected=expected_state(),
        receiver=pool_receiver,
        worker_token_revoker=worker_revoker,
    )
    assert result.proven is True
    assert pool_receiver.revoked == [INSTANCE]
    assert worker_revoker.revoked == ["worker-token-a", "worker-token-b"]


def test_uuid_like_but_non_v4_pool_instance_identity_is_not_removable():
    invalid = "123e4567-e89b-12d3-a456-426614174000"
    inspected = managed_inspect(pool_instance_id=invalid)
    expected = expected_state(pool_instance_id=invalid)
    verdict = RuntimeCleanupInspector().inspect_candidate(inspected, expected=expected)
    assert verdict.safe_to_remove is False
    assert "pool_instance_identity_invalid" in verdict.reasons
