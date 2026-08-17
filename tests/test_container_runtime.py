from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from dswarm.solver.cli_driver import CliResult
from dswarm.solver.container_runtime import (
    CONTAINER_WORKSPACE,
    ContainerCreateRequest,
    ContainerGenerationIdentity,
    ContainerInspection,
    ContainerMount,
    ContainerRuntimeError,
    ContainerRuntimeExecutor,
)
from dswarm.solver.control_receiver import ExpectedRuntimeIdentity
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimeResourceSpec,
)


class FakeDriver:
    name = "pi"


class FakeLink:
    def __init__(self, name: str = "link") -> None:
        self.name = name
        self.alive = True
        self.signal_calls: list[tuple[str, str, float]] = []
        self.status_calls: list[tuple[str, float]] = []
        self.teardown_calls: list[float] = []

    def health(self, *, timeout: float) -> dict[str, Any]:
        return {"ok": True, "name": self.name}

    def signal(self, worker_id: str, name: str, *, timeout: float = 10.0) -> bool:
        self.signal_calls.append((worker_id, name, timeout))
        return True

    def status(self, worker_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
        self.status_calls.append((worker_id, timeout))
        return {"ok": True, "status": "running", "worker_id": worker_id}

    def teardown(self, *, timeout: float = 20.0) -> dict[str, Any]:
        self.teardown_calls.append(timeout)
        return {"ok": True, "remaining": 0}


class FakeReceiver:
    def __init__(self, *, link: FakeLink | None = None, wait_error: Exception | None = None) -> None:
        self.link = link or FakeLink()
        self.wait_error = wait_error
        self.issued: list[ExpectedRuntimeIdentity] = []
        self.waited: list[tuple[str, float]] = []
        self.revoked: list[str] = []

    def issue_pool(self, expected: ExpectedRuntimeIdentity) -> str:
        self.issued.append(expected)
        return "control-token-secret"

    def wait_pool(self, pool_instance_id: str, timeout: float) -> FakeLink:
        self.waited.append((pool_instance_id, timeout))
        if self.wait_error is not None:
            raise self.wait_error
        expected = self.issued[-1]
        self.link.run_id = expected.run_id
        self.link.pool_id = expected.pool_id
        self.link.pool_instance_id = expected.pool_instance_id
        self.link.generation = expected.generation
        self.link.protocol_version = expected.protocol_version
        return self.link

    def revoke_pool_instance(self, pool_instance_id: str) -> None:
        self.revoked.append(pool_instance_id)


class FakeDocker:
    def __init__(self) -> None:
        self.create_calls: list[ContainerCreateRequest] = []
        self.inspect_calls: list[str] = []
        self.remove_calls: list[tuple[str, bool]] = []
        self.inspection_mutator: Callable[[ContainerInspection], ContainerInspection] | None = None
        self.remove_result = True

    def create(self, request: ContainerCreateRequest) -> str:
        self.create_calls.append(request)
        return "container-123"

    def inspect(self, container_id: str) -> ContainerInspection:
        self.inspect_calls.append(container_id)
        request = self.create_calls[-1]
        inspection = ContainerInspection(
            container_id=container_id,
            image_id=request.image,
            labels=request.labels,
            mounts=request.mounts,
            network=request.network,
            uid=request.uid,
            gid=request.gid,
            running=True,
        )
        if self.inspection_mutator is not None:
            inspection = self.inspection_mutator(inspection)
        return inspection

    def remove(self, container_id: str, *, force: bool) -> bool:
        self.remove_calls.append((container_id, force))
        return self.remove_result


def pool_spec(*, network: RuntimeNetworkSpec | None = None) -> PoolSpec:
    return PoolSpec.with_computed_id(
        profile_id="pi-main",
        runtime_kind="pi",
        resolved_image_id="sha256:immutable",
        requested_image_ref="dswarm-worker-pi:latest",
        network=network or RuntimeNetworkSpec(kind="bridge"),
        resources=RuntimeResourceSpec(
            cpus="1.5",
            memory="2g",
            pids_limit=257,
            tmpfs_bytes=67_108_864,
        ),
        credential_binding_id="pi-main",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1001,
        gid=1002,
        runtime_features=("rcp-v2",),
        protocol_version=2,
        pool_max_concurrent_workers=4,
    )


async def ready_executor(
    tmp_path: Path,
    *,
    docker: FakeDocker | None = None,
    receiver: FakeReceiver | None = None,
    spec: PoolSpec | None = None,
    run_id: str = "run-a",
    generation: int = 1,
    run_rcp=None,
    run_streaming_rcp=None,
) -> ContainerRuntimeExecutor:
    return await ContainerRuntimeExecutor.create(
        run_id=run_id,
        pool_spec=spec or pool_spec(),
        generation=generation,
        run_root=tmp_path / run_id,
        docker=docker or FakeDocker(),
        receiver=receiver or FakeReceiver(),
        run_rcp=run_rcp,
        run_streaming_rcp=run_streaming_rcp,
        startup_timeout=3,
    )


@pytest.mark.asyncio
async def test_create_uses_exact_snapshot_identity_labels_and_mount_allowlist(tmp_path: Path):
    docker = FakeDocker()
    receiver = FakeReceiver()
    spec = pool_spec()

    executor = await ready_executor(tmp_path, docker=docker, receiver=receiver, spec=spec)

    create = docker.create_calls[0]
    assert create.image == "sha256:immutable"
    assert create.labels == {
        "com.dswarm.managed": "true",
        "com.dswarm.run_id": "run-a",
        "com.dswarm.pool_id": spec.pool_id,
        "com.dswarm.pool_instance_id": executor.pool_instance_id,
        "com.dswarm.generation": "1",
    }
    assert create.mounts == (
        ContainerMount(
            source=str((tmp_path / "run-a" / "workspace").resolve()),
            target=CONTAINER_WORKSPACE,
            read_only=False,
        ),
    )
    wire = json.dumps(create.snapshot(), sort_keys=True)
    assert "docker.sock" not in wire
    assert "/.runtime" not in wire.replace("\\", "/")
    assert "control-token-secret" not in wire
    assert receiver.issued == [
        ExpectedRuntimeIdentity(
            run_id="run-a",
            pool_id=spec.pool_id,
            pool_instance_id=executor.pool_instance_id,
            generation=1,
            expected_image_id="sha256:immutable",
            protocol_version=2,
        )
    ]


@pytest.mark.asyncio
async def test_create_freezes_named_network_resources_and_numeric_uid_gid(tmp_path: Path):
    docker = FakeDocker()
    spec = pool_spec(network=RuntimeNetworkSpec(kind="named", name="dswarm-workers"))

    await ready_executor(tmp_path, docker=docker, spec=spec)

    create = docker.create_calls[0]
    assert create.network == "dswarm-workers"
    assert create.cpus == "1.5"
    assert create.memory == "2g"
    assert create.pids_limit == 257
    assert create.tmpfs_bytes == 67_108_864
    assert (create.uid, create.gid) == (1001, 1002)
    assert create.user == "0:0"
    assert create.env["DSWARM_RUN_ID"] == "run-a"
    assert create.env["DSWARM_POOL_ID"] == spec.pool_id
    assert (
        create.env["DSWARM_POOL_INSTANCE_ID"]
        == create.labels["com.dswarm.pool_instance_id"]
    )
    assert create.env["DSWARM_POOL_GENERATION"] == "1"
    assert create.command[:2] == ("--connect", "host.docker.internal:9100")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["image_id", "labels", "network", "uid", "gid", "mounts"])
