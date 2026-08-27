"""Docker-only execution adapter for graph-resolved Verified-PoC reproductions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping

from dswarm.solver.cli_driver import CliResult
from dswarm.solver.container_pool import WorkerRuntimeLease
from dswarm.solver.container_runtime import (
    ContainerRuntimeError,
    ContainerRuntimeExecutor,
)
from dswarm.swarm.poc_verification import (
    VerificationFailure,
    normalize_reproduction_indicator,
    reproduction_id_for,
)

_GRAPH_RESOLUTION = object()
_MAX_COMMAND_CHARS = 4096
_MAX_TIMEOUT_SECONDS = 600.0
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class ResolvedPocRegistration:
    """Immutable registration resolved from a canonical workspace graph row."""

    poc_id: str
    reproduction_id: str
    artifact_id: str
    workspace_root: Path
    artifact_path: Path
    artifact_name: str
    entry_command: str
    indicator: str
    argv: tuple[str, ...]
    _resolution_origin: object

    @classmethod
    def from_graph(
        cls,
        graph: Any,
        *,
        poc_id: str,
        reproduction_id: str,
        workspace_root: str | Path,
    ) -> "ResolvedPocRegistration":
        """Resolve one immutable registration from the canonical graph boundary."""
        getter = getattr(graph, "get_poc_reproduction", None)
        if not callable(getter):
            raise TypeError("canonical graph reproduction lookup is required")
        row = getter(str(poc_id))
        if not isinstance(row, Mapping) or str(row.get("reproduction_id") or "") != str(reproduction_id):
            raise ValueError("missing graph reproduction registration")
        return cls._from_graph_row(row, workspace_root=workspace_root)

    @classmethod
    def _from_graph_row(
        cls,
        row: Mapping[str, Any],
        *,
        workspace_root: str | Path,
    ) -> "ResolvedPocRegistration":
        """Build a registration after the canonical graph lookup has completed."""
        if not isinstance(row, Mapping):
            raise TypeError("graph row is required")
        poc_id = str(row.get("poc_id") or "").strip()
        reproduction_id = str(row.get("reproduction_id") or "").strip()
        artifact_id = str(row.get("artifact_id") or "").strip()
        # Do not retain a compatibility command alias here: the registration
        # contract names this immutable POC_SAVE field explicitly.
        entry_command = str(row.get("entry_command") or "").strip()
        indicator = normalize_reproduction_indicator(str(row.get("indicator") or ""))
        if not poc_id or not reproduction_id or not artifact_id or not entry_command:
            raise ValueError("incomplete graph reproduction registration")
        if (
            len(entry_command) > _MAX_COMMAND_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in entry_command)
        ):
            raise ValueError("invalid registered command")
        expected_id = reproduction_id_for(
            artifact_id=artifact_id,
            command=entry_command,
            indicator=indicator,
        )
        if reproduction_id != expected_id:
            raise ValueError("graph reproduction identity mismatch")
        workspace = Path(workspace_root).resolve(strict=False)
        artifact_path = _resolve_graph_artifact_path(
            workspace=workspace,
            raw_path=row.get("path"),
            artifact_id=artifact_id,
        )
        artifact_name = _normalize_artifact_name(row.get("name"))
        return cls(
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            artifact_id=artifact_id,
            workspace_root=workspace,
            artifact_path=artifact_path,
            artifact_name=artifact_name,
            entry_command=entry_command,
            indicator=indicator,
            # The registered string is passed unchanged as the only shell input.
            # The fixed wrapper itself is adapter-owned and not caller-controlled.
            argv=("sh", "-c", entry_command),
            _resolution_origin=_GRAPH_RESOLUTION,
        )

    def __post_init__(self) -> None:
        if self._resolution_origin is not _GRAPH_RESOLUTION:
            raise TypeError("registration must be graph-resolved")
        if not self.poc_id or not self.reproduction_id or not self.artifact_id:
            raise ValueError("invalid resolved registration")
        if not isinstance(self.workspace_root, Path) or not isinstance(self.artifact_path, Path):
            raise TypeError("graph artifact paths must be Path instances")
        if _normalize_artifact_name(self.artifact_name) != self.artifact_name:
            raise ValueError("invalid graph artifact name")
        try:
            self.artifact_path.relative_to(self.workspace_root / "shared" / "objects")
        except ValueError as exc:
            raise ValueError("invalid graph artifact path") from exc


@dataclass(frozen=True)
class VerifierExecutionResult:
    """Bounded result of one container verifier execution."""

    status: str
    exit_code: int | None = None
    observed_location: str = ""
    provenance_artifact_ids: tuple[str, ...] = ()
    diagnostics: str = ""
    elapsed_ms: int | None = None
    # Internal-only runtime provenance.  The orchestration layer must never copy
    # these raw corpora to graph/UI payloads.
    stdout: str = ""
    stderr: str = ""

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    @property
    def failure_reason(self) -> VerificationFailure | None:
        if self.verified:
            return None
        try:
            return VerificationFailure(self.status)
        except ValueError:
            return VerificationFailure.EXECUTION_ERROR


class ContainerPocVerifier:
    """Run only the immutable graph command through a Docker pool lease."""

    async def verify(
        self,
        registration: ResolvedPocRegistration,
        lease: WorkerRuntimeLease,
        *,
        timeout: float,
    ) -> VerifierExecutionResult:
        if not isinstance(registration, ResolvedPocRegistration):
            raise TypeError("graph-resolved registration is required")
        if not isinstance(lease, WorkerRuntimeLease):
            raise TypeError("Docker runtime lease is required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or timeout > _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(f"timeout must be between 0 and {_MAX_TIMEOUT_SECONDS:g} seconds")

        try:
            executor = lease.executor
            if not _matches_current_container_lease(executor, lease):
                return VerifierExecutionResult(
                    status=VerificationFailure.DOCKER_RUNTIME_UNAVAILABLE.value
                )
            try:
                host_cwd = await asyncio.to_thread(_stage_artifact, registration)
            except (OSError, ValueError):
                return VerifierExecutionResult(
                    status=VerificationFailure.ARTIFACT_UNAVAILABLE.value
                )
            try:
                result = await executor.run_registered_command(
                    registration.argv,
                    host_cwd=host_cwd,
                    timeout=timeout,
                    worker_instance_id=lease.worker_instance_id,
                    env=dict(lease.worker_env),
                )
            except asyncio.CancelledError:
                raise
            except (ContainerRuntimeError, Exception):
                return VerifierExecutionResult(
                    status=VerificationFailure.EXECUTION_ERROR.value,
                    diagnostics="container verifier execution failed",
                )
            return self._normalize_runtime_result(registration, result)
        finally:
            await lease.release()

    @staticmethod
    def _normalize_runtime_result(
        registration: ResolvedPocRegistration,
        result: Any,
    ) -> VerifierExecutionResult:
        if not isinstance(result, CliResult):
            return VerifierExecutionResult(
                status=VerificationFailure.PROVENANCE_UNAVAILABLE.value,
                diagnostics="container verifier returned no normalized provenance",
            )
        runtime = result.runtime_status
        if not isinstance(runtime, Mapping):
            return VerifierExecutionResult(
                status=VerificationFailure.PROVENANCE_UNAVAILABLE.value,
                diagnostics="container verifier provenance unavailable",
            )
        stdout = str(getattr(result, "text", "") or "")
        stderr = str(getattr(result, "raw_stderr", "") or "")
        elapsed_ms = _elapsed_ms(result)
        if bool(result.timed_out) or bool(runtime.get("timed_out")):
            return VerifierExecutionResult(
                status=VerificationFailure.TIMED_OUT.value,
                exit_code=_safe_exit_code(runtime.get("rc")),
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        if bool(result.cancelled) or bool(runtime.get("cancelled")):
            return VerifierExecutionResult(
                status=VerificationFailure.CANCELLED.value,
                exit_code=_safe_exit_code(runtime.get("rc")),
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        # A success claim is fail-closed: only the executor's known clean
        # terminal state can carry stdout/stderr provenance into verification.
        if "status" not in runtime:
            return VerifierExecutionResult(
                status=VerificationFailure.PROVENANCE_UNAVAILABLE.value,
                elapsed_ms=elapsed_ms,
            )
        if runtime.get("status") != "finished":
            return VerifierExecutionResult(
                status=VerificationFailure.EXECUTION_ERROR.value,
                diagnostics="container verifier did not reach a clean terminal state",
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        if bool(runtime.get("oom_killed")) or bool(runtime.get("steered")):
            return VerifierExecutionResult(
                status=VerificationFailure.EXECUTION_ERROR.value,
                diagnostics="container verifier runtime provenance incomplete",
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        rc = runtime.get("rc")
        if isinstance(rc, bool) or not isinstance(rc, int):
            return VerifierExecutionResult(
                status=VerificationFailure.PROVENANCE_UNAVAILABLE.value,
                elapsed_ms=elapsed_ms,
            )
        if rc != 0:
            return VerifierExecutionResult(
                status=VerificationFailure.NONZERO_EXIT.value,
                exit_code=rc,
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        if registration.indicator in stdout:
            location = "stdout"
        elif registration.indicator in stderr:
            location = "stderr"
        else:
            return VerifierExecutionResult(
                status=VerificationFailure.INDICATOR_NOT_OBSERVED.value,
                exit_code=rc,
                elapsed_ms=elapsed_ms,
                stdout=stdout,
                stderr=stderr,
            )
        return VerifierExecutionResult(
            status="verified",
            exit_code=rc,
            observed_location=location,
            elapsed_ms=elapsed_ms,
            stdout=stdout,
            stderr=stderr,
        )


def _normalize_artifact_name(value: Any) -> str:
    name = str(value or "").strip()
    candidate = Path(name)
    if (
        not name
        or len(name) > 255
        or candidate.name != name
        or name in {".", ".."}
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("invalid graph artifact name")
    return name


def _resolve_graph_artifact_path(
    *,
    workspace: Path,
    raw_path: Any,
    artifact_id: str,
) -> Path:
    raw = str(raw_path or "").strip()
    relative = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or _WINDOWS_ABSOLUTE_PATH.match(raw) is not None
        or Path(raw).is_absolute()
        or relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < 5
        or relative.parts[:2] != ("shared", "objects")
        or relative.name != artifact_id
    ):
        raise ValueError("invalid graph artifact path")
    candidate = workspace.joinpath(*relative.parts)
    try:
        candidate.relative_to(workspace / "shared" / "objects")
    except ValueError as exc:
        raise ValueError("invalid graph artifact path") from exc
    return candidate


def _matches_current_container_lease(executor: Any, lease: WorkerRuntimeLease) -> bool:
    """Require the concrete RCP container executor for this exact pool generation."""
    if not isinstance(executor, ContainerRuntimeExecutor):
        return False
    identity = executor.identity
    return (
        executor.is_docker_runtime is True
        and identity.pool_id == lease.pool_id
        and identity.pool_instance_id == lease.pool_instance_id
        and identity.generation == lease.generation
    )


def _stage_artifact(registration: ResolvedPocRegistration) -> Path:
    """Stage only a regular shared-CAS file in a non-replaceable source directory."""
    shared_root = (registration.workspace_root / "shared").resolve(strict=True)
    source_lexical = registration.artifact_path
    if source_lexical.is_symlink():
        raise ValueError("PoC artifact must not be a symlink")
    try:
        source = source_lexical.resolve(strict=True)
        source.relative_to(shared_root / "objects")
    except (OSError, ValueError) as exc:
        raise ValueError("PoC artifact escapes shared CAS") from exc
    # Refuse a symlink in any CAS path component as well as at the leaf.  A
    # graph path can be syntactically valid yet resolve outside the CAS through
    # an intermediate symlink.
    current = registration.workspace_root
    for part in source_lexical.relative_to(registration.workspace_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("PoC artifact must not traverse symlinks")
    if not source.is_file():
        raise ValueError("PoC artifact must be a regular file")

    verifier_root = registration.workspace_root / "verifiers"
    verifier_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(registration.reproduction_id.encode("utf-8")).hexdigest()[:24]
    stage = Path(tempfile.mkdtemp(prefix=f"poc-{digest}-", dir=verifier_root))
    destination = stage / registration.artifact_name
    try:
        shutil.copy2(source, destination, follow_symlinks=False)
        _make_read_only(destination)
        # The command's CWD remains compatible with existing POC_SAVE command
        # names, but its parent is no longer writable, so it cannot replace the
        # staged source.  A dedicated output directory is the only writable
        # verifier-created-artifact location.
        output = stage / "output"
        output.mkdir()
        output.chmod(0o700)
        stage.chmod(0o555)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def _make_read_only(path: Path) -> None:
    targets = [path]
    if path.is_dir():
        targets.extend(sorted(path.rglob("*"), reverse=True))
    for item in targets:
        try:
            mode = item.stat().st_mode
            item.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        except OSError:
            raise


def _safe_exit_code(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _elapsed_ms(result: Any) -> int | None:
    value = getattr(result, "elapsed_s", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value * 1000)
    return None


__all__ = [
    "ContainerPocVerifier",
    "ResolvedPocRegistration",
    "VerifierExecutionResult",
]
