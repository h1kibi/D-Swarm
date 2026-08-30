"""Generation-scoped Docker runtime for frozen M9 runtime pools.

This module deliberately does not import the legacy ``container_exec`` facade.  One
executor owns exactly one immutable pool generation, one container, and one RCP-v2
link.  Docker or control-plane failures fail closed; there is no host-local fallback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any, Callable, Mapping, Optional, Protocol
import uuid

from dswarm.solver.cli_driver import CliResult, StreamStep
from dswarm.solver.control_client import run_cli_rcp, run_cli_streaming_rcp
from dswarm.solver.control_receiver import ExpectedRuntimeIdentity, _SupervisorLink
from dswarm.solver.docker import docker_run
from dswarm.solver.runtime_policy import PoolSpec

CONTAINER_WORKSPACE = "/home/kali/workspace"
_MANAGED_LABEL = "com.dswarm.managed"
_LABEL_PREFIX = "com.dswarm."
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_CONTROL_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SAFE_STATUS = frozenset({"starting", "running", "finished", "timeout", "oom", "cancelled", "steered", "failed"})

def _control_host_from_environment() -> str:
    """Return the validated control-plane host used by worker supervisors."""
    raw = os.environ.get("DSWARM_CONTROL_HOST")
    host = "host.docker.internal" if raw is None else raw.strip()
    if not host or not _SAFE_CONTROL_HOST_RE.fullmatch(host):
        raise ContainerRuntimeError("invalid_control_host")
    return host


class ContainerRuntimeError(RuntimeError):
    """A sanitized, machine-readable runtime failure."""

    def __init__(self, code: str):
        self.code = _safe_code(code, "runtime_error")
        super().__init__(self.code)


@dataclass(frozen=True)
class ContainerGenerationIdentity:
    run_id: str
    pool_id: str
    pool_instance_id: str
    generation: int
    resolved_image_id: str


@dataclass(frozen=True)
class ContainerMount:
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class ContainerCreateRequest:
    name: str
    image: str
    labels: Mapping[str, str]
    mounts: tuple[ContainerMount, ...]
    env: Mapping[str, str]
    network: str
    cpus: str
    memory: str
    pids_limit: int
    tmpfs_bytes: int
    uid: int
    gid: int
    user: str
    command: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        """Return secret-free launch metadata suitable for diagnostics."""
        return {
            "image": self.image,
            "labels": dict(self.labels),
            "mounts": [
                {"target": mount.target, "read_only": mount.read_only}
                for mount in self.mounts
            ],
            "network": self.network,
            "cpus": self.cpus,
            "memory": self.memory,
            "pids_limit": self.pids_limit,
            "tmpfs_bytes": self.tmpfs_bytes,
            "uid": self.uid,
            "gid": self.gid,
            "user": self.user,
            "command": list(self.command),
        }


@dataclass(frozen=True)
class ContainerInspection:
    container_id: str
    image_id: str
    labels: Mapping[str, str]
    mounts: tuple[ContainerMount, ...]
    network: str
    uid: int
    gid: int
    running: bool


@dataclass(frozen=True)
class RuntimeTerminationReport:
    pool_instance_id: str
    link_drained: bool
    token_revoked: bool
    container_removed: bool
    proof_complete: bool

    def snapshot(self) -> dict[str, Any]:
        return {
            "pool_instance_id": self.pool_instance_id,
            "link_drained": self.link_drained,
            "token_revoked": self.token_revoked,
            "container_removed": self.container_removed,
            "proof_complete": self.proof_complete,
        }


@dataclass(frozen=True)
class RuntimeExecRecord:
    exec_id: str
    identity: ContainerGenerationIdentity
    worker_instance_id: str
    profile_id: str
    runtime_kind: str
    driver: str
    operation_kind: str
    argv0: str
    status: str
    started_at: float
    finished_at: float
    elapsed_s: float
    rc: int | None = None
    timed_out: bool = False
    oom_killed: bool = False
    cancelled: bool = False
    steered: bool = False
    failure_code: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend": "container_rcp_v2",
            "exec_id": self.exec_id,
            "run_id": self.identity.run_id,
            "pool_id": self.identity.pool_id,
            "pool_instance_id": self.identity.pool_instance_id,
            "generation": self.identity.generation,
            "worker_instance_id": _safe_token(self.worker_instance_id, "unknown-worker"),
            "profile_id": _safe_token(self.profile_id, "unknown-profile"),
            "runtime_kind": _safe_token(self.runtime_kind, "unknown-runtime"),
            "driver": _safe_token(self.driver, "unknown-driver"),
            "operation_kind": _safe_token(self.operation_kind, "unknown-operation"),
            "image_id_short": _short_image_id(self.identity.resolved_image_id),
            "argv0": _safe_token(self.argv0, "unknown-command"),
            "status": self.status if self.status in _SAFE_STATUS else "failed",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": self.elapsed_s,
            "rc": self.rc,
            "timed_out": self.timed_out,
            "oom_killed": self.oom_killed,
            "cancelled": self.cancelled,
            "steered": self.steered,
            "failure_code": _safe_code(self.failure_code, "") if self.failure_code else "",
        }


class DockerRuntimeAdapter(Protocol):
    def create(self, request: ContainerCreateRequest) -> str: ...

    def inspect(self, container_id: str) -> ContainerInspection: ...

    def list(self, *, container_id: str) -> tuple[str, ...]: ...

    def remove(self, container_id: str, *, force: bool) -> bool: ...


class DockerCliRuntimeAdapter:
    """Minimal Docker CLI adapter; it never resolves or pulls an image tag."""

    def create(self, request: ContainerCreateRequest) -> str:
        args: list[str] = [
            "run",
            "-d",
            "--name",
            request.name,
            "--network",
            request.network,
            "--cpus",
            request.cpus,
            "--memory",
            request.memory,
            "--pids-limit",
            str(request.pids_limit),
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={request.tmpfs_bytes}",
            "--user",
            request.user,
        ]
        if request.network != "none":
            args.extend(("--add-host", "host.docker.internal:host-gateway"))
        for key, value in sorted(request.labels.items()):
            args.extend(("--label", f"{key}={value}"))
        for mount in request.mounts:
            mode = ",readonly" if mount.read_only else ""
            args.extend(
                (
                    "--mount",
                    f"type=bind,source={mount.source},target={mount.target}{mode}",
                )
            )
        for key, value in sorted(request.env.items()):
            args.extend(("-e", f"{key}={value}"))
        args.append(request.image)
        args.extend(request.command)
        result = docker_run(*args, timeout=60.0)
        if result.returncode != 0:
            # docker create failures were previously undiagnosable: the code
            # alone said nothing about WHICH argument docker rejected.
            import logging

            logging.getLogger(__name__).warning(
                "docker create failed rc=%s stderr=%s stdout=%s argv_tail=%s",
                result.returncode,
                (result.stderr or "")[-400:],
                (result.stdout or "")[-200:],
                args[-12:],
            )
            raise ContainerRuntimeError("container_create_failed")
        container_id = result.stdout.strip()
        if not container_id:
            raise ContainerRuntimeError("container_create_failed")
        return container_id

    def inspect(self, container_id: str) -> ContainerInspection:
        result = docker_run("inspect", container_id, timeout=20.0)
        if result.returncode != 0:
            raise ContainerRuntimeError("container_inspect_failed")
        try:
            payload = json.loads(result.stdout)
            item = payload[0]
            mounts = tuple(
                ContainerMount(
                    source=str(mount["Source"]),
                    target=str(mount["Destination"]),
                    read_only=not bool(mount.get("RW", False)),
                )
                for mount in item.get("Mounts", [])
                if mount.get("Type") == "bind"
            )
            network = str(item.get("HostConfig", {}).get("NetworkMode", ""))
            running = bool(item.get("State", {}).get("Running", False))
            labels = dict(item.get("Config", {}).get("Labels") or {})
            image_id = str(item.get("Image", ""))
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContainerRuntimeError("container_inspect_failed") from exc
        # A stopped stale generation must remain inspectable for proof-first
        # cleanup.  Live startup still probes the in-image kali identity.
        if running:
            uid = self._container_identity(container_id, "-u")
            gid = self._container_identity(container_id, "-g")
        else:
            uid = 0
            gid = 0
        return ContainerInspection(
            container_id=container_id,
            image_id=image_id,
            labels=labels,
            mounts=mounts,
            network=network,
            uid=uid,
            gid=gid,
            running=running,
        )

    @staticmethod
    def _container_identity(container_id: str, flag: str) -> int:
        # Probe the container's EFFECTIVE identity (docker exec runs as the
        # --user the pool created the container with). Asking for a named user
        # ("kali") broke when the M9a image switched its worker user to "ctf":
        # numeric proof works for any image user.
        result = docker_run("exec", container_id, "id", flag, timeout=10.0)
        if result.returncode != 0:
            raise ContainerRuntimeError("container_identity_probe_failed")
        try:
            value = int(result.stdout.strip())
        except ValueError as exc:
            raise ContainerRuntimeError("container_identity_probe_failed") from exc
        if value <= 0:
            raise ContainerRuntimeError("container_identity_probe_failed")
        return value

    def list(self, *, container_id: str) -> tuple[str, ...]:
        result = docker_run(
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
            "--format",
            "{{.ID}}",
            timeout=20.0,
        )
        if result.returncode != 0:
            raise ContainerRuntimeError("container_list_failed")
        return tuple(
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() == container_id
        )

    def remove(self, container_id: str, *, force: bool) -> bool:
        args = ["rm"]
        if force:
            args.append("-f")
        args.append(container_id)
        try:
            return docker_run(*args, timeout=30.0).returncode == 0
        except Exception:
            return False


class _RegisteredCommandDriver:
    """Minimal raw-output parser for the adapter-owned fixed shell wrapper."""

    name = "poc-verifier"

    @staticmethod
    def parse(stdout: str, stderr: str) -> CliResult:
        return CliResult(text=stdout, raw_stderr=stderr)


class ContainerRuntimeExecutor:
    """Own one frozen pool generation and its exact RCP-v2 control link."""

    # Explicit marker consumed by the Verified-PoC adapter; host executors do not
    # implement this marker and therefore cannot enter the production verifier path.
    is_docker_runtime = True

    def __init__(
        self,
        *,
        identity: ContainerGenerationIdentity,
        pool_spec: PoolSpec,
        run_root: Path,
        docker: DockerRuntimeAdapter,
        receiver: Any,
        container_id: str,
        control_link: _SupervisorLink,
        mounts: tuple[ContainerMount, ...],
        run_rcp_impl: Callable[..., CliResult],
        run_streaming_rcp_impl: Callable[..., CliResult],
        worker_token_revoker: Any | None = None,
    ) -> None:
        self.identity = identity
        self.pool_spec = pool_spec
        self.run_root = run_root
        self.docker = docker
        self.receiver = receiver
        self.container_id = container_id
        self.control_link = control_link
        self.mounts = mounts
        self._run_rcp = run_rcp_impl
        self._run_streaming_rcp = run_streaming_rcp_impl
        self._worker_token_revoker = worker_token_revoker
        self._worker_token_ids: set[str] = set()
        self._terminated = False

    @property
    def run_id(self) -> str:
        return self.identity.run_id

    @property
    def pool_id(self) -> str:
        return self.identity.pool_id

    @property
    def pool_instance_id(self) -> str:
        return self.identity.pool_instance_id

    @property
    def generation(self) -> int:
        return self.identity.generation

    def register_worker_token(self, token: str) -> None:
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in token)
        ):
            raise ContainerRuntimeError("invalid_worker_token")
        self._worker_token_ids.add(token)

    def unregister_worker_token(self, token: str) -> None:
        self._worker_token_ids.discard(token)

    @classmethod
    async def create(
        cls,
        *,
        run_id: str,
        pool_spec: PoolSpec,
        generation: int,
        run_root: str | Path,
        docker: DockerRuntimeAdapter | None = None,
        receiver: Any,
        run_rcp: Callable[..., CliResult] | None = None,
        run_streaming_rcp: Callable[..., CliResult] | None = None,
        worker_token_revoker: Any | None = None,
        startup_timeout: float = 40.0,
    ) -> "ContainerRuntimeExecutor":
        if not isinstance(pool_spec, PoolSpec):
            raise ContainerRuntimeError("invalid_pool_spec")
        if not _safe_identity(run_id):
            raise ContainerRuntimeError("invalid_run_id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ContainerRuntimeError("invalid_generation")
        if not isinstance(startup_timeout, (int, float)) or isinstance(startup_timeout, bool) or startup_timeout <= 0:
            raise ContainerRuntimeError("invalid_startup_timeout")

        control_host = _control_host_from_environment()
        pool_instance_id = str(uuid.uuid4())
        identity = ContainerGenerationIdentity(
            run_id=run_id,
            pool_id=pool_spec.pool_id,
            pool_instance_id=pool_instance_id,
            generation=generation,
            resolved_image_id=pool_spec.resolved_image_id,
        )
        expected = ExpectedRuntimeIdentity(
            run_id=run_id,
            pool_id=pool_spec.pool_id,
            pool_instance_id=pool_instance_id,
            generation=generation,
            expected_image_id=pool_spec.resolved_image_id,
            protocol_version=pool_spec.protocol_version,
        )
        adapter = docker or DockerCliRuntimeAdapter()
        root = Path(run_root).resolve(strict=False)
        workspace = (root / "workspace").resolve(strict=False)
        workspace.mkdir(parents=True, exist_ok=True)
        mounts = (
            ContainerMount(
                source=str(workspace),
                target=CONTAINER_WORKSPACE,
                read_only=False,
            ),
        )
        labels = {
            _MANAGED_LABEL: "true",
            f"{_LABEL_PREFIX}run_id": run_id,
            f"{_LABEL_PREFIX}pool_id": pool_spec.pool_id,
            f"{_LABEL_PREFIX}pool_instance_id": pool_instance_id,
            f"{_LABEL_PREFIX}generation": str(generation),
        }
        token = ""
        container_id = ""
        try:
            token = await asyncio.to_thread(receiver.issue_pool, expected)
            port = getattr(receiver, "port", 9100)
            if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
                port = 9100
            request = ContainerCreateRequest(
                name=_container_name(run_id, pool_spec.pool_id, pool_instance_id),
                image=pool_spec.resolved_image_id,
                labels=labels,
                mounts=mounts,
                env={
                    "DSWARM_RUN_ID": run_id,
                    "DSWARM_POOL_ID": pool_spec.pool_id,
                    "DSWARM_POOL_INSTANCE_ID": pool_instance_id,
                    "DSWARM_POOL_GENERATION": str(generation),
                    "DSWARM_CONTROL_TOKEN": token,
                },
                network=_network_name(pool_spec),
                cpus=pool_spec.resources.cpus,
                memory=pool_spec.resources.memory,
                pids_limit=pool_spec.resources.pids_limit,
                tmpfs_bytes=pool_spec.resources.tmpfs_bytes,
                uid=pool_spec.uid,
                gid=pool_spec.gid,
                # Run as the exact worker identity the snapshot preflight proved
                # (PoolSpec.uid/gid). Hardcoded "0:0" (root) contradicted the
                # identity contract: post-create inspection proves uid/gid, and
                # a root worker is exactly what the M9a model forbids.
                user=f"{pool_spec.uid}:{pool_spec.gid}",
                command=(
                    "--connect",
                    f"{control_host}:{port}",
                    "--workspace",
                    CONTAINER_WORKSPACE,
                ),
            )
            container_id = await asyncio.to_thread(adapter.create, request)
            inspection = await asyncio.to_thread(adapter.inspect, container_id)
            _validate_inspection(request, inspection, container_id)
            link = await asyncio.to_thread(
                receiver.wait_pool, pool_instance_id, float(startup_timeout)
            )
            _validate_link(link, expected)
            health = await asyncio.to_thread(link.health, timeout=min(5.0, float(startup_timeout)))
            if not isinstance(health, Mapping) or not health.get("ok"):
                raise ContainerRuntimeError("runtime_hello_failed")
            return cls(
                identity=identity,
                pool_spec=pool_spec,
                run_root=root,
                docker=adapter,
                receiver=receiver,
                container_id=container_id,
                control_link=link,
                mounts=mounts,
                run_rcp_impl=run_rcp or run_cli_rcp,
                run_streaming_rcp_impl=run_streaming_rcp or run_cli_streaming_rcp,
                worker_token_revoker=worker_token_revoker,
            )
        except ContainerRuntimeError as exc:
            await _log_pool_container_death(adapter, container_id)
            await _cleanup_startup(adapter, receiver, container_id, pool_instance_id)
            raise ContainerRuntimeError(exc.code) from None
        except Exception:
            await _log_pool_container_death(adapter, container_id)
            await _cleanup_startup(adapter, receiver, container_id, pool_instance_id)
            code = "runtime_hello_failed" if container_id else "runtime_start_failed"
            raise ContainerRuntimeError(code) from None

    def to_container_path(self, host_path: str | Path) -> str:
        candidate = Path(host_path).resolve(strict=False)
        ordered = sorted(self.mounts, key=lambda mount: len(Path(mount.source).parts), reverse=True)
        for mount in ordered:
            source = Path(mount.source).resolve(strict=False)
            try:
                relative = candidate.relative_to(source)
            except ValueError:
                continue
            target = PurePosixPath(mount.target)
            if not relative.parts:
                return str(target)
            return str(target.joinpath(*relative.parts))
        raise ContainerRuntimeError("host_path_not_mounted")

    async def run(
        self,
        driver: Any,
        argv: list[str],
        *,
        host_cwd: str | Path,
        timeout: int,
        env: Optional[dict] = None,
        worker_instance_id: str,
        operation_kind: str,
    ) -> CliResult:
        return await self._execute(
            streaming=False,
            driver=driver,
            argv=argv,
            host_cwd=host_cwd,
            timeout=timeout,
            env=env,
            worker_instance_id=worker_instance_id,
            operation_kind=operation_kind,
        )

    async def run_registered_command(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        host_cwd: str | Path,
        timeout: float,
        worker_instance_id: str,
        env: Optional[dict] = None,
    ) -> CliResult:
        """Execute a pre-resolved argv through this Docker pool generation."""
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ContainerRuntimeError("invalid_registered_command")
        normalized = list(argv)
        if any(not isinstance(item, str) or not item for item in normalized):
            raise ContainerRuntimeError("invalid_registered_command")
        return await self.run(
            _RegisteredCommandDriver(),
            normalized,
            host_cwd=host_cwd,
            timeout=timeout,
            env=env,
            worker_instance_id=worker_instance_id,
            operation_kind="poc_verifier",
        )

    async def run_streaming(
        self,
        driver: Any,
        argv: list[str],
        *,
        host_cwd: str | Path,
        timeout: int,
        on_step: Callable[[StreamStep], None],
        env: Optional[dict] = None,
        cancel_event: Any = None,
        on_proc: Optional[Callable[[object], None]] = None,
        steer_event: Any = None,
        worker_instance_id: str,
        operation_kind: str,
    ) -> CliResult:
        return await self._execute(
            streaming=True,
            driver=driver,
            argv=argv,
            host_cwd=host_cwd,
            timeout=timeout,
            env=env,
            worker_instance_id=worker_instance_id,
            operation_kind=operation_kind,
            on_step=on_step,
            cancel_event=cancel_event,
            on_proc=on_proc,
            steer_event=steer_event,
        )

    async def _execute(
        self,
        *,
        streaming: bool,
        driver: Any,
        argv: list[str],
        host_cwd: str | Path,
        timeout: int,
        env: Optional[dict],
        worker_instance_id: str,
        operation_kind: str,
        on_step: Optional[Callable[[StreamStep], None]] = None,
        cancel_event: Any = None,
        on_proc: Optional[Callable[[object], None]] = None,
        steer_event: Any = None,
    ) -> CliResult:
        if self._terminated:
            raise ContainerRuntimeError("runtime_terminated")
        if not getattr(self.control_link, "alive", False):
            raise ContainerRuntimeError("runtime_link_unavailable")
        if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
            raise ContainerRuntimeError("invalid_argv")
        container_argv = list(argv)
        driver_name = str(getattr(driver, "name", ""))
        container_argv[0] = "pi" if driver_name == "pi" else ntpath.basename(argv[0].replace("/", "\\"))
        if not container_argv[0]:
            raise ContainerRuntimeError("invalid_argv")
        container_cwd = self.to_container_path(host_cwd)
        started_at = time.time()
        kwargs: dict[str, Any] = {
            "run_id": self.run_id,
            "container_cwd": container_cwd,
            "timeout": timeout,
            "env": env,
            "link": self.control_link,
        }
        call = self._run_rcp
        if streaming:
            call = self._run_streaming_rcp
            kwargs.update(
                on_step=on_step,
                cancel_event=cancel_event,
                on_proc=on_proc,
                steer_event=steer_event,
            )
        try:
            result = await asyncio.to_thread(call, driver, container_argv, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ContainerRuntimeError("runtime_exec_failed") from None
        finished_at = time.time()
        raw = result.runtime_status if isinstance(result.runtime_status, Mapping) else {}
        elapsed = _finite_nonnegative(raw.get("elapsed_s"), result.elapsed_s)
        status = _terminal_status(raw, result)
        rc = raw.get("rc")
        if isinstance(rc, bool) or not isinstance(rc, int):
            rc = None
        record = RuntimeExecRecord(
            exec_id=str(uuid.uuid4()),
            identity=self.identity,
            worker_instance_id=worker_instance_id,
            profile_id=self.pool_spec.profile_id,
            runtime_kind=self.pool_spec.runtime_kind,
            driver=driver_name,
            operation_kind=operation_kind,
            argv0=container_argv[0],
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_s=elapsed,
            rc=rc,
            timed_out=bool(raw.get("timed_out", result.timed_out)),
            oom_killed=bool(raw.get("oom_killed", result.oom_killed)),
            cancelled=bool(raw.get("cancelled", result.cancelled)),
            steered=bool(raw.get("steered", result.steered)),
            failure_code=_safe_code(raw.get("failure_code"), ""),
        )
        result.runtime_status = record.snapshot()
        return result

    async def signal(self, worker_id: str, signal_name: str) -> bool:
        if self._terminated or not getattr(self.control_link, "alive", False):
            raise ContainerRuntimeError("runtime_link_unavailable")
        try:
            return await asyncio.to_thread(
                self.control_link.signal, worker_id, signal_name, timeout=10.0
            )
        except Exception:
            raise ContainerRuntimeError("runtime_signal_failed") from None

    async def status(self, worker_id: str) -> dict[str, Any]:
        if self._terminated or not getattr(self.control_link, "alive", False):
            raise ContainerRuntimeError("runtime_link_unavailable")
        try:
            return await asyncio.to_thread(
                self.control_link.status, worker_id, timeout=10.0
            )
        except Exception:
            raise ContainerRuntimeError("runtime_status_failed") from None

    async def terminate(self, *, require_proof: bool = False) -> RuntimeTerminationReport:
        if self._terminated:
            return RuntimeTerminationReport(
                pool_instance_id=self.pool_instance_id,
                link_drained=True,
                token_revoked=True,
                container_removed=True,
                proof_complete=True,
            )
        link_drained = False
        try:
            response = await asyncio.to_thread(self.control_link.teardown, timeout=20.0)
            link_drained = bool(
                isinstance(response, Mapping)
                and response.get("ok")
                and response.get("remaining", 0) == 0
            )
        except Exception:
            link_drained = False

        # Lazy import avoids a cycle with the shared inspection dataclasses.
        from dswarm.solver.runtime_cleanup import (
            RuntimeCleanupExpectation,
            cleanup_pool_generation,
        )

        expected = RuntimeCleanupExpectation(
            container_id=self.container_id,
            run_id=self.run_id,
            pool_id=self.pool_id,
            pool_instance_id=self.pool_instance_id,
            generation=self.generation,
            image_id=self.identity.resolved_image_id,
            network=_network_name(self.pool_spec),
            mounts=self.mounts,
            private_state_mounts=self.mounts,
            worker_token_ids=tuple(sorted(self._worker_token_ids)),
        )
        cleanup = await asyncio.to_thread(
            cleanup_pool_generation,
            docker=self.docker,
            expected=expected,
            receiver=self.receiver,
            worker_token_revoker=self._worker_token_revoker,
            link_drained=link_drained,
        )
        proof_complete = cleanup.proven
        self._terminated = proof_complete
        if proof_complete:
            self._worker_token_ids.clear()
        report = RuntimeTerminationReport(
            pool_instance_id=self.pool_instance_id,
            link_drained=link_drained,
            token_revoked=cleanup.pool_token_revoked,
            container_removed=cleanup.removed,
            proof_complete=proof_complete,
        )
        if require_proof and not proof_complete:
            raise ContainerRuntimeError("cleanup_unproven")
        return report


def _safe_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _safe_token(value: object, default: str) -> str:
    text = str(value or "")
    if _SAFE_TOKEN_RE.fullmatch(text):
        return text
    return default


def _safe_code(value: object, default: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text):
        return text
    return default


def _short_image_id(value: str) -> str:
    if value.startswith("sha256:"):
        return value[:19]
    return _safe_token(value[:32], "unknown-image")


def _container_name(run_id: str, pool_id: str, pool_instance_id: str) -> str:
    def part(value: str, limit: int) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-._")
        return (normalized or "runtime")[:limit]

    return f"dswarm-{part(run_id, 28)}-{part(pool_id.split('::')[-1], 12)}-{pool_instance_id[:8]}"


def _network_name(pool_spec: PoolSpec) -> str:
    if pool_spec.network.kind == "named":
        return pool_spec.network.name
    return pool_spec.network.kind


def _validate_inspection(
    request: ContainerCreateRequest,
    inspection: ContainerInspection,
    container_id: str,
) -> None:
    expected_mounts = tuple(
        sorted(request.mounts, key=lambda mount: (mount.target, mount.source, mount.read_only))
    )
    actual_mounts = tuple(
        sorted(inspection.mounts, key=lambda mount: (mount.target, mount.source, mount.read_only))
    )
    actual_labels = dict(inspection.labels)
    managed_labels_match = all(
        actual_labels.get(key) == value for key, value in request.labels.items()
    ) and not any(
        key.startswith(_LABEL_PREFIX) and key not in request.labels
        for key in actual_labels
    )
    if (
        inspection.container_id != container_id
        or inspection.image_id != request.image
        or not managed_labels_match
        or actual_mounts != expected_mounts
        or inspection.network != request.network
        or inspection.uid != request.uid
        or inspection.gid != request.gid
        or not inspection.running
    ):
        raise ContainerRuntimeError("runtime_identity_mismatch")


def _validate_link(link: Any, expected: ExpectedRuntimeIdentity) -> None:
    if link is None or not getattr(link, "alive", False):
        raise ContainerRuntimeError("runtime_hello_failed")
    checks = {
        "run_id": expected.run_id,
        "pool_id": expected.pool_id,
        "pool_instance_id": expected.pool_instance_id,
        "generation": expected.generation,
        "protocol_version": expected.protocol_version,
    }
    for name, expected_value in checks.items():
        if getattr(link, name, None) != expected_value:
            raise ContainerRuntimeError("runtime_hello_failed")


async def _log_pool_container_death(
    docker: DockerRuntimeAdapter, container_id: str,
) -> None:
    """Best-effort: a pool container that died during startup loses its agent
    logs when _cleanup_startup removes it. Persist the terminal state + last
    log lines to the backend log so hello/identity failures carry evidence
    (run-4408-class failures were undiagnosable without this)."""
    if not container_id:
        return
    try:
        import logging
        import subprocess

        state = await asyncio.to_thread(
            lambda: subprocess.run(
                ["docker", "inspect", "--format",
                 "{{.State.Status}} exit={{.State.ExitCode}} err={{.State.Error}}",
                 container_id], capture_output=True, text=True, timeout=10))
        logs = await asyncio.to_thread(
            lambda: subprocess.run(
                ["docker", "logs", "--tail", "40", container_id],
                capture_output=True, text=True, timeout=10))
        logging.getLogger(__name__).warning(
            "pool container %s startup failure: state=%s agent_logs=%s",
            container_id[:12], state.stdout.strip() or state.stderr.strip(),
            (logs.stdout + logs.stderr)[-1500:])
    except Exception:
        pass


async def _cleanup_startup(
    docker: DockerRuntimeAdapter,
    receiver: Any,
    container_id: str,
    pool_instance_id: str,
) -> None:
    if container_id:
        try:
            await asyncio.to_thread(docker.remove, container_id, force=True)
        except Exception:
            pass
    try:
        await asyncio.to_thread(receiver.revoke_pool_instance, pool_instance_id)
    except Exception:
        pass


def _finite_nonnegative(value: object, fallback: object) -> float:
    for candidate in (value, fallback, 0.0):
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            continue
        number = float(candidate)
        if math.isfinite(number) and number >= 0:
            return number
    return 0.0


def _terminal_status(raw: Mapping[str, Any], result: CliResult) -> str:
    status = raw.get("status")
    if isinstance(status, str) and status in _SAFE_STATUS:
        return status
    if result.oom_killed:
        return "oom"
    if result.timed_out:
        return "timeout"
    if result.cancelled:
        return "cancelled"
    if result.steered:
        return "steered"
    return "finished"


__all__ = [
    "CONTAINER_WORKSPACE",
    "ContainerCreateRequest",
    "ContainerGenerationIdentity",
    "ContainerInspection",
    "ContainerMount",
    "ContainerRuntimeError",
    "ContainerRuntimeExecutor",
    "DockerCliRuntimeAdapter",
    "DockerRuntimeAdapter",
    "RuntimeExecRecord",
    "RuntimeTerminationReport",
]