async def test_create_rejects_inspection_identity_mismatch(tmp_path: Path, field: str):
    docker = FakeDocker()
    receiver = FakeReceiver()

    def mutate(inspection: ContainerInspection) -> ContainerInspection:
        if field == "image_id":
            return replace(inspection, image_id="sha256:wrong")
        if field == "labels":
            return replace(
                inspection,
                labels={**inspection.labels, "com.dswarm.pool_id": "wrong"},
            )
        if field == "network":
            return replace(inspection, network="none")
        if field == "uid":
            return replace(inspection, uid=2001)
        if field == "gid":
            return replace(inspection, gid=2002)
        return replace(
            inspection,
            mounts=(replace(inspection.mounts[0], target="/wrong"),),
        )

    docker.inspection_mutator = mutate

    with pytest.raises(ContainerRuntimeError, match="runtime_identity_mismatch"):
        await ready_executor(tmp_path, docker=docker, receiver=receiver)

    assert docker.remove_calls == [("container-123", True)]
    assert len(receiver.revoked) == 1


@pytest.mark.asyncio
async def test_create_accepts_unrelated_image_labels_when_managed_labels_match(tmp_path: Path):
    docker = FakeDocker()

    def add_image_label(inspection: ContainerInspection) -> ContainerInspection:
        return replace(
            inspection,
            labels={**inspection.labels, "org.opencontainers.image.version": "v1"},
        )

    docker.inspection_mutator = add_image_label
    executor = await ready_executor(tmp_path, docker=docker)

    assert executor.container_id == "container-123"


