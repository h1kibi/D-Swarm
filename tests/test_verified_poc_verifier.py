from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from dswarm.solver.cli_driver import CliResult
from dswarm.solver.container_pool import WorkerRuntimeLease
from dswarm.solver.container_runtime import ContainerGenerationIdentity, ContainerRuntimeExecutor
from dswarm.solver.poc_verifier import (
    ContainerPocVerifier,
    ResolvedPocRegistration,
)
from dswarm.swarm.poc_verification import reproduction_id_for


class FakeExecutor(ContainerRuntimeExecutor):
    def __init__(self, result: CliResult | Exception):
        self.result = result
        self.identity = ContainerGenerationIdentity(
            run_id="run-1",
            pool_id="pool-1",
            pool_instance_id="pool-instance-1",
            generation=3,
            resolved_image_id="sha256:test",
        )
        self.calls: list[dict[str, Any]] = []

    async def run_registered_command(self, argv, *, host_cwd, timeout, worker_instance_id, env=None):
        self.calls.append({
            "argv": tuple(argv),
            "host_cwd": Path(host_cwd),
            "timeout": timeout,
            "worker_instance_id": worker_instance_id,
            "env": dict(env or {}),
        })
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class LocalExecutor(FakeExecutor):
    is_docker_runtime = False


def make_lease(executor: Any) -> WorkerRuntimeLease:
    released: list[bool] = []

    async def release_once() -> None:
        released.append(True)

    lease = WorkerRuntimeLease(
        pool_id="pool-1",
        pool_instance_id="pool-instance-1",
        generation=3,
        worker_instance_id="worker-1",
        executor=executor,
        credential_projection=None,
        worker_env={"SAFE": "1"},
        _release_once=release_once,
    )
    lease._test_released = released  # type: ignore[attr-defined]
    return lease


class FakeCanonicalGraph:
    def __init__(self, row: dict[str, Any]):
        self.row = dict(row)

    def get_poc_reproduction(self, poc_id: str) -> dict[str, Any] | None:
        return dict(self.row) if poc_id == self.row["poc_id"] else None


def resolve_registration(row: dict[str, Any], workspace: Path) -> ResolvedPocRegistration:
    return ResolvedPocRegistration.from_graph(
        FakeCanonicalGraph(row),
        poc_id=row["poc_id"],
        reproduction_id=row["reproduction_id"],
        workspace_root=workspace,
    )


def make_registration(tmp_path: Path) -> ResolvedPocRegistration:
    workspace = tmp_path / "workspace"
    artifact = workspace / "shared" / "objects" / "ab" / "cd" / "artifact-1"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("print('source-only POC_OK')\n", encoding="utf-8")
    command = "python3 poc.py"
    indicator = "POC_OK"
    row = {
        "poc_id": "poc-1",
        "reproduction_id": reproduction_id_for(
            artifact_id="artifact-1", command=command, indicator=indicator
        ),
        "artifact_id": "artifact-1",
        "path": "shared/objects/ab/cd/artifact-1",
        "name": "poc.py",
        "entry_command": command,
        "indicator": indicator,
    }
    return resolve_registration(row, workspace)


