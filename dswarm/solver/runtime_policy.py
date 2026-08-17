"""Immutable runtime execution policy for Docker-first Worker launches."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Literal, Mapping


_TRUTHY = frozenset({"1", "true", "yes", "on"})


class RuntimePolicyError(ValueError):
    """Raised when a runtime policy violates the frozen V1 contract."""


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


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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
    """Validate and freeze the run's Worker execution policy.

    Host-local execution is deliberately fail-closed: selecting ``local_dev`` is
    accepted only when both the caller's explicit flag and the documented
    environment gate are present. Test-process ambient state is never consulted.
    """

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