@pytest.mark.asyncio
async def test_create_rejects_link_without_exact_generation_identity(tmp_path: Path):
    class MissingIdentityReceiver(FakeReceiver):
        def wait_pool(self, pool_instance_id: str, timeout: float) -> FakeLink:
            self.waited.append((pool_instance_id, timeout))
            return self.link

    docker = FakeDocker()
    receiver = MissingIdentityReceiver()

    with pytest.raises(ContainerRuntimeError, match="runtime_hello_failed"):
        await ready_executor(tmp_path, docker=docker, receiver=receiver)

    assert docker.remove_calls == [("container-123", True)]
    assert len(receiver.revoked) == 1


@pytest.mark.asyncio
async def test_failed_hello_removes_container_and_revokes_pool_instance(tmp_path: Path):
    docker = FakeDocker()
    receiver = FakeReceiver(wait_error=TimeoutError("secret transport detail"))

    with pytest.raises(ContainerRuntimeError, match="runtime_hello_failed") as caught:
        await ready_executor(tmp_path, docker=docker, receiver=receiver)

    assert caught.value.__cause__ is None
    assert "secret transport detail" not in str(caught.value)
    assert docker.remove_calls == [("container-123", True)]
    assert len(receiver.revoked) == 1


@pytest.mark.asyncio
async def test_to_container_path_maps_only_allowlisted_roots(tmp_path: Path):
    executor = await ready_executor(tmp_path)
    inside = tmp_path / "run-a" / "workspace" / "workers" / "w1"
    inside.mkdir(parents=True)

    assert executor.to_container_path(inside) == f"{CONTAINER_WORKSPACE}/workers/w1"
    assert executor.to_container_path(tmp_path / "run-a" / "workspace") == CONTAINER_WORKSPACE
    with pytest.raises(ContainerRuntimeError, match="host_path_not_mounted"):
        executor.to_container_path(tmp_path / "run-a" / ".runtime" / "secret")
    with pytest.raises(ContainerRuntimeError, match="host_path_not_mounted"):
        executor.to_container_path(tmp_path / "other-run")


