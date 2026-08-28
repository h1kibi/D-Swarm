"""Per-operation, one-binding credential projections for container Workers.

This module intentionally does not resolve credentials from the host environment.
Gateway operations receive no provider files; direct/custom operations receive a
private copy of exactly one frozen binding for the lifetime of one Worker/Probe.
"""

from __future__ import annotations

from hashlib import blake2b
import os
from pathlib import Path
import shutil
import threading
from typing import Literal, Mapping
from urllib.parse import quote

from dswarm.solver.credential_accounts import valid_account_id


CONTAINER_CREDENTIAL_ROOT = "/run/dswarm/accounts"
_ALLOWED_BINDING_FILES = ("API_KEY", "BASE_URL", "ENGINE")
_MAX_BINDING_FILE_BYTES = 64 * 1024
CredentialMode = Literal["gateway", "direct", "custom"]


class CredentialProjectionError(RuntimeError):
    """Structured credential projection failure with operator-safe detail."""

    def __init__(self, code: str, safe_detail: str) -> None:
        self.code = code
        self.safe_detail = safe_detail
        super().__init__(f"{code}: {safe_detail}")


class CredentialProjectionCleanupError(CredentialProjectionError):
    """Cleanup could not prove the per-operation credential root was removed."""


class CredentialProjectionLease:
    """Lifetime handle for one Worker's private credential projection."""

    def __init__(
        self,
        *,
        root: Path | None,
        operation_root: Path | None,
        env: Mapping[str, str],
        binding_id: str,
        credential_mode: CredentialMode,
        credential_version_digest: str,
    ) -> None:
        self.root = root
        self.env = dict(env)
        self.binding_id = binding_id
        self.credential_mode = credential_mode
        self.credential_version_digest = credential_version_digest
        self._operation_root = operation_root
        self._closed = False
        self._close_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Remove this operation's private root; repeated success is a no-op."""

        with self._close_lock:
            if self._closed:
                return
            if self._operation_root is not None:
                try:
                    shutil.rmtree(self._operation_root)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise CredentialProjectionCleanupError(
                        "credential_cleanup_failed",
                        "worker credential cleanup could not be confirmed",
                    ) from exc
                if self._operation_root.exists():
                    raise CredentialProjectionCleanupError(
                        "credential_cleanup_failed",
                        "worker credential cleanup could not be confirmed",
                    )
            self._closed = True

    def __enter__(self) -> CredentialProjectionLease:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "CredentialProjectionLease("
            f"binding_id={self.binding_id!r}, credential_mode={self.credential_mode!r}, "
            f"credential_version_digest={self.credential_version_digest!r}, "
            f"closed={self.closed!r})"
        )


