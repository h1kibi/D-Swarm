"""Production composition for frozen per-run Docker worker pools.

The factory in this module is intentionally control-plane only.  It creates or
reloads one immutable runtime snapshot, wires the M5 durable usage boundary used
by runtime probes, and returns the strict Docker context expected by ``Swarm``.
It never falls back to host-local workers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from dswarm.core.usage_journal import UsageJournal, UsageWriter
from dswarm.solver.container_pool import ContainerPoolManager
from dswarm.solver.container_runtime import ContainerRuntimeExecutor
from dswarm.solver.control_receiver import ControlReceiver
from dswarm.solver.credential_accounts import account_store_root
from dswarm.solver.runtime_credentials import CredentialProjector
from dswarm.solver.runtime_diagnostics import RuntimeDiagnosticsStore
from dswarm.solver.runtime_policy import RuntimeSnapshot, build_runtime_policy
from dswarm.solver.runtime_probe import RuntimeProbe
from dswarm.solver.runtime_snapshot import (
    RuntimeSnapshotBuildError,
    RuntimeSnapshotBuilder,
    RuntimeSnapshotStore,
)


def build_pool_manager_for_run(
    *,
    run_id: str,
    snapshot: RuntimeSnapshot,
    worker_profiles: Sequence[Mapping[str, Any]],
    sessions_root: str | Path,
    bus: Any = None,
    budget_gate: Any | None = None,
    pool_manager_factory: Callable[..., Any] | None = None,
    receiver: Any | None = None,
) -> ContainerPoolManager:
    """Compose the M9a pool manager (executor/probe/credentials) for one snapshot.

    Extracted from :func:`build_docker_runtime_context` so control planes that
    already own snapshot freezing (the web RunManager) can reuse the exact same
    production composition without re-deriving usage/credential wiring.
    """
    root = Path(sessions_root)
    journal = UsageJournal(root / f"{run_id}-usage-journal.jsonl")
    usage_writer = UsageWriter(journal, bus=bus)
    probe = RuntimeProbe(usage_writer=usage_writer, budget_gate=budget_gate)
    projector = CredentialProjector(account_store_root(root), root)
    profile_by_id = {
        str(profile.get("name") or profile.get("id") or "").strip(): profile
        for profile in worker_profiles
    }
    credential_modes = {
        pool.pool_id: _credential_mode(profile_by_id.get(pool.profile_id, {}))
        for pool in snapshot.pools
    }
    diagnostics_store = RuntimeDiagnosticsStore(
        run_root=root / run_id, run_id=run_id
    )

    def record_transition(view: Any, error: str | None) -> None:
        diagnostics_store.record_transition(view, error=error)

    async def executor_factory(*, run_id: str, pool_spec: Any, generation: int) -> Any:
        active_receiver = receiver or ControlReceiver.instance()
        return await ContainerRuntimeExecutor.create(
            run_id=run_id,
            pool_spec=pool_spec,
            generation=generation,
            run_root=root / run_id,
            receiver=active_receiver,
        )

    manager_type = pool_manager_factory or ContainerPoolManager
    manager = manager_type(
        run_id=run_id,
        snapshot=snapshot,
        executor_factory=executor_factory,
        probe=probe,
        credential_projector=projector,
        credential_modes=credential_modes,
        transition_callback=record_transition,
    )
    if getattr(manager, "run_id", None) != run_id:
        raise RuntimeSnapshotBuildError(
            "runtime_manager_run_mismatch", "runtime manager identity is invalid"
        )
    if getattr(manager, "snapshot", None) is not snapshot:
        raise RuntimeSnapshotBuildError(
            "runtime_manager_snapshot_mismatch", "runtime manager snapshot is invalid"
        )
    return manager


def build_docker_runtime_context(
    *,
    run_id: str,
    sessions_root: str | Path,
    bus: Any,
    budget_gate: Any,
    worker_profiles: Sequence[Mapping[str, Any]],
    runtime_profiles: Sequence[Mapping[str, Any]],
    run_max_workers: int,
    snapshot_builder: RuntimeSnapshotBuilder | None = None,
    snapshot_store: RuntimeSnapshotStore | None = None,
    pool_manager_factory: Callable[..., Any] | None = None,
    receiver: Any | None = None,
) -> dict[str, Any]:
    """Build or reload the strict Docker runtime context for one run.

    ``worker_profiles`` must already be the run's selected profile roster.  The
    create-once snapshot is the authority on reopen; changed live configuration
    cannot silently mutate an existing run's pool identity.
    """

    profiles = [dict(profile) for profile in worker_profiles]
    if not profiles:
        raise RuntimeSnapshotBuildError(
            "no_worker_profiles", "no container worker profile is enabled"
        )
    if (
        isinstance(run_max_workers, bool)
        or not isinstance(run_max_workers, int)
        or run_max_workers <= 0
    ):
        raise RuntimeSnapshotBuildError(
            "invalid_run_max_workers", "run worker capacity is invalid"
        )

    root = Path(sessions_root)
    store = snapshot_store or RuntimeSnapshotStore(root)
    snapshot_path = store.path_for(run_id)
    if snapshot_path.is_file():
        snapshot = store.load(run_id)
    else:
        policy = build_runtime_policy(mode="docker")
        builder = snapshot_builder or RuntimeSnapshotBuilder()
        snapshot = builder.build(
            run_id=run_id,
            policy=policy,
            worker_profiles=profiles,
            runtime_profiles=list(runtime_profiles),
            run_max_workers=run_max_workers,
        )
        store.create(snapshot)

    _validate_snapshot_profiles(snapshot, profiles)

    manager = build_pool_manager_for_run(
        run_id=run_id,
        snapshot=snapshot,
        worker_profiles=profiles,
        sessions_root=root,
        bus=bus,
        budget_gate=budget_gate,
        pool_manager_factory=pool_manager_factory,
        receiver=receiver,
    )

    return {
        "runtime_policy": snapshot.runtime_policy,
        "runtime_snapshot": snapshot,
        "pool_manager": manager,
        "worker_backend": "container",
    }


def _credential_mode(profile: Mapping[str, Any]) -> str:
    if profile.get("base_url") or profile.get("api_key_ref"):
        return "direct"
    # A provider_ref-only binding owns its secret on the relay side; container
    # workers authenticate with gateway task tokens (never the upstream key).
    # ("direct" would make the projector look for an accounts-store entry that
    # provider bindings intentionally do not have.)
    return "gateway"


def _validate_snapshot_profiles(
    snapshot: RuntimeSnapshot,
    profiles: Sequence[Mapping[str, Any]],
) -> None:
    expected = sorted(
        str(profile.get("name") or profile.get("id") or "").strip()
        for profile in profiles
    )
    actual = sorted(pool.profile_id for pool in snapshot.pools)
    if not expected or any(not profile_id for profile_id in expected):
        raise RuntimeSnapshotBuildError(
            "invalid_worker_profile", "worker profile identity is missing"
        )
    if actual != expected:
        raise RuntimeSnapshotBuildError(
            "runtime_snapshot_profile_mismatch",
            "runtime snapshot does not match the selected worker profiles",
        )