@pytest.mark.asyncio
async def test_exec_record_never_exposes_argv_env_token_host_path_or_stderr(tmp_path: Path):
    calls: list[dict[str, Any]] = []

    def fake_run(driver, argv, **kwargs):
        calls.append({"driver": driver, "argv": argv, **kwargs})
        result = CliResult(text="ok", raw_stderr="raw provider secret")
        result.runtime_status = {
            "worker_id": "worker-runtime-id",
            "status": "finished",
            "rc": 0,
            "raw": "secret prompt raw provider secret",
        }
        return result

    executor = await ready_executor(tmp_path, run_rcp=fake_run)
    cwd = tmp_path / "run-a" / "workspace" / "workers" / "worker-1"
    cwd.mkdir(parents=True)
    result = await executor.run(
        driver=FakeDriver(),
        argv=[str(tmp_path / "host-pi"), "secret prompt"],
        host_cwd=cwd,
        timeout=5,
        env={"DEEPSEEK_API_KEY": "secret", "SAFE": "x"},
        worker_instance_id="worker-1",
        operation_kind="worker",
    )

    wire = json.dumps(result.runtime_status, sort_keys=True)
    assert "secret prompt" not in wire
    assert "DEEPSEEK_API_KEY" not in wire
    assert "raw provider secret" not in wire
    assert "control-token-secret" not in wire
    assert str(tmp_path) not in wire
    assert result.runtime_status["pool_instance_id"] == executor.pool_instance_id
    assert result.runtime_status["worker_instance_id"] == "worker-1"
    assert result.runtime_status["argv0"] == "pi"
    assert calls[0]["argv"] == ["pi", "secret prompt"]
    assert calls[0]["container_cwd"] == f"{CONTAINER_WORKSPACE}/workers/worker-1"
    assert calls[0]["link"] is executor.control_link


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_failure_code", "expected_failure_code"),
    [
        ("provider_timeout", "provider_timeout"),
        ("provider timeout: secret upstream detail", ""),
    ],
)
async def test_exec_record_projects_only_bounded_sanitized_failure_code(
    tmp_path: Path,
    raw_failure_code: str,
    expected_failure_code: str,
):
    def fake_run(*_args, **_kwargs):
        result = CliResult(text="failed")
        result.runtime_status = {
            "status": "failed",
            "rc": 1,
            "failure_code": raw_failure_code,
        }
        return result

    executor = await ready_executor(tmp_path, run_rcp=fake_run)
    cwd = tmp_path / "run-a" / "workspace" / "workers" / "worker-1"
    cwd.mkdir(parents=True)

    result = await executor.run(
        driver=FakeDriver(),
        argv=["pi", "secret prompt"],
        host_cwd=cwd,
        timeout=5,
        worker_instance_id="worker-1",
        operation_kind="worker",
    )

    assert result.runtime_status["failure_code"] == expected_failure_code
    assert "secret upstream detail" not in json.dumps(result.runtime_status, sort_keys=True)


@pytest.mark.asyncio
async def test_run_streaming_uses_bound_generation_link_and_forwards_controls(tmp_path: Path):
    calls: list[dict[str, Any]] = []
    observed_steps: list[str] = []
    cancel_event = object()
    proc_callback = object()
    steer_event = object()

    def fake_streaming_run(driver, argv, **kwargs):
        calls.append({"driver": driver, "argv": argv, **kwargs})
        kwargs["on_step"](SimpleNamespace(text="step-one"))
        result = CliResult(text="streamed")
        result.runtime_status = {"status": "finished", "rc": 0}
        return result

    executor = await ready_executor(tmp_path, run_streaming_rcp=fake_streaming_run)
    cwd = tmp_path / "run-a" / "workspace" / "workers" / "worker-stream"
    cwd.mkdir(parents=True)

    result = await executor.run_streaming(
        driver=FakeDriver(),
        argv=["pi", "secret prompt"],
        host_cwd=cwd,
        timeout=5,
        env={"DEEPSEEK_API_KEY": "secret"},
        on_step=lambda step: observed_steps.append(step.text),
        cancel_event=cancel_event,
        on_proc=proc_callback,
        steer_event=steer_event,
        worker_instance_id="worker-stream",
        operation_kind="worker",
    )

    assert result.text == "streamed"
    assert result.runtime_status["worker_instance_id"] == "worker-stream"
    assert observed_steps == ["step-one"]
    assert calls[0]["link"] is executor.control_link
    assert calls[0]["cancel_event"] is cancel_event
    assert calls[0]["on_proc"] is proc_callback
    assert calls[0]["steer_event"] is steer_event


