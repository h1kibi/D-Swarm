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

    # diagnostics: capture pool container death (identity validate + exec probe)
    import dswarm.solver.container_runtime as cr
    import subprocess as _sp

    def _dump_container(container_id: str, tag: str) -> None:
        state = _sp.run(["docker", "inspect", "--format",
                         "{{.State.Status}} exit={{.State.ExitCode}} "
                         "err={{.State.Error}}", container_id],
                        capture_output=True, text=True)
        logs = _sp.run(["docker", "logs", "--tail", "40", container_id],
                       capture_output=True, text=True)
        print(f"{tag} STATE:", state.stdout.strip() or state.stderr.strip())
        print(f"{tag} LOGS:", (logs.stdout + logs.stderr)[:1600])

    _orig_validate = cr._validate_inspection

    def _patched_validate(request, inspection, container_id):
        try:
            _orig_validate(request, inspection, container_id)
        except cr.ContainerRuntimeError:
            _dump_container(container_id, "HELLO-REJECT")
            raise
    cr._validate_inspection = _patched_validate

    _orig_ident = cr.DockerCliRuntimeAdapter.__dict__["_container_identity"]

    def _ident_probe(container_id: str, flag: str):
        try:
            return _orig_ident(container_id, flag)
        except Exception:
            _dump_container(container_id, "PROBE-FAIL")
            raw = _sp.run(["docker", "exec", container_id, "id", flag],
                          capture_output=True, text=True)
            print("RAW EXEC:", "rc=", raw.returncode,
                  "out=", raw.stdout.strip()[:80],
                  "err=", raw.stderr.strip()[:200])
            raise
    cr.DockerCliRuntimeAdapter._container_identity = staticmethod(_ident_probe)

    from dswarm.solver.runtime_credentials import CredentialProjector as _CP

    _orig_project = _CP.project

    def _project_debug(self, *, run_id, pool_id, worker_instance_id, binding_id, credential_mode):
        lease = _orig_project(self, run_id=run_id, pool_id=pool_id,
                              worker_instance_id=worker_instance_id,
                              binding_id=binding_id, credential_mode=credential_mode)
        env = getattr(lease, "env", {}) or {}
        print("PROJECTION:", "binding=", binding_id, "| mode=", credential_mode,
              "| env keys:", sorted(env.keys()),
              "| base_url env:", {k: v for k, v in env.items() if "URL" in k.upper()})
        return lease
    _CP.project = _project_debug

    from dswarm.solver.runtime_probe import RuntimeProbe as _RP

    _orig_classify = _RP.__dict__["_classify_result"]

    def _classify_debug(result):
        failure, health, meta = _orig_classify(result)
        rt = getattr(result, "runtime_status", {}) or {}
        print("PROBE RESULT:", "text=", str(getattr(result, "text", ""))[:260],
              "| rc=", rt.get("rc"), "| runtime=", {k: rt.get(k) for k in ("status", "timed_out")},
              "| failure=", getattr(failure, "code", failure))
        raw = getattr(result, "raw_stderr", None) or getattr(result, "raw_output", None)
        if raw:
            print("PROBE STDERR:", str(raw)[:300])
        return failure, health, meta
    _RP._classify_result = staticmethod(_classify_debug)

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
