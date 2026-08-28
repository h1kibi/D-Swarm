"""Reproduce the M9a lease path for one container profile, no LLM involved.

Freezes the runtime context exactly like RunManager.start does, then requests
one worker lease and prints whatever the pool/probe chain really says.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.web.worker_config import WorkerConfigStore
from dswarm.solver.runtime_policy import build_runtime_policy
from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuilder
from dswarm.swarm.runtime import RuntimeSpawnRequest, runtime_lease_factory_for_request


async def main() -> None:
    sessions = Path(tempfile.mkdtemp(prefix="repro-pool-", dir="sessions"))
    run_id = "run-repro"
    store = WorkerConfigStore(root=Path("sessions"))
    cfg = store.resolve("")
    profiles = [p for p in cfg.get("worker_profiles", []) if isinstance(p, dict)]
    if not profiles:
        print("no profiles in stored config")
        return
    profile = profiles[0]
    print(f"profile: {profile.get('name')} image={profile.get('image')} "
          f"runtime={profile.get('runtime')}")
    runtime_profiles = cfg.get("runtime_profiles") or []

    policy = build_runtime_policy(mode="docker")
    builder = RuntimeSnapshotBuilder()
    snapshot = builder.build(
        run_id=run_id,
        policy=policy,
        worker_profiles=[profile],
        runtime_profiles=runtime_profiles,
        run_max_workers=2,
    )
    print(f"snapshot ok: pools={[p.pool_id for p in snapshot.pools]}")

    from apps.web.run_manager import RunManager
    mgr = RunManager(sessions_root=sessions)
    policy2, snap2, pool = mgr.ensure_runtime_context(
        run_id,
        policy=policy,
        worker_profiles=[profile],
        runtime_profiles=runtime_profiles,
        run_max_workers=2,
    )
    print(f"frozen: policy={policy2.mode} pool_manager={type(pool).__name__}")

    request = RuntimeSpawnRequest(
        profile_id=str(profile.get("name") or profile.get("id")),
        worker_instance_id="repro-worker-1",
        operation_kind="review",
        mode="review",
        intent_id="intent-repro",
    )

    lease_factory = runtime_lease_factory_for_request(
        snapshot=snap2, pool_manager=pool, request=request
    )
    print("requesting lease (pool container + pi probe)...")
    lease = await asyncio.wait_for(lease_factory("repro-worker-1", "review"), timeout=180)
    print("LEASE OK:", type(lease).__name__, getattr(lease, "pool_id", ""))
    release = getattr(lease, "release", None)
    if callable(release):
        await release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 — this script exists to surface errors
        print(f"REPRO FAILURE: {type(exc).__name__}: {exc}")
        raise