@pytest.mark.asyncio
async def test_runtime_exec_failure_does_not_expose_transport_exception_chain(tmp_path: Path):
    def failing_run(*_args, **_kwargs):
        raise RuntimeError("secret provider transport detail")

    executor = await ready_executor(tmp_path, run_rcp=failing_run)
    cwd = tmp_path / "run-a" / "workspace" / "workers" / "worker-1"
    cwd.mkdir(parents=True)

    with pytest.raises(ContainerRuntimeError, match="runtime_exec_failed") as caught:
        await executor.run(
            driver=FakeDriver(),
            argv=["pi", "secret prompt"],
            host_cwd=cwd,
            timeout=5,
            worker_instance_id="worker-1",
            operation_kind="worker",
        )

    assert caught.value.__cause__ is None
    assert "secret provider transport detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_runtime_signal_failure_does_not_expose_control_exception_chain(tmp_path: Path):
    class FailingSignalLink(FakeLink):
        def signal(self, worker_id: str, name: str, *, timeout: float = 10.0) -> bool:
            del worker_id, name, timeout
            raise RuntimeError("secret signal transport detail")

    executor = await ready_executor(tmp_path, receiver=FakeReceiver(link=FailingSignalLink()))

    with pytest.raises(ContainerRuntimeError, match="runtime_signal_failed") as caught:
        await executor.signal("worker-1", "KILL")

    assert caught.value.__cause__ is None
    assert "secret signal transport detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_runtime_status_failure_does_not_expose_control_exception_chain(tmp_path: Path):
    class FailingStatusLink(FakeLink):
        def status(self, worker_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
            del worker_id, timeout
            raise RuntimeError("secret status transport detail")

    executor = await ready_executor(tmp_path, receiver=FakeReceiver(link=FailingStatusLink()))

    with pytest.raises(ContainerRuntimeError, match="runtime_status_failed") as caught:
        await executor.status("worker-1")

    assert caught.value.__cause__ is None
    assert "secret status transport detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_signal_and_status_route_only_through_bound_pool_link(tmp_path: Path):
    link = FakeLink("bound")
    receiver = FakeReceiver(link=link)
    executor = await ready_executor(tmp_path, receiver=receiver)

    assert await executor.signal("worker-1", "STOP") is True
    assert await executor.status("worker-1") == {
        "ok": True,
        "status": "running",
        "worker_id": "worker-1",
    }
    assert link.signal_calls == [("worker-1", "STOP", 10.0)]
    assert link.status_calls == [("worker-1", 10.0)]


@pytest.mark.asyncio
async def test_concurrent_executors_in_one_run_keep_pool_links_separate(tmp_path: Path):
    links = [FakeLink("first"), FakeLink("second")]

    class Receiver(FakeReceiver):
        def wait_pool(self, pool_instance_id: str, timeout: float) -> FakeLink:
            self.link = links[len(self.waited)]
            return super().wait_pool(pool_instance_id, timeout)

    receiver = Receiver()
    first = await ready_executor(tmp_path, receiver=receiver, run_id="same-run")
    second = await ready_executor(tmp_path, receiver=receiver, run_id="same-run", generation=2)

    await asyncio.gather(
        first.signal("worker-a", "CONT"),
        second.signal("worker-b", "KILL"),
    )
    assert links[0].signal_calls == [("worker-a", "CONT", 10.0)]
    assert links[1].signal_calls == [("worker-b", "KILL", 10.0)]


@pytest.mark.asyncio
async def test_terminate_require_proof_rejects_unproved_cleanup(tmp_path: Path):
    docker = FakeDocker()
    docker.remove_result = False
    receiver = FakeReceiver()
    executor = await ready_executor(tmp_path, docker=docker, receiver=receiver)

    with pytest.raises(ContainerRuntimeError, match="cleanup_unproven"):
        await executor.terminate(require_proof=True)

    assert receiver.revoked == [executor.pool_instance_id]
    assert docker.remove_calls == [("container-123", True)]


@pytest.mark.asyncio
async def test_generation_identity_is_frozen_and_safe(tmp_path: Path):
    executor = await ready_executor(tmp_path)
    assert executor.identity == ContainerGenerationIdentity(
        run_id="run-a",
        pool_id=pool_spec().pool_id,
        pool_instance_id=executor.pool_instance_id,
        generation=1,
        resolved_image_id="sha256:immutable",
    )
    with pytest.raises(Exception):
        executor.identity.generation = 2