def _safe_identity_component(value: str, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise CredentialProjectionError(code, "credential projection identity is invalid")
    encoded = quote(value, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
    if not encoded or encoded in {".", ".."}:
        raise CredentialProjectionError(code, "credential projection identity is invalid")
    return encoded


def _version_digest(
    *, mode: CredentialMode, binding_id: str, material: Mapping[str, bytes]
) -> str:
    digest = blake2b(digest_size=20)
    digest.update(b"credential-version-v1\0")
    digest.update(mode.encode("utf-8"))
    digest.update(b"\0")
    digest.update(binding_id.encode("utf-8"))
    for filename in sorted(material):
        digest.update(b"\0")
        digest.update(filename.encode("ascii"))
        digest.update(b"\0")
        digest.update(material[filename])
    return "cred-v1::" + digest.hexdigest()


def _read_binding_material(source: Path, *, mode: CredentialMode) -> dict[str, bytes]:
    material: dict[str, bytes] = {}
    for filename in _ALLOWED_BINDING_FILES:
        path = source / filename
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise CredentialProjectionError(
                "credential_binding_unavailable", "credential binding is unavailable"
            )
        try:
            value = path.read_bytes()
        except OSError as exc:
            raise CredentialProjectionError(
                "credential_binding_unavailable", "credential binding is unavailable"
            ) from exc
        if len(value) > _MAX_BINDING_FILE_BYTES:
            raise CredentialProjectionError(
                "credential_binding_unavailable", "credential binding is unavailable"
            )
        material[filename] = value

    if not material.get("API_KEY", b"").strip():
        raise CredentialProjectionError(
            "credential_binding_unavailable", "credential binding is unavailable"
        )
    if mode == "custom" and not material.get("BASE_URL", b"").strip():
        raise CredentialProjectionError(
            "credential_binding_unavailable", "credential binding is unavailable"
        )
    return material


def _write_private_file(path: Path, value: bytes) -> None:
    # Native Windows may not honor POSIX owner-only bits. Keep the request for
    # Linux/Docker and do not treat host-side credential staging as isolation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        path.chmod(0o600)
    except OSError:
        pass


class CredentialProjector:
    """Create one non-enumerable binding projection per Worker operation."""

    def __init__(self, account_root: str | Path, sessions_root: str | Path) -> None:
        self._account_root = Path(account_root)
        self._sessions_root = Path(sessions_root)

    def project(
        self,
        *,
        run_id: str,
        pool_id: str,
        worker_instance_id: str,
        binding_id: str,
        credential_mode: CredentialMode,
    ) -> CredentialProjectionLease:
        if credential_mode not in {"gateway", "direct", "custom"}:
            raise CredentialProjectionError(
                "invalid_credential_mode", "credential mode is invalid"
            )
        safe_run = _safe_identity_component(run_id, code="invalid_projection_identity")
        safe_pool = _safe_identity_component(pool_id, code="invalid_projection_identity")
        safe_worker = _safe_identity_component(
            worker_instance_id, code="invalid_projection_identity"
        )
        if not valid_account_id(binding_id):
            raise CredentialProjectionError(
                "credential_binding_unavailable", "credential binding is unavailable"
            )

        if credential_mode == "gateway":
            return CredentialProjectionLease(
                root=None,
                operation_root=None,
                env={},
                binding_id=binding_id,
                credential_mode=credential_mode,
                credential_version_digest=_version_digest(
                    mode=credential_mode, binding_id=binding_id, material={}
                ),
            )

        source = self._account_root / binding_id
        if not source.is_dir() or source.is_symlink():
            raise CredentialProjectionError(
                "credential_binding_unavailable", "credential binding is unavailable"
            )
        material = _read_binding_material(source, mode=credential_mode)
        version_digest = _version_digest(
            mode=credential_mode, binding_id=binding_id, material=material
        )

        operation_root = (
            self._sessions_root
            / safe_run
            / ".runtime"
            / "pools"
            / safe_pool
            / "workers"
            / safe_worker
        )
        credential_root = operation_root / "credentials"
        binding_root = credential_root / binding_id
        try:
            operation_root.mkdir(parents=True, exist_ok=False, mode=0o700)
            credential_root.mkdir(mode=0o700)
            binding_root.mkdir(mode=0o700)
            # Directory modes are enforced by the Linux container runtime;
            # on native Windows they are only best-effort ACL/mode hints.
            for private_dir in (operation_root, credential_root, binding_root):
                try:
                    private_dir.chmod(0o700)
                except OSError:
                    pass
            for filename, value in material.items():
                _write_private_file(binding_root / filename, value)
        except FileExistsError as exc:
            raise CredentialProjectionError(
                "credential_projection_exists",
                "worker credential projection already exists",
            ) from exc
        except CredentialProjectionError:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(operation_root, ignore_errors=True)
            raise CredentialProjectionError(
                "credential_projection_failed",
                "worker credential projection could not be created",
            ) from exc

        return CredentialProjectionLease(
            root=credential_root,
            operation_root=operation_root,
            env={
                "DSWARM_CREDENTIAL_ROOT": CONTAINER_CREDENTIAL_ROOT,
                "DSWARM_CREDENTIAL_BINDING_ID": binding_id,
            },
            binding_id=binding_id,
            credential_mode=credential_mode,
            credential_version_digest=version_digest,
        )
