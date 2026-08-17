"""Proof-first cleanup primitives for run-scoped container generations.

This module deliberately treats Docker names as presentation only.  A generation is
removable only after the inspected container proves the complete frozen identity and
private-state mount contract.  Revocation is independent from container removal so a
failed Docker operation cannot leave usable RCP or worker credentials behind.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol, Sequence
import uuid

from dswarm.solver.container_runtime import ContainerInspection, ContainerMount

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REQUIRED_LABELS = (
    "com.dswarm.managed",
    "com.dswarm.run_id",
    "com.dswarm.pool_id",
    "com.dswarm.pool_instance_id",
    "com.dswarm.generation",
)


class CleanupDocker(Protocol):
    def inspect(self, container_id: str) -> ContainerInspection: ...

    def remove(self, container_id: str, *, force: bool) -> bool: ...


@dataclass(frozen=True)
class RuntimeCleanupExpectation:
    """The immutable identity/private-state contract for one generation."""

    container_id: str
    run_id: str
    pool_id: str
    pool_instance_id: str
    generation: int
    image_id: str
    network: str
    mounts: tuple[ContainerMount, ...]
    private_state_mounts: tuple[ContainerMount, ...]
    worker_token_ids: tuple[str, ...] = ()

    @property
    def labels(self) -> dict[str, str]:
        return {
            "com.dswarm.managed": "true",
            "com.dswarm.run_id": self.run_id,
            "com.dswarm.pool_id": self.pool_id,
            "com.dswarm.pool_instance_id": self.pool_instance_id,
            "com.dswarm.generation": str(self.generation),
        }


@dataclass(frozen=True)
class RuntimeCleanupVerdict:
    safe_to_remove: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeTerminationProof:
    """Independent proof components used by the cleanup barrier."""

    identity_proven: bool
    absence_proven: bool
    link_drained: bool
    pool_token_revoked: bool
    worker_tokens_revoked: bool
    failures: tuple[str, ...] = ()

    @property
    def proven(self) -> bool:
        return (
            self.identity_proven
            and self.absence_proven
            and self.link_drained
            and self.pool_token_revoked
            and self.worker_tokens_revoked
            and not self.failures
        )


@dataclass(frozen=True)
class RuntimeCleanupResult:
    container_id: str
    safe_to_remove: bool
    removed: bool
    absence_proven: bool
    pool_token_revoked: bool
    worker_tokens_revoked: bool
    failures: tuple[str, ...]
    proof: RuntimeTerminationProof

    @property
    def proven(self) -> bool:
        return self.proof.proven


class RuntimeCleanupInspector:
    """Validate every exact identity dimension before allowing removal."""

    def inspect_candidate(
        self,
        inspected: ContainerInspection,
        *,
        expected: RuntimeCleanupExpectation,
    ) -> RuntimeCleanupVerdict:
        reasons: list[str] = []
        labels = dict(inspected.labels)
        identity_values = (expected.run_id, expected.pool_id, str(expected.generation))
        if (
            expected.generation <= 0
            or any(not _valid_identity(value) for value in identity_values)
            or not _valid_pool_instance_id(expected.pool_instance_id)
        ):
            reasons.append("expected_identity_invalid")
        if inspected.container_id != expected.container_id:
            reasons.append("container_id_mismatch")
        for key in _REQUIRED_LABELS:
            if labels.get(key) != expected.labels[key]:
                reasons.append(f"label_mismatch:{key.rsplit('.', 1)[-1]}")
        if any(key.startswith("com.dswarm.") and key not in _REQUIRED_LABELS for key in labels):
            reasons.append("unexpected_managed_label")
        if not _valid_identity(labels.get("com.dswarm.run_id")):
            reasons.append("run_identity_invalid")
        if not _valid_identity(labels.get("com.dswarm.pool_id")):
            reasons.append("pool_identity_invalid")
        if not _valid_pool_instance_id(labels.get("com.dswarm.pool_instance_id")):
            reasons.append("pool_instance_identity_invalid")
        if labels.get("com.dswarm.generation", "").isdigit() is False:
            reasons.append("generation_identity_invalid")
        if inspected.image_id != expected.image_id:
            reasons.append("image_mismatch")
        if inspected.network != expected.network:
            reasons.append("network_mismatch")
        if _sorted_mounts(inspected.mounts) != _sorted_mounts(expected.mounts):
            reasons.append("mount_mismatch")
        actual_mounts = set(_mount_key(mount) for mount in inspected.mounts)
        if not expected.private_state_mounts or any(
            _mount_key(mount) not in actual_mounts for mount in expected.private_state_mounts
        ):
            reasons.append("private_state_mount_mismatch")
        return RuntimeCleanupVerdict(safe_to_remove=not reasons, reasons=tuple(reasons))


def cleanup_pool_generation(
    *,
    docker: CleanupDocker,
    expected: RuntimeCleanupExpectation,
    receiver: Any | None = None,
    worker_token_revoker: Any | None = None,
    link_drained: bool = True,
) -> RuntimeCleanupResult:
    """Revoke credentials and prove exact container absence for one generation."""

    failures: list[str] = []
    safe_to_remove = False
    identity_proven = False
    removed = False
    absence_proven = False
    inspection: ContainerInspection | None = None
    try:
        inspection = docker.inspect(expected.container_id)
    except Exception as exc:
        if _absence_proven_from_error(docker, expected.container_id, exc):
            absence_proven = True
            removed = True
            identity_proven = True
        else:
            failures.append("inspect_failed")
    if inspection is not None:
        verdict = RuntimeCleanupInspector().inspect_candidate(inspection, expected=expected)
        safe_to_remove = verdict.safe_to_remove
        if not verdict.safe_to_remove:
            failures.extend(verdict.reasons)
        else:
            identity_proven = True
            try:
                removed = bool(docker.remove(expected.container_id, force=True))
            except Exception:
                failures.append("remove_failed")
            if not removed:
                failures.append("remove_failed")
            else:
                try:
                    post = docker.inspect(expected.container_id)
                except Exception as exc:
                    if _absence_proven_from_error(docker, expected.container_id, exc):
                        absence_proven = True
                    else:
                        failures.append("absence_unproven")
                else:
                    del post
                    failures.append("absence_unproven")

    pool_token_revoked = receiver is None
    token_revoker = worker_token_revoker if worker_token_revoker is not None else receiver
    worker_tokens_revoked = token_revoker is None or not expected.worker_token_ids
    if receiver is not None:
        try:
            receiver.revoke_pool_instance(expected.pool_instance_id)
            pool_token_revoked = True
        except Exception:
            failures.append("pool_token_revoke_failed")
            pool_token_revoked = False
    if token_revoker is not None:
        worker_tokens_revoked = True
        for token_id in expected.worker_token_ids:
            try:
                _revoke_worker_token(token_revoker, token_id)
            except Exception:
                failures.append("worker_token_revoke_failed")
                worker_tokens_revoked = False

    proof = RuntimeTerminationProof(
        identity_proven=identity_proven,
        absence_proven=absence_proven,
        link_drained=bool(link_drained),
        pool_token_revoked=pool_token_revoked,
        worker_tokens_revoked=worker_tokens_revoked,
        failures=tuple(dict.fromkeys(failures)),
    )
    return RuntimeCleanupResult(
        container_id=expected.container_id,
        safe_to_remove=safe_to_remove,
        removed=removed,
        absence_proven=absence_proven,
        pool_token_revoked=pool_token_revoked,
        worker_tokens_revoked=worker_tokens_revoked,
        failures=proof.failures,
        proof=proof,
    )


def _revoke_worker_token(receiver: Any, token_id: str) -> None:
    for name in ("revoke_worker_token", "revoke_token", "revoke"):
        method = getattr(receiver, name, None)
        if callable(method):
            method(token_id)
            return
    raise RuntimeError("worker token revocation unavailable")


def _absence_proven_from_error(docker: Any, container_id: str, exc: BaseException) -> bool:
    strong_absence = _is_explicit_absence_error(exc)
    listing = getattr(docker, "list", None)
    if not callable(listing):
        return strong_absence
    try:
        values = listing(container_id=container_id)
    except Exception:
        return False
    if values:
        return False
    return strong_absence or getattr(exc, "code", "") == "container_inspect_failed"


def _is_explicit_absence_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, (LookupError, FileNotFoundError)) or any(
        marker in text for marker in ("no such container", "not found", "404")
    )


def _valid_identity(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value.strip())) and value == value.strip()


def _valid_pool_instance_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _mount_key(mount: ContainerMount) -> tuple[str, str, bool]:
    return (mount.source, mount.target, bool(mount.read_only))


def _sorted_mounts(mounts: Sequence[ContainerMount]) -> tuple[tuple[str, str, bool], ...]:
    return tuple(sorted((_mount_key(mount) for mount in mounts)))