@pytest.mark.asyncio
async def test_verifier_accepts_only_graph_resolved_registration_and_no_free_command(tmp_path: Path):
    registration = make_registration(tmp_path)
    executor = FakeExecutor(CliResult(text="POC_OK", runtime_status={"status": "finished", "rc": 0}))
    lease = make_lease(executor)

    with pytest.raises(TypeError):
        await ContainerPocVerifier().verify(object(), lease, timeout=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await ContainerPocVerifier().verify(registration, lease, timeout=5, command="evil")  # type: ignore[call-arg]

    with pytest.raises(FrozenInstanceError):
        registration.entry_command = "evil"  # type: ignore[misc]


def test_registration_has_no_public_raw_row_factory():
    assert not hasattr(ResolvedPocRegistration, "from_graph_row")


def test_graph_resolution_rejects_unsafe_or_oversized_registered_command(tmp_path: Path):
    registration = make_registration(tmp_path)
    base = {
        "poc_id": registration.poc_id,
        "reproduction_id": registration.reproduction_id,
        "artifact_id": registration.artifact_id,
        "path": "shared/objects/ab/cd/artifact-1",
        "name": registration.artifact_name,
        "indicator": registration.indicator,
    }
    for command in ("python3 poc.py\x00", "python3 poc.py\nwhoami", "x" * 4097):
        row = {
            **base,
            "entry_command": command,
            "reproduction_id": reproduction_id_for(
                artifact_id=registration.artifact_id,
                command=command,
                indicator=registration.indicator,
            ),
        }
        with pytest.raises(ValueError, match="registered command"):
            resolve_registration(row, registration.workspace_root)


def test_graph_resolution_rejects_absolute_or_escaping_artifact_path(tmp_path: Path):
    registration = make_registration(tmp_path)
    base = {
        "poc_id": registration.poc_id,
        "reproduction_id": registration.reproduction_id,
        "artifact_id": registration.artifact_id,
        "entry_command": registration.entry_command,
        "indicator": registration.indicator,
    }
    for graph_path in ("C:/host-secret", "/host-secret", "shared/../../host-secret"):
        with pytest.raises(ValueError, match="artifact path"):
            resolve_registration(
                {**base, "path": graph_path}, registration.workspace_root
            )


def test_verifier_rejects_timeout_above_hard_cap(tmp_path: Path):
    registration = make_registration(tmp_path)
    lease = make_lease(FakeExecutor(CliResult(text="", runtime_status={"status": "finished", "rc": 0})))

    with pytest.raises(ValueError, match="600"):
        __import__("asyncio").run(ContainerPocVerifier().verify(registration, lease, timeout=601))


def test_verifier_rejects_nonclean_runtime_status_even_with_marker(tmp_path: Path):
    registration = make_registration(tmp_path)
    result = CliResult(text="POC_OK", runtime_status={"status": "failed", "rc": 0})

    outcome = ContainerPocVerifier._normalize_runtime_result(registration, result)

    assert outcome.status == "execution_error"
    assert outcome.verified is False


def test_verifier_rejects_incomplete_runtime_provenance_even_with_marker(tmp_path: Path):
    registration = make_registration(tmp_path)
    result = CliResult(
        text="POC_OK",
        runtime_status={"status": "finished", "rc": 0, "oom_killed": True},
    )

    outcome = ContainerPocVerifier._normalize_runtime_result(registration, result)

    assert outcome.status == "execution_error"
    assert outcome.verified is False


@pytest.mark.asyncio
async def test_verifier_rejects_local_executor_and_releases_lease(tmp_path: Path):
    registration = make_registration(tmp_path)
    lease = make_lease(LocalExecutor(CliResult(text="POC_OK", runtime_status={"status": "finished", "rc": 0})))

    result = await ContainerPocVerifier().verify(registration, lease, timeout=5)

    assert result.status == "docker_runtime_unavailable"
    assert lease.released
    assert not lease.executor.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected",
    [
        (CliResult(text="real output POC_OK", raw_stderr="", runtime_status={"status": "finished", "rc": 0}), "verified"),
        (CliResult(text="real output", raw_stderr="warning POC_OK", runtime_status={"status": "finished", "rc": 0}), "verified"),
        (CliResult(text="POC_OK", runtime_status={"status": "finished", "rc": 7}), "nonzero_exit"),
        (CliResult(text="POC_OK", timed_out=True, runtime_status={"status": "timeout", "rc": 124, "timed_out": True}), "timed_out"),
        (CliResult(text="POC_OK", cancelled=True, runtime_status={"status": "cancelled", "rc": None, "cancelled": True}), "cancelled"),
        (CliResult(text="no marker", runtime_status={"status": "finished", "rc": 0}), "indicator_not_observed"),
        (CliResult(text="POC_OK", runtime_status={}), "provenance_unavailable"),
    ],
)
async def test_verifier_normalizes_runtime_result_and_releases_lease(
    tmp_path: Path, result: CliResult, expected: str
):
    registration = make_registration(tmp_path)
    executor = FakeExecutor(result)
    lease = make_lease(executor)

    outcome = await ContainerPocVerifier().verify(registration, lease, timeout=9)

    assert outcome.status == expected
    assert lease.released
    assert executor.calls == [
        {
            "argv": ("sh", "-c", "python3 poc.py"),
            "host_cwd": executor.calls[0]["host_cwd"],
            "timeout": 9,
            "worker_instance_id": "worker-1",
            "env": {"SAFE": "1"},
        }
    ]
    staged_cwd = executor.calls[0]["host_cwd"]
    assert staged_cwd.parent == registration.workspace_root / "verifiers"
    assert (staged_cwd / "poc.py").read_text(encoding="utf-8") == "print('source-only POC_OK')\n"
    assert not (staged_cwd / "poc.py").stat().st_mode & 0o222


@pytest.mark.asyncio
async def test_verifier_does_not_count_indicator_in_source_or_other_non_runtime_text(tmp_path: Path):
    registration = make_registration(tmp_path)
    executor = FakeExecutor(CliResult(text="", runtime_status={"status": "finished", "rc": 0}))
    lease = make_lease(executor)

    outcome = await ContainerPocVerifier().verify(registration, lease, timeout=5)

    assert outcome.status == "indicator_not_observed"
    assert outcome.verified is False


@pytest.mark.asyncio
async def test_verifier_reports_missing_artifact_without_execution(tmp_path: Path):
    registration = make_registration(tmp_path)
    registration = resolve_registration(
        {
            "poc_id": registration.poc_id,
            "reproduction_id": registration.reproduction_id,
            "artifact_id": registration.artifact_id,
            "path": "shared/objects/ef/01/artifact-1",
            "name": registration.artifact_name,
            "entry_command": registration.entry_command,
            "indicator": registration.indicator,
        },
        registration.workspace_root,
    )
    executor = FakeExecutor(CliResult(text="POC_OK", runtime_status={"status": "finished", "rc": 0}))
    lease = make_lease(executor)

    outcome = await ContainerPocVerifier().verify(registration, lease, timeout=5)

    assert outcome.status == "artifact_unavailable"
    assert not executor.calls
    assert lease.released


@pytest.mark.asyncio
async def test_verifier_maps_runtime_exception_without_host_fallback(tmp_path: Path):
    registration = make_registration(tmp_path)
    executor = FakeExecutor(RuntimeError("host subprocess must not be used"))
    lease = make_lease(executor)

    outcome = await ContainerPocVerifier().verify(registration, lease, timeout=5)

    assert outcome.status == "execution_error"
    assert "host subprocess" not in outcome.diagnostics
    assert lease.released


@pytest.mark.asyncio
async def test_verifier_releases_lease_and_propagates_direct_cancellation(tmp_path: Path):
    registration = make_registration(tmp_path)

    class CancellingExecutor(FakeExecutor):
        async def run_registered_command(self, *args, **kwargs):
            raise __import__("asyncio").CancelledError

    lease = make_lease(CancellingExecutor(CliResult(text="", runtime_status={})))

    with pytest.raises(__import__("asyncio").CancelledError):
        await ContainerPocVerifier().verify(registration, lease, timeout=5)

    assert lease.released
