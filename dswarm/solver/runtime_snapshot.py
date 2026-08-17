"""Frozen Worker image resolution and numeric identity preflight."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from dswarm.solver.docker import docker_run


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
    """Resolve each requested tag once and prove the image's ``kali`` identity."""

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

        try:
            identity = self._backend.query_user(
                image_id,
                "kali",
                network="none",
                mounts=(),
                env={},
            )
        except Exception as exc:
            raise RuntimeSnapshotBuildError(
                "worker_identity_mismatch", "worker identity could not be proven"
            ) from exc
        if (
            identity is None
            or len(identity) != 2
            or not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                for value in identity
            )
        ):
            raise RuntimeSnapshotBuildError(
                "worker_identity_mismatch", "worker identity could not be proven"
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
    """Require every image in one run to expose the same numeric ``kali`` user."""

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
