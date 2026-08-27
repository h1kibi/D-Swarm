from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dswarm.solver.cli_driver import CliResult
from dswarm.solver.container_pool import WorkerRuntimeLease
from dswarm.solver.container_runtime import ContainerGenerationIdentity, ContainerRuntimeExecutor
from dswarm.solver.poc_verifier import ContainerPocVerifier, ResolvedPocRegistration
from dswarm.swarm.poc_verification import reproduction_id_for


class FakeDockerPoolExecutor(ContainerRuntimeExecutor):
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.identity = ContainerGenerationIdentity(
            run_id="run-1",
            pool_id="pool-1",
            pool_instance_id="pool-instance-1",
            generation=3,
            resolved_image_id="sha256:test",
        )
        self.invocations: list[tuple[tuple[str, ...], Path]] = []

    async def run_registered_command(self, argv, *, host_cwd, timeout, worker_instance_id, env=None):
        cwd = Path(host_cwd)
        self.invocations.append((tuple(argv), cwd))
        assert cwd.parent == self.workspace / "verifiers"
        assert (cwd / "repro.sh").exists()
        return CliResult(
            text="container stdout: RUN_OK",
            raw_stderr="",
            runtime_status={"status": "finished", "rc": 0, "backend": "container_rcp"},
        )


class FakeCanonicalGraph:
    def __init__(self, row: dict[str, Any]):
        self.row = row

    def get_poc_reproduction(self, poc_id: str) -> dict[str, Any] | None:
        return dict(self.row) if poc_id == self.row["poc_id"] else None


def registration(workspace: Path) -> ResolvedPocRegistration:
    artifact = workspace / "shared" / "objects" / "ab" / "cd" / "artifact-docker"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("#!/bin/sh\necho RUN_OK\n", encoding="utf-8")
    command = "sh repro.sh"
    indicator = "RUN_OK"
    row = {
        "poc_id": "poc-docker",
        "reproduction_id": reproduction_id_for(
            artifact_id="artifact-docker", command=command, indicator=indicator
        ),
        "artifact_id": "artifact-docker",
        "path": "shared/objects/ab/cd/artifact-docker",
        "name": "repro.sh",
        "entry_command": command,
        "indicator": indicator,
    }
    return ResolvedPocRegistration.from_graph(
        FakeCanonicalGraph(row),
        poc_id="poc-docker",
        reproduction_id=row["reproduction_id"],
        workspace_root=workspace,
    )


@pytest.mark.asyncio
async def test_fake_docker_pool_executes_only_registered_command(tmp_path: Path):
    executor = FakeDockerPoolExecutor(tmp_path)
    released: list[bool] = []

    async def release_once() -> None:
        released.append(True)

    lease = WorkerRuntimeLease(
        pool_id="pool-1",
        pool_instance_id="pool-instance-1",
        generation=3,
        worker_instance_id="verifier-1",
        executor=executor,
        credential_projection=None,
        worker_env={},
        _release_once=release_once,
    )

    result = await ContainerPocVerifier().verify(registration(tmp_path), lease, timeout=10)

    assert result.verified is True
    assert result.status == "verified"
    assert len(executor.invocations) == 1
    argv, cwd = executor.invocations[0]
    assert argv == ("sh", "-c", "sh repro.sh")
    assert cwd.parent == tmp_path / "verifiers"
    assert released == [True]
