"""Immutable runtime policy, PoolKey, and frozen snapshot models."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
import re
from typing import Any, Literal, Mapping, TypeAlias


_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SIMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_NETWORK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEMORY_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([kmgtpe]?i?b?)?$", re.IGNORECASE)
_POOL_ID_RE = re.compile(r"^pool-v1::[0-9a-f]{40}$")

RuntimePoolIdentity: TypeAlias = tuple[str, str]


class RuntimePolicyError(ValueError):
    """Raised when a runtime policy or frozen runtime model is invalid."""


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_simple_id(value: object, error: str) -> str:
    if not isinstance(value, str) or not _SIMPLE_ID_RE.fullmatch(value):
        raise RuntimePolicyError(error)
    return value


def _validate_model_id(value: object) -> str:
    if not isinstance(value, str) or not _MODEL_ID_RE.fullmatch(value):
        raise RuntimePolicyError("invalid_model")
    return value


def _validate_bounded_text(value: object, error: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise RuntimePolicyError(error)
    return value


def _canonical_positive_decimal(value: object, error: str) -> str:
    if isinstance(value, bool):
        raise RuntimePolicyError(error)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimePolicyError(error) from exc
    if not number.is_finite() or number <= 0:
        raise RuntimePolicyError(error)
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


@dataclass(frozen=True)
class RuntimePolicy:
    mode: Literal["docker", "local_dev"]
    local_dev_cli_flag: bool
    local_dev_env_allowed: bool
    max_pools_per_run: int = 32
    pool_max_concurrent_workers_default: int | None = None
    probe_timeout_seconds: float = 45.0
    recovery_attempts_per_episode: int = 1
    snapshot_version: int = 1

    @property
    def local_workers_allowed(self) -> bool:
        return (
            self.mode == "local_dev"
            and self.local_dev_cli_flag
            and self.local_dev_env_allowed
        )


@dataclass(frozen=True)
class RuntimeNetworkSpec:
    kind: Literal["none", "bridge", "named"]
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str):
            raise RuntimePolicyError("invalid_network")
        normalized_kind = self.kind.strip().lower()
        if normalized_kind not in {"none", "bridge", "named"}:
            raise RuntimePolicyError("invalid_network")
        normalized_name = self.name.strip() if isinstance(self.name, str) else ""
        if normalized_kind == "named":
            if not _NETWORK_NAME_RE.fullmatch(normalized_name):
                raise RuntimePolicyError("invalid_network")
        elif normalized_name:
            raise RuntimePolicyError("invalid_network")
        object.__setattr__(self, "kind", normalized_kind)
        object.__setattr__(self, "name", normalized_name)


@dataclass(frozen=True)
class RuntimeResourceSpec:
    cpus: str
    memory: str
    pids_limit: int
    tmpfs_bytes: int

    def __post_init__(self) -> None:
        normalized_cpus = _canonical_positive_decimal(self.cpus, "invalid_cpus")
        if not isinstance(self.memory, str):
            raise RuntimePolicyError("invalid_memory")
        memory_match = _MEMORY_RE.fullmatch(self.memory.strip())
        if memory_match is None:
            raise RuntimePolicyError("invalid_memory")
        memory_number = _canonical_positive_decimal(memory_match.group(1), "invalid_memory")
        normalized_memory = memory_number + memory_match.group(2).lower()
        if not _is_positive_int(self.pids_limit):
            raise RuntimePolicyError("invalid_pids_limit")
        if not _is_positive_int(self.tmpfs_bytes):
            raise RuntimePolicyError("invalid_tmpfs_bytes")
        object.__setattr__(self, "cpus", normalized_cpus)
        object.__setattr__(self, "memory", normalized_memory)


@dataclass(frozen=True)
class PoolSpec:
    pool_id: str
    profile_id: str
    runtime_kind: Literal["pi"]
    resolved_image_id: str
    requested_image_ref: str
    network: RuntimeNetworkSpec
    resources: RuntimeResourceSpec
    credential_binding_id: str
    provider_binding_id: str
    model: str
    uid: int
    gid: int
    runtime_features: tuple[str, ...]
    protocol_version: int
    pool_max_concurrent_workers: int

    def __post_init__(self) -> None:
        _validate_simple_id(self.profile_id, "invalid_profile_id")
        if self.runtime_kind != "pi":
            raise RuntimePolicyError("invalid_runtime_kind")
        _validate_bounded_text(self.resolved_image_id, "invalid_resolved_image_id")
        _validate_bounded_text(self.requested_image_ref, "invalid_requested_image_ref")
        if not isinstance(self.network, RuntimeNetworkSpec):
            raise RuntimePolicyError("invalid_network")
        if not isinstance(self.resources, RuntimeResourceSpec):
            raise RuntimePolicyError("invalid_resources")
        _validate_simple_id(
            self.credential_binding_id, "invalid_credential_binding_id"
        )
        _validate_simple_id(self.provider_binding_id, "invalid_provider_binding_id")
        _validate_model_id(self.model)
        if not _is_positive_int(self.uid):
            raise RuntimePolicyError("invalid_uid")
        if not _is_positive_int(self.gid):
            raise RuntimePolicyError("invalid_gid")
        if not _is_positive_int(self.protocol_version):
            raise RuntimePolicyError("invalid_protocol_version")
        if not _is_positive_int(self.pool_max_concurrent_workers):
            raise RuntimePolicyError("invalid_pool_worker_cap")
        if not isinstance(self.runtime_features, tuple):
            raise RuntimePolicyError("invalid_runtime_features")
        normalized_features = tuple(
            sorted(
                {
                    _validate_simple_id(feature, "invalid_runtime_features")
                    for feature in self.runtime_features
                }
            )
        )
        if not normalized_features:
            raise RuntimePolicyError("invalid_runtime_features")
        object.__setattr__(self, "runtime_features", normalized_features)

        if self.pool_id:
            if not _POOL_ID_RE.fullmatch(self.pool_id):
                raise RuntimePolicyError("pool_id_mismatch")
            if self.pool_id != pool_id_for_spec(self):
                raise RuntimePolicyError("pool_id_mismatch")

    @classmethod
    def with_computed_id(cls, **values: Any) -> PoolSpec:
        if "pool_id" in values:
            raise TypeError("pool_id is computed")
        candidate = cls(pool_id="", **values)
        return replace(candidate, pool_id=pool_id_for_spec(candidate))


@dataclass(frozen=True)
class RuntimeSnapshot:
    version: int
    run_id: str
    created_at: float
    runtime_policy: RuntimePolicy
    shared_uid: int
    shared_gid: int
    pools: tuple[PoolSpec, ...]


def canonical_pool_payload(spec: PoolSpec) -> bytes:
    """Return the exact secret-free canonical PoolKey payload."""

    if not isinstance(spec, PoolSpec):
        raise TypeError("spec must be PoolSpec")
    payload = {
        "credential_binding_id": spec.credential_binding_id,
        "gid": spec.gid,
        "model": spec.model,
        "network": {"kind": spec.network.kind, "name": spec.network.name},
        "pool_max_concurrent_workers": spec.pool_max_concurrent_workers,
        "profile_id": spec.profile_id,
        "protocol_version": spec.protocol_version,
        "provider_binding_id": spec.provider_binding_id,
        "resolved_image_id": spec.resolved_image_id,
        "resources": {
            "cpus": spec.resources.cpus,
            "memory": spec.resources.memory,
            "pids_limit": spec.resources.pids_limit,
            "tmpfs_bytes": spec.resources.tmpfs_bytes,
        },
        "runtime_features": list(spec.runtime_features),
        "runtime_kind": spec.runtime_kind,
        "uid": spec.uid,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def pool_id_for_spec(spec: PoolSpec) -> str:
    digest = hashlib.blake2b(canonical_pool_payload(spec), digest_size=20).hexdigest()
    return f"pool-v1::{digest}"


def build_runtime_policy(
    *,
    mode: str = "docker",
    local_dev_cli_flag: bool = False,
    env: Mapping[str, str] | None = None,
    max_pools_per_run: int = 32,
    pool_max_concurrent_workers_default: int | None = None,
    probe_timeout_seconds: float = 45.0,
    recovery_attempts_per_episode: int = 1,
) -> RuntimePolicy:
    """Validate and freeze the run's Worker execution policy."""

    if mode not in {"docker", "local_dev"}:
        raise RuntimePolicyError("invalid_runtime_mode")

    source_env = os.environ if env is None else env
    env_value = source_env.get("DSWARM_ALLOW_LOCAL_WORKERS", "")
    local_dev_env_allowed = str(env_value).strip().lower() in _TRUTHY

    if not _is_positive_int(max_pools_per_run) or max_pools_per_run > 128:
        raise RuntimePolicyError("invalid_max_pools_per_run")

    if (
        pool_max_concurrent_workers_default is not None
        and not _is_positive_int(pool_max_concurrent_workers_default)
    ):
        raise RuntimePolicyError("invalid_pool_worker_cap")

    if isinstance(probe_timeout_seconds, bool):
        raise RuntimePolicyError("invalid_probe_timeout_seconds")
    try:
        normalized_timeout = float(probe_timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise RuntimePolicyError("invalid_probe_timeout_seconds") from exc
    if not math.isfinite(normalized_timeout) or normalized_timeout <= 0:
        raise RuntimePolicyError("invalid_probe_timeout_seconds")

    if recovery_attempts_per_episode != 1 or isinstance(
        recovery_attempts_per_episode, bool
    ):
        raise RuntimePolicyError("invalid_recovery_attempts_per_episode")

    policy = RuntimePolicy(
        mode=mode,
        local_dev_cli_flag=bool(local_dev_cli_flag),
        local_dev_env_allowed=local_dev_env_allowed,
        max_pools_per_run=max_pools_per_run,
        pool_max_concurrent_workers_default=pool_max_concurrent_workers_default,
        probe_timeout_seconds=normalized_timeout,
        recovery_attempts_per_episode=recovery_attempts_per_episode,
    )
    if mode == "local_dev" and not policy.local_workers_allowed:
        raise RuntimePolicyError("local_worker_policy_denied")
    return policy
