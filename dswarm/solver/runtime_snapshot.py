"""Frozen Worker image resolution and numeric identity preflight."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from dswarm.solver.docker import docker_run
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimePolicy,
    RuntimePolicyError,
    RuntimeResourceSpec,
    RuntimeSnapshot,
)
from dswarm.solver.worker_profiles import normalize_runtime_profile


_IMAGE_ID_RE = re.compile(r"^sha256:[0-9A-Za-z._-]+$")


class RuntimeSnapshotBuildError(RuntimeError):
    """A structured, operator-safe runtime snapshot construction failure."""

    def __init__(self, code: str, safe_detail: str) -> None:
        self.code = code
        self.safe_detail = safe_detail
        super().__init__(f"{code}: {safe_detail}")


@dataclass(frozen=True)
class ResolvedWorkerImage:
    requested_ref: str
    image_id: str
    uid: int
    gid: int


class DockerImageBackend(Protocol):
    def resolve_image(self, ref: str) -> Mapping[str, Any] | None: ...

    def pull_image(self, ref: str) -> bool: ...

    def query_user(
        self,
        image_id: str,
        user: str,
        *,
        network: str,
        mounts: tuple[()],
        env: Mapping[str, str],
    ) -> tuple[int, int] | None: ...


class DockerCliImageBackend:
    """Minimal Docker CLI adapter used only during snapshot preflight."""

    def resolve_image(self, ref: str) -> Mapping[str, Any] | None:
        result = docker_run("image", "inspect", ref, timeout=20.0)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        image_id = payload[0].get("Id")
        return {"image_id": image_id} if isinstance(image_id, str) else None

    def pull_image(self, ref: str) -> bool:
        result = docker_run("pull", ref, timeout=300.0)
        return result.returncode == 0

    def query_user(
        self,
        image_id: str,
        user: str,
        *,
        network: str,
        mounts: tuple[()],
        env: Mapping[str, str],
    ) -> tuple[int, int] | None:
        if network != "none" or mounts or env:
            raise ValueError("identity probe isolation contract violated")
        result = docker_run(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            image_id,
            "-lc",
            f"id -u {user} && id -g {user}",
            timeout=30.0,
        )
        if result.returncode != 0:
            return None
        values: list[int] = []
        for line in (result.stdout or "").splitlines():
            try:
                values.append(int(line.strip()))
            except ValueError:
                continue
        if len(values) != 2:
            return None
        return values[0], values[1]


class DockerImageInspector:
    """Resolve each requested tag once and prove the image's worker identity.

    The worker user is an image property, not a constant: the current
    docker/worker-kali image creates ``ctf`` (uid 1000), while pre-M9a images
    created ``kali``. Candidates are probed in order and the first that proves
    wins; the numeric identity must then be consistent across the run's pools
    (validated separately).
    """

    WORKER_USER_CANDIDATES = ("ctf", "kali")

    def __init__(
        self,
        backend: DockerImageBackend | None = None,
        *,
        allow_pull: bool = True,
    ) -> None:
        self._backend = backend or DockerCliImageBackend()
        self._allow_pull = bool(allow_pull)
        self._cache: dict[str, ResolvedWorkerImage] = {}

    def resolve(self, image_ref: str) -> ResolvedWorkerImage:
        if not isinstance(image_ref, str) or not image_ref.strip():
            raise RuntimeSnapshotBuildError(
                "image_resolution_failed", "worker image is unavailable"
            )
        normalized_ref = image_ref.strip()
        cached = self._cache.get(normalized_ref)
        if cached is not None:
            return cached

        try:
            resolved = self._backend.resolve_image(normalized_ref)
            if resolved is None and self._allow_pull:
                if not self._backend.pull_image(normalized_ref):
                    raise RuntimeSnapshotBuildError(
                        "image_resolution_failed", "worker image is unavailable"
                    )
                resolved = self._backend.resolve_image(normalized_ref)
        except RuntimeSnapshotBuildError:
            raise
        except Exception as exc:
            raise RuntimeSnapshotBuildError(
                "image_resolution_failed", "worker image is unavailable"
            ) from exc

        image_id = resolved.get("image_id") if resolved is not None else None
        if not isinstance(image_id, str) or not _IMAGE_ID_RE.fullmatch(image_id):
            raise RuntimeSnapshotBuildError(
                "image_resolution_failed", "worker image is unavailable"
            )

        identity = None
        for user in self.WORKER_USER_CANDIDATES:
            try:
                identity = self._backend.query_user(
                    image_id,
                    user,
                    network="none",
                    mounts=(),
                    env={},
                )
            except Exception as exc:
                raise RuntimeSnapshotBuildError(
                    "worker_identity_mismatch", "worker identity could not be proven"
                ) from exc
            if (
                identity is not None
                and len(identity) == 2
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                    for value in identity
                )
            ):
                break
            identity = None
        if identity is None:
            raise RuntimeSnapshotBuildError(
                "worker_identity_mismatch",
                "worker identity could not be proven for any known worker user",
            )

        result = ResolvedWorkerImage(
            requested_ref=normalized_ref,
            image_id=image_id,
            uid=identity[0],
            gid=identity[1],
        )
        self._cache[normalized_ref] = result
        return result


def validate_shared_worker_identity(
    images: Sequence[ResolvedWorkerImage],
) -> tuple[int, int]:
    """Require every image in one run to expose the same numeric worker user."""

    if not images:
        raise RuntimeSnapshotBuildError(
            "worker_identity_mismatch", "no worker identity was resolved"
        )
    identities = {(image.uid, image.gid) for image in images}
    if len(identities) != 1:
        raise RuntimeSnapshotBuildError(
            "worker_identity_mismatch", "worker images use different identities"
        )
    uid, gid = next(iter(identities))
    if uid <= 0 or gid <= 0:
        raise RuntimeSnapshotBuildError(
            "worker_identity_mismatch", "worker identity could not be proven"
        )
    return uid, gid


def _network_spec(runtime: Mapping[str, Any]) -> RuntimeNetworkSpec:
    raw = str(runtime.get("network") or "").strip()
    lowered = raw.lower()
    if lowered in {"none", "bridge", "host"}:
        return RuntimeNetworkSpec(kind=lowered, name="")
    if lowered == "named":
        return RuntimeNetworkSpec(
            kind="named", name=str(runtime.get("network_name") or "").strip()
        )
    if raw:
        return RuntimeNetworkSpec(kind="named", name=raw)
    raise RuntimePolicyError("invalid_network")


def _resource_spec(runtime: Mapping[str, Any]) -> RuntimeResourceSpec:
    return RuntimeResourceSpec(
        cpus=str(runtime.get("cpus") or "1"),
        memory=str(runtime.get("memory") or "1g"),
        pids_limit=runtime.get("pids_limit") or 256,
        tmpfs_bytes=runtime.get("tmpfs_bytes") or 67108864,
    )


class RuntimeSnapshotBuilder:
    """Build one immutable, secret-free runtime snapshot for a run."""

    def __init__(
        self,
        image_inspector: DockerImageInspector | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._images = image_inspector or DockerImageInspector()
        self._clock = clock

    def build(
        self,
        *,
        run_id: str,
        policy: RuntimePolicy,
        worker_profiles: Sequence[Mapping[str, Any]],
        runtime_profiles: Sequence[Mapping[str, Any]],
        run_max_workers: int,
    ) -> RuntimeSnapshot:
        if not isinstance(policy, RuntimePolicy):
            raise RuntimeSnapshotBuildError("invalid_runtime_policy", "runtime policy is invalid")
        if not isinstance(run_max_workers, int) or isinstance(run_max_workers, bool) or run_max_workers <= 0:
            raise RuntimeSnapshotBuildError("invalid_run_max_workers", "run worker capacity is invalid")

        runtimes: dict[str, dict[str, Any]] = {}
        for raw_runtime in runtime_profiles:
            try:
                runtime = normalize_runtime_profile(raw_runtime, reject_invalid=True)
            except ValueError as exc:
                raise RuntimeSnapshotBuildError(
                    "invalid_runtime_profile", "runtime profile is invalid"
                ) from exc
            assert runtime is not None
            runtime_id = str(runtime["id"])
            if runtime_id in runtimes:
                raise RuntimeSnapshotBuildError(
                    "duplicate_runtime_profile", "runtime profile identity is duplicated"
                )
            runtimes[runtime_id] = runtime

        selected: list[tuple[str, Mapping[str, Any], Mapping[str, Any], ResolvedWorkerImage]] = []
        seen_profiles: set[str] = set()
        for profile in worker_profiles:
            if not isinstance(profile, Mapping):
                raise RuntimeSnapshotBuildError(
                    "invalid_worker_profile", "worker profile is invalid"
                )
            if profile.get("enabled", True) is False:
                continue
            profile_id = str(profile.get("name") or profile.get("id") or "").strip()
            if not profile_id:
                raise RuntimeSnapshotBuildError(
                    "invalid_worker_profile", "worker profile identity is missing"
                )
            if profile_id in seen_profiles:
                raise RuntimeSnapshotBuildError(
                    "duplicate_profile_mapping", "worker profile identity is duplicated"
                )
            seen_profiles.add(profile_id)
            runtime_id = str(profile.get("runtime") or "").strip()
            runtime = runtimes.get(runtime_id)
            if runtime is None:
                raise RuntimeSnapshotBuildError(
                    "runtime_profile_not_found", "worker runtime profile is unavailable"
                )
            if runtime.get("backend") != "container":
                raise RuntimeSnapshotBuildError(
                    "runtime_profile_not_container", "worker runtime is not containerized"
                )
            image_ref = str(profile.get("image") or "").strip()
            if not image_ref:
                raise RuntimeSnapshotBuildError(
                    "image_resolution_failed", "worker image is unavailable"
                )
            image = self._images.resolve(image_ref)
            selected.append((profile_id, profile, runtime, image))

        if not selected:
            raise RuntimeSnapshotBuildError(
                "no_worker_profiles", "no container worker profile is enabled"
            )
        if len(selected) > policy.max_pools_per_run:
            raise RuntimeSnapshotBuildError(
                "max_pools_per_run_exceeded", "run defines too many runtime pools"
            )

        shared_uid, shared_gid = validate_shared_worker_identity(
            [entry[3] for entry in selected]
        )
        pools: list[PoolSpec] = []
        try:
            for profile_id, profile, runtime, image in selected:
                explicit_cap = profile.get("pool_max_concurrent_workers")
                if explicit_cap is None:
                    explicit_cap = runtime.get("pool_max_concurrent_workers")
                pool_capacity = (
                    explicit_cap
                    if explicit_cap is not None
                    else policy.pool_max_concurrent_workers_default
                )
                if pool_capacity is None:
                    pool_capacity = run_max_workers

                runtime_features = runtime.get("runtime_features") or (
                    "rcp-v2",
                    "tool-disabled-probe",
                )
                credential_binding_id = str(
                    profile.get("credential_account")
                    or profile.get("credential_binding_id")
                    or ""
                ).strip()
                provider_binding_id = str(
                    profile.get("provider_ref")
                    or profile.get("provider_binding_id")
                    or profile.get("engine")
                    or ""
                ).strip()
                pool = PoolSpec.with_computed_id(
                    profile_id=profile_id,
                    runtime_kind=str(profile.get("engine") or "").strip(),
                    resolved_image_id=image.image_id,
                    requested_image_ref=image.requested_ref,
                    network=_network_spec(runtime),
                    resources=_resource_spec(runtime),
                    credential_binding_id=credential_binding_id,
                    provider_binding_id=provider_binding_id,
                    model=str(profile.get("model") or "").strip(),
                    uid=image.uid,
                    gid=image.gid,
                    runtime_features=tuple(runtime_features),
                    protocol_version=runtime.get("protocol_version") or 2,
                    pool_max_concurrent_workers=pool_capacity,
                )
                pools.append(pool)
        except (RuntimePolicyError, TypeError, ValueError) as exc:
            code = str(exc) if isinstance(exc, RuntimePolicyError) else "invalid_pool_spec"
            raise RuntimeSnapshotBuildError(code, "runtime pool specification is invalid") from exc

        pools.sort(key=lambda pool: (pool.profile_id, pool.pool_id))
        if len({pool.pool_id for pool in pools}) != len(pools):
            raise RuntimeSnapshotBuildError(
                "duplicate_pool_id", "runtime pool identity is duplicated"
            )
        try:
            return RuntimeSnapshot(
                version=policy.snapshot_version,
                run_id=run_id,
                created_at=float(self._clock()),
                runtime_policy=policy,
                shared_uid=shared_uid,
                shared_gid=shared_gid,
                pools=tuple(pools),
            )
        except RuntimePolicyError as exc:
            raise RuntimeSnapshotBuildError(
                str(exc), "runtime snapshot is invalid"
            ) from exc


_SNAPSHOT_KEYS = {
    "version",
    "run_id",
    "created_at",
    "runtime_policy",
    "shared_uid",
    "shared_gid",
    "pools",
}
_POOL_KEYS = {
    "pool_id",
    "profile_id",
    "runtime_kind",
    "resolved_image_id",
    "requested_image_ref",
    "network",
    "resources",
    "credential_binding_id",
    "provider_binding_id",
    "model",
    "uid",
    "gid",
    "runtime_features",
    "protocol_version",
    "pool_max_concurrent_workers",
}
_NETWORK_KEYS = {"kind", "name"}
_RESOURCE_KEYS = {"cpus", "memory", "pids_limit", "tmpfs_bytes"}
_POLICY_KEYS = set(RuntimePolicy.__dataclass_fields__)


def _snapshot_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "run_id": snapshot.run_id,
        "created_at": snapshot.created_at,
        "runtime_policy": asdict(snapshot.runtime_policy),
        "shared_uid": snapshot.shared_uid,
        "shared_gid": snapshot.shared_gid,
        "pools": [
            {
                "pool_id": pool.pool_id,
                "profile_id": pool.profile_id,
                "runtime_kind": pool.runtime_kind,
                "resolved_image_id": pool.resolved_image_id,
                "requested_image_ref": pool.requested_image_ref,
                "network": asdict(pool.network),
                "resources": asdict(pool.resources),
                "credential_binding_id": pool.credential_binding_id,
                "provider_binding_id": pool.provider_binding_id,
                "model": pool.model,
                "uid": pool.uid,
                "gid": pool.gid,
                "runtime_features": list(pool.runtime_features),
                "protocol_version": pool.protocol_version,
                "pool_max_concurrent_workers": pool.pool_max_concurrent_workers,
            }
            for pool in snapshot.pools
        ],
    }


def _exact_mapping(value: object, keys: set[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RuntimeSnapshotBuildError(code, "runtime snapshot has an invalid schema")
    return value


class RuntimeSnapshotStore:
    """Create-once durable storage for scheduler-private runtime snapshots."""

    def __init__(self, root: str | Path = "sessions") -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if (
            not isinstance(run_id, str)
            or not run_id
            or ".." in run_id
            or "/" in run_id
            or "\\" in run_id
            or any(ord(char) < 32 or ord(char) == 127 for char in run_id)
        ):
            raise RuntimeSnapshotBuildError("invalid_run_id", "run identity is invalid")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
        if not safe:
            raise RuntimeSnapshotBuildError("invalid_run_id", "run identity is invalid")
        return safe

    def path_for(self, run_id: str) -> Path:
        safe = self._safe_run_id(run_id)
        return self.root / safe / ".runtime" / "pool-snapshot.v1.json"

    def create(self, snapshot: RuntimeSnapshot) -> Path:
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot must be RuntimeSnapshot")
        path = self.path_for(snapshot.run_id)
        runtime_dir = path.parent
        with self._lock:
            if path.exists():
                raise RuntimeSnapshotBuildError(
                    "snapshot_already_exists", "runtime snapshot already exists"
                )
            try:
                runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Native Windows cannot reproduce POSIX owner-only isolation;
                # this mode is best-effort and Docker/Linux is the production
                # security boundary for runtime material.
                try:
                    os.chmod(runtime_dir, 0o700)
                except OSError:
                    pass
                temp = runtime_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
                encoded = (
                    json.dumps(
                        _snapshot_payload(snapshot),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                    + "\n"
                )
                with temp.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
                try:
                    directory_fd = os.open(
                        runtime_dir,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                except OSError:
                    directory_fd = None
                if directory_fd is not None:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                return path
            except RuntimeSnapshotBuildError:
                raise
            except Exception as exc:
                try:
                    if "temp" in locals() and temp.exists():
                        temp.unlink()
                except OSError:
                    pass
                raise RuntimeSnapshotBuildError(
                    "snapshot_write_failed", "runtime snapshot could not be persisted"
                ) from exc

    def load(self, run_id: str) -> RuntimeSnapshot:
        path = self.path_for(run_id)
        if not path.is_file():
            raise RuntimeSnapshotBuildError(
                "snapshot_not_found", "runtime snapshot is unavailable"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            root = _exact_mapping(raw, _SNAPSHOT_KEYS, "invalid_snapshot_schema")
            policy_raw = _exact_mapping(
                root["runtime_policy"], _POLICY_KEYS, "invalid_snapshot_schema"
            )
            policy = RuntimePolicy(**dict(policy_raw))
            pools: list[PoolSpec] = []
            if not isinstance(root["pools"], list):
                raise RuntimeSnapshotBuildError(
                    "invalid_snapshot_schema", "runtime snapshot has an invalid schema"
                )
            for raw_pool in root["pools"]:
                pool_data = _exact_mapping(
                    raw_pool, _POOL_KEYS, "invalid_snapshot_schema"
                )
                network_data = _exact_mapping(
                    pool_data["network"], _NETWORK_KEYS, "invalid_snapshot_schema"
                )
                resource_data = _exact_mapping(
                    pool_data["resources"], _RESOURCE_KEYS, "invalid_snapshot_schema"
                )
                values = dict(pool_data)
                values["network"] = RuntimeNetworkSpec(**dict(network_data))
                values["resources"] = RuntimeResourceSpec(**dict(resource_data))
                values["runtime_features"] = tuple(values["runtime_features"])
                pools.append(PoolSpec(**values))
            snapshot = RuntimeSnapshot(
                version=root["version"],
                run_id=root["run_id"],
                created_at=root["created_at"],
                runtime_policy=policy,
                shared_uid=root["shared_uid"],
                shared_gid=root["shared_gid"],
                pools=tuple(pools),
            )
            if snapshot.run_id != run_id:
                raise RuntimeSnapshotBuildError(
                    "snapshot_identity_mismatch", "runtime snapshot identity does not match"
                )
            return snapshot
        except RuntimeSnapshotBuildError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimePolicyError) as exc:
            raise RuntimeSnapshotBuildError(
                "invalid_snapshot", "runtime snapshot is invalid"
            ) from exc
