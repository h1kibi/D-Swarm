# M9a Profile/Runtime Container Pools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the run-global Worker container with a Docker-first, per-run/per-profile-runtime pool manager that freezes runtime identity, probes each lazy pool through a real tool-disabled Pi turn, routes every real shell invocation through an isolated lease, and fails closed without touching the host Pi installation.

**Architecture:** Run creation builds one durable, secret-free `RuntimeSnapshot` containing canonical `PoolSpec` records keyed by `pool-v1::blake2b(...)`. A run-scoped `ContainerPoolManager` owns one long-lived RCP-v2 container generation per `(run_id, pool_id)`, single-flights startup and Probe, and hands each Worker a capacity-bound `WorkerRuntimeLease` with private HOME/session/workdir/credential/token state. Web and real TUI control planes become Docker-first; reopen performs a proof-based cleanup barrier before any new Probe or dispatch, while runtime diagnostics remain private sidecars and sanitized UI/API projections rather than evidence-graph events.

**Tech Stack:** Python 3.11+, frozen dataclasses, `asyncio`, stdlib JSON/`hashlib.blake2b`/`uuid`, Docker CLI and Compose, reverse-connect RCP JSON protocol, Go runtime-agent, append-only JSONL plus atomic fsync snapshots, FastAPI, Textual, pytest, Go tests, shell syntax checks, optional Docker integration tests.

**Spec:** `docs/superpowers/specs/2026-08-17-m9a-profile-runtime-container-pools-design.md`

## Global Constraints

- Preserve the hardcoded provenance gate, anti-laundering checks, first-valid-flag race, append-only SharedGraph, EventBus substrate, M5 canonical usage ledger, and M7/M8 offline-only boundaries.
- `RuntimePoolIdentity = (run_id, pool_id)` is run-scoped. Do not reuse pools across runs and do not create a second active container for one PoolKey in V1.
- Production defaults to `RuntimePolicy(mode="docker")`. Real host-local Workers require both `--local-dev` and `DSWARM_ALLOW_LOCAL_WORKERS=1`; Python callers must supply the same immutable policy. Never inspect `PYTEST_CURRENT_TEST` to bypass policy.
- `max_pools_per_run` defaults to 32 and accepts only `1..128`. A missing pool concurrency cap inherits `run.max_workers`.
- Pool IDs use `"pool-v1::" + blake2b(canonical_json(spec), digest_size=20).hexdigest()`; never use Python `hash()`.
- Snapshot path is `sessions/<run-id>/.runtime/pool-snapshot.v1.json`; write with temp file, `flush`, `os.fsync`, and atomic replace. `.runtime` is coordinator-private and is never mounted into Worker containers.
- Snapshots freeze immutable image ID, normalized network/resources, profile/runtime/model, binding identities, UID/GID, features/protocol, PoolKey, and limits. They never store secret bytes, task/control tokens, full account objects, host HOME, or unsanitized host paths.
- Every Worker container carries exact labels `com.dswarm.managed`, `com.dswarm.run_id`, `com.dswarm.pool_id`, `com.dswarm.pool_instance_id`, and `com.dswarm.generation`; cleanup validates inspected values rather than names.
- Gateway pools receive only independent per-worker M5 task tokens. Direct/custom pools receive only the selected binding projection. No Worker receives host HOME, `~/.pi`, Docker socket, another run, another pool credential, the full credential store, `.runtime`, or solution/reference files.
- All Worker images must contain `kali`; all images in one run must resolve to identical numeric UID/GID. Do not hardcode 1000 and do not use `chmod 777` as a repair.
- A pool is not `ready` until a real one-turn Pi Probe runs inside that exact long-lived container with tools explicitly disabled, no challenge/graph/provenance inputs, independent identity/state, and M5 accounting. HTTP/TCP/version checks do not satisfy readiness.
- Probe budget/accounting start failures are fail-closed with zero upstream calls. Unknown usage remains `None`, never zero. Probe output never enters evidence, solve-rate, M7, or M8.
- Global `WorkerLaneGate` remains authoritative; the pool semaphore is an additional bound. Cancellation and release must not leak permits.
- Infrastructure recovery is limited to one generation rebuild per failure episode. Identity/auth/configuration failures do not ambiently recover or repeat paid Probes.
- Docker/image/network/RCP/UID/credential/Probe/capacity failure never invokes host `pi`, never auto-falls back to local/legacy mode, and never becomes a fact/dead-end/finding.
- Process-level reopen runs a run-wide stale-runtime cleanup barrier before any Probe/cycle/dispatch; failure to prove cleanup rejects reopen.
- `RUN_FINISHED` does not own teardown. Delete, archive, server shutdown, TUI exit, or explicit dispose calls `await pool_manager.close()`.
- Runtime observability uses sanitized `WORKER_STATUS.runtime`, `PROVIDER_ERROR`, private state/diagnostic files, and `GET /api/runs/<run_id>/runtime-pools`; it adds no canonical graph runtime event and does not affect Reason prompts.
- Every task follows TDD and ends with a focused test plus `git diff --check`. Every M9a phase ends with `uv run pytest -q`. Docker-facing phases also run `bash -n ./run.sh`, `docker compose config`, and the explicitly gated integration suite.

## File and Interface Map

| File | Responsibility after M9a |
|---|---|
| `dswarm/solver/runtime_policy.py` | Immutable policy, normalized network/resources/pool models, canonical PoolKey construction, safe validation |
| `dswarm/solver/runtime_snapshot.py` | Docker image resolution/identity preflight, frozen snapshot builder/store, coordinator-private paths |
| `dswarm/solver/runtime_credentials.py` | One-binding, per-operation credential projection leases; no raw-store or host-env fallback |
| `dswarm/solver/control_receiver.py` | RCP v2 expected pool-instance identity, token/link routing and revocation |
| `cmd/runtime-agent/protocol.go`, `cmd/runtime-agent/main.go` | Emit and preserve RCP v2 Hello identity; multiplex independent Worker processes in one pool |
| `dswarm/solver/container_runtime.py` | One container generation: create, inspect, exec/stream/signal, terminate, exact labels/mounts/network |
| `dswarm/solver/container_pool.py` | Run-scoped manager, pool state machine, single-flight startup/Probe, semaphore leases, local failure/recovery |
| `dswarm/solver/runtime_probe.py` | Tool-disabled real Pi Probe, M5 identity/accounting/budget integration, timeout and cache semantics |
| `dswarm/solver/runtime_cleanup.py` | Exact-label cleanup proof, legacy evidence checks, run-wide reopen barrier |
| `dswarm/solver/runtime_diagnostics.py` | Sanitized private state/JSONL diagnostics and API-safe `RuntimePoolView` |
| `dswarm/solver/container_exec.py` | Temporary compatibility facade and shared low-level helpers only; no production run-global container ownership |
| `dswarm/swarm/swarm.py`, `worker_runtime_mixin.py`, `runtime.py`, `review_flow.py` | Inject policy/snapshot/manager and route every true Worker invocation through a lease |
| `apps/web/run_manager.py`, `apps/web/routes/btw.py`, `apps/web/routes/runtime_pools.py` | Own manager lifecycle/reopen barrier, share manager with BTW, expose read-only sanitized pool view |
| `run.sh`, `docker-compose.yml`, `docker/tui/Dockerfile` | Docker-first Web and real TUI control planes, explicit dual-gate local-dev escape hatch |
| `tests/integration/test_container_pools.py` | Opt-in fake-Pi/fake-provider real Docker coverage with no real key or billable endpoint |

---

## M9a-1 — RuntimePolicy, Models, Snapshot, and Credentials

### Task 1: Add immutable RuntimePolicy and the dual-gate local-development policy

**Files:**
- Create: `dswarm/solver/runtime_policy.py`
- Create: `tests/test_runtime_policy.py`

**Interfaces:**
- Produces: `RuntimePolicy`, `RuntimePolicyError`, and `build_runtime_policy(*, mode: str = "docker", local_dev_cli_flag: bool = False, env: Mapping[str, str] | None = None, max_pools_per_run: int = 32, pool_max_concurrent_workers_default: int | None = None, probe_timeout_seconds: float = 45.0, recovery_attempts_per_episode: int = 1) -> RuntimePolicy`.
- Consumed later by: snapshot creation, `Swarm`, `RunManager`, CLI/TUI/Web launch adapters, and every local/container execution decision.

- [ ] **Step 1: Write the failing policy tests**

```python
# tests/test_runtime_policy.py
import pytest
from dswarm.solver.runtime_policy import RuntimePolicyError, build_runtime_policy


def test_docker_is_the_default_and_policy_is_frozen():
    policy = build_runtime_policy(env={})
    assert policy.mode == "docker"
    assert policy.max_pools_per_run == 32
    with pytest.raises(AttributeError):
        policy.mode = "local_dev"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("flag", "allowed"),
    [(False, False), (True, False), (False, True)],
)
def test_local_dev_requires_both_explicit_gates(flag: bool, allowed: bool):
    env = {"DSWARM_ALLOW_LOCAL_WORKERS": "1"} if allowed else {}
    with pytest.raises(RuntimePolicyError, match="local_worker_policy_denied"):
        build_runtime_policy(mode="local_dev", local_dev_cli_flag=flag, env=env)


def test_local_dev_accepts_both_gates_without_pytest_ambient_bypass():
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1", "PYTEST_CURRENT_TEST": "must-not-matter"},
    )
    assert policy.local_workers_allowed is True


@pytest.mark.parametrize("value", [0, -1, 129])
def test_pool_cap_range_is_closed(value: int):
    with pytest.raises(RuntimePolicyError, match="invalid_max_pools_per_run"):
        build_runtime_policy(max_pools_per_run=value, env={})
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `uv run pytest -q tests/test_runtime_policy.py`

Expected: FAIL during collection with `ModuleNotFoundError: dswarm.solver.runtime_policy`.

- [ ] **Step 3: Implement the minimal immutable policy**

```python
# dswarm/solver/runtime_policy.py
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
        return self.mode == "local_dev" and self.local_dev_cli_flag and self.local_dev_env_allowed
```

`build_runtime_policy()` must normalize only documented truthy values (`1/true/yes/on`), reject unknown modes, validate finite positive Probe timeout, require recovery attempts exactly `1` for V1, and reject a non-positive explicit pool worker cap. It must never branch on pytest or test-process ambient state.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest -q tests/test_runtime_policy.py`

Expected: PASS.

- [ ] **Step 5: Commit M9a-1 policy**

```powershell
git add dswarm/solver/runtime_policy.py tests/test_runtime_policy.py
git commit -m "M9a-1 add immutable runtime policy"
```

### Task 2: Define canonical PoolSpec models and stable PoolKey hashing

**Files:**
- Modify: `dswarm/solver/runtime_policy.py`
- Modify: `tests/test_runtime_policy.py`

**Interfaces:**
- Consumes: `RuntimePolicy` from Task 1.
- Produces: frozen `RuntimeNetworkSpec`, `RuntimeResourceSpec`, `PoolSpec`, `RuntimeSnapshot`, `RuntimePoolIdentity = tuple[str, str]`, `canonical_pool_payload(spec: PoolSpec) -> bytes`, and `pool_id_for_spec(spec: PoolSpec) -> str`.

- [ ] **Step 1: Add failing canonicalization and validation tests**

```python
from dataclasses import replace
from dswarm.solver.runtime_policy import (
    PoolSpec, RuntimeNetworkSpec, RuntimeResourceSpec, pool_id_for_spec,
)


def pool_spec(**changes):
    base = PoolSpec(
        pool_id="",
        profile_id="pi-web",
        runtime_kind="pi",
        resolved_image_id="sha256:abc",
        requested_image_ref="ctf-swarm-pi-web:0.2.0",
        network=RuntimeNetworkSpec(kind="named", name="dswarm_net"),
        resources=RuntimeResourceSpec(cpus="2", memory="2g", pids_limit=256, tmpfs_bytes=67108864),
        credential_binding_id="pi-web-main",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2", "tool-disabled-probe"),
        protocol_version=2,
        pool_max_concurrent_workers=8,
    )
    return replace(base, **changes)


def test_pool_key_is_stable_and_excludes_secret_version_and_generation():
    first = pool_id_for_spec(pool_spec())
    assert first.startswith("pool-v1::")
    assert first == pool_id_for_spec(pool_spec())
    assert len(first.removeprefix("pool-v1::")) == 40


def test_binding_identity_changes_pool_key_but_secret_version_is_not_a_pool_field():
    assert pool_id_for_spec(pool_spec()) != pool_id_for_spec(
        pool_spec(credential_binding_id="pi-web-secondary")
    )
    assert "credential_version" not in PoolSpec.__dataclass_fields__


def test_named_network_requires_name_and_unknown_dict_fields_are_rejected():
    with pytest.raises(RuntimePolicyError, match="invalid_network"):
        RuntimeNetworkSpec(kind="named", name="")
    with pytest.raises(TypeError):
        PoolSpec(**{**pool_spec().__dict__, "raw_secret": "x"})
```

- [ ] **Step 2: Verify the tests fail before the models exist**

Run: `uv run pytest -q tests/test_runtime_policy.py`

Expected: FAIL with missing `PoolSpec`/`pool_id_for_spec` imports.

- [ ] **Step 3: Implement the frozen models and canonical JSON**

Use `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`, then `hashlib.blake2b(..., digest_size=20)`. Validate enum values, URL-safe bounded profile/binding/model identifiers, positive numeric UID/GID/protocol/limits, finite resource numbers, normalized tuple ordering, and an exact payload allowlist. Construct `PoolSpec.pool_id` only through `PoolSpec.with_computed_id(...)` or a builder so an externally supplied mismatched ID is rejected.

- [ ] **Step 4: Run the focused model tests**

Run: `uv run pytest -q tests/test_runtime_policy.py`

Expected: PASS, including deterministic output across repeated construction.

- [ ] **Step 5: Commit M9a-1 canonical models**

```powershell
git add dswarm/solver/runtime_policy.py tests/test_runtime_policy.py
git commit -m "M9a-1 define canonical runtime pool identities"
```

### Task 3: Resolve immutable image IDs and verify one shared Worker UID/GID

**Files:**
- Create: `dswarm/solver/runtime_snapshot.py`
- Create: `tests/test_runtime_snapshot.py`
- Modify: `dswarm/solver/container_exec.py`

**Interfaces:**
- Consumes: `RuntimePolicy`, normalized profile/runtime dictionaries, existing `docker_run`, `_mount_source`, and image-selection rules.
- Produces: `ResolvedWorkerImage`, `DockerImageInspector.resolve(image_ref: str) -> ResolvedWorkerImage`, `validate_shared_worker_identity(images: Sequence[ResolvedWorkerImage]) -> tuple[int, int]`, and `RuntimeSnapshotBuildError(code: str, safe_detail: str)`.

- [ ] **Step 1: Write failing fake-Docker preflight tests**

```python
# tests/test_runtime_snapshot.py
class FakeDocker:
    def __init__(self):
        self.calls = []
    def resolve_image(self, ref):
        self.calls.append(("resolve", ref))
        return {"image_id": "sha256:" + ref[-1] * 8}
    def query_user(self, image_id, user, *, network, mounts, env):
        self.calls.append(("identity", image_id, user, network, mounts, env))
        return 1000, 1000


def test_identity_probe_has_no_network_mount_or_secret():
    docker = FakeDocker()
    image = DockerImageInspector(docker).resolve("worker:a")
    assert (image.uid, image.gid) == (1000, 1000)
    identity_call = docker.calls[-1]
    assert identity_call[3:] == ("none", (), {})


def test_all_run_images_must_have_the_same_numeric_identity():
    images = [
        ResolvedWorkerImage("a", "sha256:a", 1000, 1000),
        ResolvedWorkerImage("b", "sha256:b", 1001, 1000),
    ]
    with pytest.raises(RuntimeSnapshotBuildError) as exc:
        validate_shared_worker_identity(images)
    assert exc.value.code == "worker_identity_mismatch"
```

Add tests that image inspect/pull failure is structured, missing `kali` is rejected, image references resolve once per unique tag, and the identity query does not create a long-lived container or provider request.

- [ ] **Step 2: Run the failing snapshot tests**

Run: `uv run pytest -q tests/test_runtime_snapshot.py`

Expected: FAIL with missing runtime snapshot interfaces.

- [ ] **Step 3: Implement injectable image resolution and remove unsafe identity fallback**

`DockerImageInspector` must parse the immutable `.Id` returned by `docker image inspect`; if policy permits pull, pull once then inspect again. The identity probe must run `id -u kali && id -g kali` under `--network none`, with no bind mounts and an empty secret environment. Do not use `_fallback_worker_uid_gid()` for M9a snapshot construction: inability to prove identity is `worker_identity_mismatch`. Keep legacy helper behavior only behind the compatibility facade until Task 23 removes production callers.

- [ ] **Step 4: Run snapshot and legacy container tests**

Run: `uv run pytest -q tests/test_runtime_snapshot.py tests/test_container_exec.py`

Expected: PASS; existing compatibility tests remain green.

- [ ] **Step 5: Commit M9a-1 image identity preflight**

```powershell
git add dswarm/solver/runtime_snapshot.py dswarm/solver/container_exec.py tests/test_runtime_snapshot.py
git commit -m "M9a-1 add immutable image identity preflight"
```

### Task 4: Build and durably persist the frozen runtime snapshot

**Files:**
- Modify: `dswarm/solver/runtime_snapshot.py`
- Modify: `tests/test_runtime_snapshot.py`
- Modify: `dswarm/solver/worker_profiles.py`

**Interfaces:**
- Consumes: normalized Worker profiles, runtime profiles, image resolver, `run.max_workers`, and Task 2 models.
- Produces: `RuntimeSnapshotBuilder.build(*, run_id: str, policy: RuntimePolicy, worker_profiles: Sequence[Mapping[str, Any]], runtime_profiles: Sequence[Mapping[str, Any]], run_max_workers: int) -> RuntimeSnapshot`; `RuntimeSnapshotStore(root: Path).create(snapshot)`, `.load(run_id)`, `.path_for(run_id)`.

- [ ] **Step 1: Add failing snapshot-builder and durability tests**

```python
def test_snapshot_freezes_image_id_pool_limit_and_binding_identity(tmp_path):
    snapshot = builder(tmp_path).build(
        run_id="run-1",
        policy=build_runtime_policy(env={}),
        worker_profiles=[web_profile, pwn_profile],
        runtime_profiles=[web_runtime, pwn_runtime],
        run_max_workers=6,
    )
    assert len(snapshot.pools) == 2
    assert {p.pool_max_concurrent_workers for p in snapshot.pools} == {6}
    assert all(p.resolved_image_id.startswith("sha256:") for p in snapshot.pools)
    serialized = RuntimeSnapshotStore(tmp_path).create(snapshot).read_text("utf-8")
    for forbidden in ("API_KEY", "secret", str(Path.home()), ".pi"):
        assert forbidden not in serialized


def test_snapshot_is_create_once_and_tag_drift_does_not_rewrite_existing_run(tmp_path):
    store = RuntimeSnapshotStore(tmp_path)
    original = builder(tmp_path, image_id="sha256:old").build_for_test("run-1")
    store.create(original)
    with pytest.raises(RuntimeSnapshotBuildError, match="snapshot_already_exists"):
        store.create(builder(tmp_path, image_id="sha256:new").build_for_test("run-1"))
    assert store.load("run-1").pools[0].resolved_image_id == "sha256:old"
```

Also test max pool cap, duplicate `pool_id`/profile mapping rejection, stable ordering, atomic replace failure preserving the old file, `fsync` call, no `.runtime` path in mount specs, and credential version absence.

- [ ] **Step 2: Verify the new tests fail**

Run: `uv run pytest -q tests/test_runtime_snapshot.py`

Expected: FAIL because builder/store are not implemented.

- [ ] **Step 3: Implement builder/store and normalize runtime fields**

Add exact normalization for network (`none`, `bridge`, `host`, or named), resources (`cpus`, `memory`, `pids_limit`, `tmpfs_bytes`), runtime feature set, protocol version `2`, profile/model/provider/credential binding identities, and pool capacity. The builder must reject more than `policy.max_pools_per_run`, verify one UID/GID pair across all images, sort pools by `(profile_id, pool_id)`, and never read secret bytes. The store must write only under `sessions_root / safe_run_id / ".runtime"`, reject path traversal, create mode-restricted directories, and use temp → flush → fsync → `os.replace` with best-effort parent-directory fsync.

- [ ] **Step 4: Run snapshot/profile tests**

Run: `uv run pytest -q tests/test_runtime_snapshot.py tests/test_worker_config.py tests/test_web_server.py -k 'profile or runtime or snapshot'`

Expected: PASS.

- [ ] **Step 5: Commit M9a-1 frozen snapshot**

```powershell
git add dswarm/solver/runtime_snapshot.py dswarm/solver/worker_profiles.py tests/test_runtime_snapshot.py
git commit -m "M9a-1 persist frozen runtime snapshots"
```

### Task 5: Replace whole-account projection with one-binding operation leases

**Files:**
- Create: `dswarm/solver/runtime_credentials.py`
- Modify: `dswarm/solver/credential_accounts.py`
- Create: `tests/test_runtime_credentials.py`
- Modify: `tests/test_credential_accounts.py`

**Interfaces:**
- Consumes: frozen `PoolSpec.credential_binding_id`, `CredentialAccountStore`, provider secret store, and M5 task-token issuer.
- Produces: `CredentialProjectionLease`, `CredentialProjector.project(*, run_id: str, pool_id: str, worker_instance_id: str, binding_id: str, credential_mode: Literal["gateway", "direct", "custom"]) -> CredentialProjectionLease`, and idempotent `CredentialProjectionLease.close()`.

- [ ] **Step 1: Write failing minimal-projection tests**

```python
# tests/test_runtime_credentials.py
def test_direct_projection_contains_only_selected_binding(tmp_path):
    store = make_accounts(tmp_path, {"pi-web-main": "web-key", "pi-pwn-main": "pwn-key"})
    lease = CredentialProjector(store.root, tmp_path / "private").project(
        run_id="r", pool_id="pool-web", worker_instance_id="worker-1",
        binding_id="pi-web-main", credential_mode="direct",
    )
    assert sorted(p.name for p in lease.root.iterdir()) == ["pi-web-main"]
    assert "web-key" in (lease.root / "pi-web-main" / "API_KEY").read_text()
    assert "pwn-key" not in "".join(p.read_text() for p in lease.root.rglob("*") if p.is_file())


def test_missing_binding_never_falls_back_to_env_or_another_account(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "host-key")
    with pytest.raises(CredentialProjectionError) as exc:
        CredentialProjector(empty_store, tmp_path / "private").project(
            run_id="r", pool_id="p", worker_instance_id="w",
            binding_id="deleted", credential_mode="direct",
        )
    assert exc.value.code == "credential_binding_unavailable"
    assert "host-key" not in repr(exc.value)


def test_gateway_projection_has_no_provider_secret_files(tmp_path):
    lease = projector(tmp_path).project(
        run_id="r", pool_id="p", worker_instance_id="w",
        binding_id="pi-main", credential_mode="gateway",
    )
    assert lease.root is None
    assert lease.env == {}
```

Add tests for `0600` secret files, non-enumerability, per-worker unique roots, idempotent cleanup, cleanup failure classification, no host HOME/raw store path, and secret/version digest changing without changing PoolKey.

- [ ] **Step 2: Run and observe failure**

Run: `uv run pytest -q tests/test_runtime_credentials.py tests/test_credential_accounts.py`

Expected: FAIL because `CredentialProjector` does not exist and current `project_account_root()` copies all accounts.

- [ ] **Step 3: Implement the projector and quarantine broad projection**

The projector copies only the selected account/provider binding into `sessions/<run-id>/.runtime/pools/<pool-id>/workers/<worker-instance-id>/credentials`, returns only container-relative env/file references, records a non-secret `credential_version_digest`, and deletes the operation root on `close()`. Gateway mode creates no provider projection. Direct/custom mode rejects missing/invalid bindings and never calls `_add_secret_file_or_env(..., source=os.environ)` as a fallback. Keep `project_account_root()` only for explicit legacy tests until Task 23; add a warning/guard so new production code cannot import it.

- [ ] **Step 4: Run credential tests**

Run: `uv run pytest -q tests/test_runtime_credentials.py tests/test_credential_accounts.py`

Expected: PASS.

- [ ] **Step 5: Run M9a-1 phase gate and commit**

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: both exit 0.

```powershell
git add dswarm/solver/runtime_credentials.py dswarm/solver/credential_accounts.py tests/test_runtime_credentials.py tests/test_credential_accounts.py
git commit -m "M9a-1 isolate per-binding worker credentials"
```

---

## M9a-2 — RCP Pool-Instance Identity

### Task 6: Route Python RCP links by ExpectedRuntimeIdentity

**Files:**
- Modify: `dswarm/solver/control_receiver.py`
- Modify: `tests/test_control_client.py`

**Interfaces:**
- Consumes: `pool_id`, UUID4 `pool_instance_id`, generation, immutable image ID, protocol version `2`.
- Produces: frozen `ExpectedRuntimeIdentity`; `ControlReceiver.issue_pool(expected_identity) -> str`; `wait_pool(pool_instance_id, timeout) -> _SupervisorLink`; `link_for(pool_instance_id) -> _SupervisorLink | None`; `revoke_pool_instance(pool_instance_id)`, `revoke_pool(pool_id)`, and `revoke_run(run_id)`.

- [ ] **Step 1: Replace run-only test fixtures with RCP-v2 identity tests**

```python
# tests/test_control_client.py
def identity(run="r", pool="pool-a", instance="11111111-1111-4111-8111-111111111111", generation=1):
    return ExpectedRuntimeIdentity(
        run_id=run, pool_id=pool, pool_instance_id=instance,
        generation=generation, expected_image_id="sha256:abc", protocol_version=2,
    )


def test_same_run_can_hold_two_independent_pool_links(receiver):
    token_a = receiver.issue_pool(identity(pool="pool-a", instance=UUID_A))
    token_b = receiver.issue_pool(identity(pool="pool-b", instance=UUID_B))
    link_a = complete_hello(receiver, identity(pool="pool-a", instance=UUID_A), token_a)
    link_b = complete_hello(receiver, identity(pool="pool-b", instance=UUID_B), token_b)
    assert receiver.link_for(UUID_A) is link_a
    assert receiver.link_for(UUID_B) is link_b


def test_stale_generation_or_wrong_pool_cannot_replace_live_link(receiver):
    token = receiver.issue_pool(identity(instance=UUID_A, generation=2))
    ok, error = raw_hello(receiver, {
        "protocol_version": 2, "run_id": "r", "pool_id": "pool-a",
        "pool_instance_id": UUID_A, "generation": 1, "token": token,
    })
    assert ok is False
    assert error == "runtime_identity_mismatch"
    assert receiver.link_for(UUID_A) is None
```

Add tests for every mismatched field, duplicate live link rejection without closing the original, pool-local revoke, run-wide revoke, waiter shutdown failure, sanitized diagnostics, and explicit v1 rejection outside legacy mode.

- [ ] **Step 2: Run RCP tests and verify run-keyed implementation fails**

Run: `uv run pytest -q tests/test_control_client.py`

Expected: FAIL because `ExpectedRuntimeIdentity` and per-instance APIs do not exist.

- [ ] **Step 3: Implement RCP-v2 maps and handshake validation**

Replace `_tokens: dict[run_id, token]` and `_links: dict[run_id, link]` with identity/token/link maps keyed by `pool_instance_id`, plus `run_id -> set[pool_instance_id]` and `(run_id, pool_id) -> current pool_instance_id`. Parse only bounded strings and positive generation; compare protocol/run/pool/instance/generation before acknowledging. A failed Hello must not install/replace a link. Receiver shutdown wakes all waiters with `ControlError("control_receiver_stopped")`.

Keep `expect()/await_link()/get_link()/forget()` only in an explicitly named legacy adapter used by compatibility tests, not by new pool code.

- [ ] **Step 4: Run RCP tests**

Run: `uv run pytest -q tests/test_control_client.py`

Expected: PASS.

- [ ] **Step 5: Commit M9a-2 Python receiver**

```powershell
git add dswarm/solver/control_receiver.py tests/test_control_client.py
git commit -m "M9a-2 route RCP links by pool instance"
```

### Task 7: Emit RCP-v2 pool identity from the Go runtime-agent

**Files:**
- Modify: `cmd/runtime-agent/protocol.go`
- Modify: `cmd/runtime-agent/main.go`
- Modify: `cmd/runtime-agent/agent_test.go`
- Modify: `docker/worker-kali/entrypoint.sh`
- Modify: `tests/test_control_client.py`

**Interfaces:**
- Consumes: `ExpectedRuntimeIdentity` and the token issued by Task 6.
- Produces: Go `Hello{ProtocolVersion, RunID, PoolID, PoolInstanceID, Generation, Token, Version}` and required process environment `DSWARM_RUN_ID`, `DSWARM_POOL_ID`, `DSWARM_POOL_INSTANCE_ID`, `DSWARM_POOL_GENERATION`, `DSWARM_CONTROL_TOKEN[_FILE]`.

- [ ] **Step 1: Write failing Go and Python wire-contract tests**

```go
// cmd/runtime-agent/agent_test.go
func TestHelloV2CarriesPoolInstanceIdentity(t *testing.T) {
    got := Hello{
        ProtocolVersion: 2,
        RunID: "run-a",
        PoolID: "pool-v1::abc",
        PoolInstanceID: "11111111-1111-4111-8111-111111111111",
        Generation: 3,
        Token: "opaque",
    }
    raw, err := json.Marshal(got)
    if err != nil { t.Fatal(err) }
    var wire map[string]any
    if err := json.Unmarshal(raw, &wire); err != nil { t.Fatal(err) }
    if wire["protocol_version"] != float64(2) || wire["generation"] != float64(3) {
        t.Fatalf("bad hello: %s", raw)
    }
    if wire["pool_id"] != "pool-v1::abc" || wire["pool_instance_id"] == "" {
        t.Fatalf("missing pool identity: %s", raw)
    }
}

func TestSupervisorRejectsMissingV2IdentityBeforeDial(t *testing.T) {
    env := map[string]string{"DSWARM_RUN_ID": "run-a"}
    if _, err := supervisorIdentityFromEnv(env); err == nil {
        t.Fatal("expected missing pool identity error")
    }
}
```

Add a Python integration fixture that captures the Go Hello and passes it through the real `ControlReceiver`; assert the receiver installs the link only when all v2 fields match. Add cases for malformed UUID, zero/negative generation, protocol `1`, and a stale generation reconnect.

- [ ] **Step 2: Run the protocol tests and observe failure**

Run: `go test ./cmd/runtime-agent/...`

Run: `uv run pytest -q tests/test_control_client.py -k 'go_hello or protocol_v2'`

Expected: FAIL because the Go Hello has only `{hello, run_id, token, version}` and the new environment parser does not exist.

- [ ] **Step 3: Implement the exact v2 Hello and strict startup validation**

Replace the numeric `hello: 1` marker with `protocol_version: 2`; parse and validate every identity field before dialing. `pool_instance_id` must be a canonical UUID string and `generation` must be a positive base-10 integer. The entrypoint must pass values unchanged and must never print them. A missing/malformed field exits non-zero before a network connection is attempted. Keep the worker multiplexing protocol unchanged after HelloAck.

The Python receiver test helper must launch the compiled/runtime test agent with explicit pool env; no production compatibility alias may synthesize pool identity from `run_id`.

- [ ] **Step 4: Run both protocol suites**

Run: `go test ./cmd/runtime-agent/...`

Run: `uv run pytest -q tests/test_control_client.py`

Expected: PASS.

- [ ] **Step 5: Run the M9a-2 phase gate and commit**

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: both exit 0.

```powershell
git add cmd/runtime-agent/protocol.go cmd/runtime-agent/main.go cmd/runtime-agent/agent_test.go docker/worker-kali/entrypoint.sh tests/test_control_client.py
git commit -m "M9a-2 add RCP v2 pool instance hello"
```

---

## M9a-3 — Container Runtime, Pool Manager, Probe, and Cleanup Primitives

### Task 8: Extract one-generation ContainerRuntimeExecutor and sanitized exec records

**Files:**
- Create: `dswarm/solver/container_runtime.py`
- Create: `tests/test_container_runtime.py`
- Modify: `dswarm/solver/container_exec.py`
- Modify: `tests/test_container_exec.py`

**Interfaces:**
- Consumes: `PoolSpec`, `ExpectedRuntimeIdentity`, `ControlReceiver`, Docker CLI adapter, the existing RCP `run_cli_rcp`/`run_cli_streaming_rcp` functions, and a per-operation credential projection.
- Produces: frozen `ContainerGenerationIdentity`; `RuntimeExecRecord`; `ContainerRuntimeExecutor.create(...)`; `executor.run(...)`; `executor.run_streaming(...)`; `executor.signal(worker_id, signal)`; `executor.status(worker_id)`; `executor.terminate(*, require_proof: bool) -> RuntimeTerminationReport`; `executor.to_container_path(host_path)`.

- [ ] **Step 1: Write failing executor tests against a fake Docker/RCP adapter**

```python
# tests/test_container_runtime.py
@pytest.mark.asyncio
async def test_create_uses_exact_snapshot_identity_labels_and_mount_allowlist(tmp_path):
    docker = FakeDocker()
    receiver = FakeReceiver()
    executor = await ContainerRuntimeExecutor.create(
        run_id="run-a", pool_spec=pool_spec(), generation=1,
        run_root=tmp_path / "run-a", docker=docker, receiver=receiver,
    )
    create = docker.create_calls[0]
    assert create.image == "sha256:immutable"
    assert create.labels == {
        "com.dswarm.managed": "true",
        "com.dswarm.run_id": "run-a",
        "com.dswarm.pool_id": pool_spec().pool_id,
        "com.dswarm.pool_instance_id": executor.pool_instance_id,
        "com.dswarm.generation": "1",
    }
    assert all("docker.sock" not in m.source for m in create.mounts)
    assert all("/.runtime" not in m.source.replace("\\", "/") for m in create.mounts)


@pytest.mark.asyncio
async def test_exec_record_never_exposes_argv_env_token_or_host_path(tmp_path):
    executor = await ready_executor(tmp_path)
    result = await executor.run(
        driver=FakeDriver(), argv=["pi", "secret prompt"], host_cwd=tmp_path,
        timeout=5, env={"DEEPSEEK_API_KEY": "secret", "SAFE": "x"},
        worker_instance_id="worker-1", operation_kind="worker",
    )
    wire = json.dumps(result.runtime_status, sort_keys=True)
    assert "secret prompt" not in wire
    assert "DEEPSEEK_API_KEY" not in wire
    assert str(tmp_path) not in wire
    assert result.runtime_status["pool_instance_id"] == executor.pool_instance_id
```

Add deterministic cases for immutable image use, named-network validation, resource flags, numeric UID/GID, exact mount targets, failed Hello cleanup, stale-link rejection, signal/status routing, concurrent independent worker IDs, and `terminate()` proof failure.

- [ ] **Step 2: Run the tests and verify extraction is absent**

Run: `uv run pytest -q tests/test_container_runtime.py tests/test_container_exec.py`

Expected: FAIL because `container_runtime.py` and the generation-scoped executor do not exist.

- [ ] **Step 3: Implement a generation-scoped executor**

Move only reusable Docker/RCP mechanics from `container_exec.py`; do not carry over run-global registries, `_RUN_PREFIX` ownership, ambient image lookup, or local fallback. Container names may include a sanitized short run/pool/instance suffix for operator readability, but labels are authoritative. The executor must:

```python
@dataclass(frozen=True)
class ContainerGenerationIdentity:
    run_id: str
    pool_id: str
    pool_instance_id: str
    generation: int
    resolved_image_id: str
```

Create the control token through `ControlReceiver.issue_pool()`, start exactly the frozen image ID, inspect labels/image/mounts/network/UID/GID after create, await the matching pool-instance link, and remove/revoke on any startup error. Convert argv/cwd only from allowlisted mounted roots. `RuntimeExecRecord.snapshot()` exposes IDs, driver name, operation kind, status/timing/rc/timeout/OOM/cancel flags, and bounded sanitized error code; it excludes argv beyond `argv0`, prompt text, env values, tokens, container host paths, and raw stderr.

`container_exec.py` continues exporting its old functions for tests only, implemented as a named compatibility facade around old behavior until Task 23; no new module imports those facades.

- [ ] **Step 4: Run executor and compatibility tests**

Run: `uv run pytest -q tests/test_container_runtime.py tests/test_container_exec.py tests/test_container_oom.py`

Expected: PASS.

- [ ] **Step 5: Commit the one-generation executor**

```powershell
git add dswarm/solver/container_runtime.py dswarm/solver/container_exec.py tests/test_container_runtime.py tests/test_container_exec.py
git commit -m "M9a-3 extract generation scoped container runtime"
```

### Task 9: Implement ContainerPoolManager state machine, single-flight, capacity, and leases

**Files:**
- Create: `dswarm/solver/container_pool.py`
- Create: `tests/test_container_pool.py`

**Interfaces:**
- Consumes: `RuntimePolicy`, `RuntimeSnapshot`, `ContainerRuntimeExecutor`, and `CredentialProjector`.
- Produces: `RuntimeFailure`; `RuntimeProbeResult`; `RuntimeProbeProtocol`; `RuntimePoolView`; `PoolCloseReport`; `WorkerRuntimeLease`; `ContainerPoolManager.acquire(...)`; `mark_failure(...)`; `close()`; `snapshot_view()`.

- [ ] **Step 1: Write failing state-machine and concurrency tests**

```python
# tests/test_container_pool.py
@pytest.mark.asyncio
async def test_concurrent_first_acquire_singleflights_create_and_probe(snapshot):
    factory = FakeExecutorFactory()
    probe = FakeProbe()
    manager = ContainerPoolManager(
        run_id="run-a", snapshot=snapshot, executor_factory=factory,
        probe=probe, credential_projector=FakeProjector(),
    )
    leases = await asyncio.gather(*[
        manager.acquire(pool_id=POOL_ID, worker_instance_id=f"w-{i}", operation_kind="worker")
        for i in range(4)
    ])
    assert factory.create_count == 1
    assert probe.calls == 1
    assert {x.pool_instance_id for x in leases} == {factory.executor.pool_instance_id}
    await asyncio.gather(*(x.release() for x in leases))


@pytest.mark.asyncio
async def test_cancelled_capacity_waiter_does_not_leak_permit(snapshot):
    manager = manager_with_capacity(snapshot, capacity=1)
    first = await manager.acquire(pool_id=POOL_ID, worker_instance_id="w1", operation_kind="worker")
    waiter = asyncio.create_task(manager.acquire(
        pool_id=POOL_ID, worker_instance_id="w2", operation_kind="worker"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await first.release()
    third = await asyncio.wait_for(manager.acquire(
        pool_id=POOL_ID, worker_instance_id="w3", operation_kind="worker"), 1)
    await third.release()
```

Add tests for every legal transition; illegal transition rejection; max-pools cap; inherited worker capacity; idempotent release; manager-close rejection; close during startup/probe/acquire; pool-local degradation; pending waiter structured failure; one infrastructure recovery per episode; no recovery for identity/auth/configuration; and inability to prove old generation stopped preventing replacement.

- [ ] **Step 2: Run and observe missing manager failures**

Run: `uv run pytest -q tests/test_container_pool.py`

Expected: FAIL during import.

- [ ] **Step 3: Implement the run-scoped manager and lease ownership**

Use one `ContainerPoolEntry` per snapshot pool. Each entry owns an `asyncio.Lock`, one startup future, one semaphore, state/reason, generation counter, current executor, Probe cache key, active/waiting counters, and recovery-episode counter. Transitions are performed only under the entry lock. Startup work may run outside the lock through the shared future, but completion re-enters the lock and checks that manager/entry generation is still current.

```python
@dataclass(frozen=True)
class RuntimeProbeResult:
    ready: bool
    probe_id: str
    failure: RuntimeFailure | None
    cache_identity: str


class RuntimeProbeProtocol(Protocol):
    async def run(self, *, executor: ContainerRuntimeExecutor, pool_spec: PoolSpec,
                      credential_projection: CredentialProjectionLease,
                      generation: int, timeout: float) -> RuntimeProbeResult: ...


@dataclass
class WorkerRuntimeLease:
    pool_id: str
    pool_instance_id: str
    generation: int
    worker_instance_id: str
    executor: ContainerRuntimeExecutor
    credential_projection: CredentialProjectionLease
    worker_env: Mapping[str, str]
    _release_once: Callable[[], Awaitable[None]]

    async def release(self) -> None: ...
```

Acquire validates that `pool_id` exists in the frozen snapshot, reserves semaphore capacity cancellation-safely, single-flights startup+Probe, creates the per-operation credential projection only after a trustworthy generation exists, and returns only from `ready`. Every failure releases acquired resources in reverse order. `close()` is idempotent, marks all entries stopping, wakes waiters, terminates every executor even if another fails, closes projections, revokes tokens, and returns all sanitized residual failures.

- [ ] **Step 4: Run manager tests**

Run: `uv run pytest -q tests/test_container_pool.py`

Expected: PASS.

- [ ] **Step 5: Commit the pool manager**

```powershell
git add dswarm/solver/container_pool.py tests/test_container_pool.py
git commit -m "M9a-3 add run scoped container pool manager"
```

### Task 10: Add the public tool-disabled CliDriver Probe contract

**Files:**
- Modify: `dswarm/solver/cli_driver.py`
- Create: `tests/test_runtime_probe_driver.py`
- Modify: `tests/test_worker_endpoint.py`

**Interfaces:**
- Consumes: Pi JSON mode and existing `CliResult` parsing.
- Produces: frozen `CliProbeSpec`; `CliDriver.probe_spec(*, model: str, session_dir: str) -> CliProbeSpec`; `CliDriver.parse_probe_result(stdout: str, stderr: str, returncode: int) -> CliProbeResult`.

- [ ] **Step 1: Write failing tests proving the Probe cannot use tools**

```python
# tests/test_runtime_probe_driver.py
def test_pi_probe_spec_is_real_one_turn_and_explicitly_disables_every_builtin_tool():
    spec = PiDriver().probe_spec(model="deepseek-chat", session_dir="/private/probe/session")
    assert spec.argv[:3] == ("pi", "--mode", "json")
    assert spec.prompt == "Reply with exactly: OK"
    assert spec.non_agentic is True
    denied = set(spec.disabled_tools)
    assert {"read", "bash", "edit", "write", "grep", "find", "ls"} <= denied
    assert "--exclude-tools" in spec.argv
    assert spec.requires_closed_stdin is True


def test_driver_without_a_provable_tool_disabled_mode_is_rejected():
    with pytest.raises(ProbeContractError, match="tool_disabled_unprovable"):
        UnsafeDriver().probe_spec(model="m", session_dir="/tmp/s")
```

Add tests that `probe_spec()` never calls host `resolve_engine_bin()` when supplied a container binary name, always uses an independent session dir, disallows web/KB/MCP tools, emits one prompt only, validates model, and classifies completed/auth/model/config/timeout/transport/empty-reply outcomes without returning raw model text in diagnostics.

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest -q tests/test_runtime_probe_driver.py tests/test_worker_endpoint.py`

Expected: FAIL because only private `_hello_argv()`/host `health_detail()` exist and they can fall back to `--version`.

- [ ] **Step 3: Implement a container-safe public Probe contract**

`CliProbeSpec.argv` starts with the logical in-container binary `pi`, not `self.bin`; this prevents host binary resolution. For Pi, construct exactly one JSON-mode invocation with an explicit `--exclude-tools` list covering every current built-in and all configured Web/KB/MCP tool prefixes. Set `non_agentic=True`, closed stdin, bounded output bytes, and no resume/session reuse. `parse_probe_result()` accepts only a completed model-turn event and returns usage fields separately from a bounded error code/classification. Do not make `health_detail()` satisfy M9a readiness; it remains a UI health helper.

- [ ] **Step 4: Run driver Probe tests**

Run: `uv run pytest -q tests/test_runtime_probe_driver.py tests/test_worker_endpoint.py`

Expected: PASS.

- [ ] **Step 5: Commit the Probe driver contract**

```powershell
git add dswarm/solver/cli_driver.py tests/test_runtime_probe_driver.py tests/test_worker_endpoint.py
git commit -m "M9a-3 define tool disabled runtime probe contract"
```

### Task 11: Implement RuntimeProbe with M5 accounting, budget gating, cache, and recovery classification

**Files:**
- Create: `dswarm/solver/runtime_probe.py`
- Create: `tests/test_runtime_probe.py`
- Modify: `dswarm/core/usage_journal.py`
- Modify: `tests/test_phase5_ledger.py`

**Interfaces:**
- Consumes: `RuntimeProbeProtocol` and `RuntimeProbeResult` from Task 9, plus `CliProbeSpec`, `ContainerRuntimeExecutor`, `UsageWriter`, `UsageContext`, `ProfileBudgetGate`, `SpawnGuard`, frozen pool/binding identity, and credential version digest.
- Produces: concrete `RuntimeProbe.run(*, executor, pool_spec, credential_projection, generation, timeout) -> RuntimeProbeResult`; `RuntimeProbeCacheKey`; `ProbeFailureClass = Literal["infrastructure", "identity", "auth", "configuration", "capacity"]`.

- [ ] **Step 1: Write failing Probe lifecycle/accounting tests**

```python
# tests/test_runtime_probe.py
@pytest.mark.asyncio
async def test_probe_is_accounted_before_upstream_and_never_receives_challenge_inputs(tmp_path):
    calls = []
    writer = OrderedUsageWriter(calls)
    executor = FakeExecutor(calls, reply=measured_probe_reply())
    result = await RuntimeProbe(usage_writer=writer, budget_gate=AllowBudget()).run(
        executor=executor, pool_spec=pool_spec(),
        credential_projection=gateway_projection(), generation=1, timeout=5,
    )
    assert calls[:2] == ["usage_started", "provider_request"]
    assert result.ready is True
    env_and_argv = executor.last_request
    serialized = json.dumps(env_and_argv)
    for forbidden in ("DSWARM_BLACKBOARD_DB", "challenge", "target", "player_files", "FOUND_FLAG"):
        assert forbidden not in serialized
    assert writer.finished.operation_kind == "runtime_probe"


@pytest.mark.asyncio
async def test_accounting_start_failure_makes_zero_upstream_calls():
    executor = FakeExecutor()
    with pytest.raises(RuntimeProbeError, match="accounting_unavailable"):
        await RuntimeProbe(usage_writer=FailingStartWriter(), budget_gate=AllowBudget()).run(
            executor=executor, pool_spec=pool_spec(),
            credential_projection=gateway_projection(), generation=1, timeout=5,
        )
    assert executor.calls == []
```

Add tests for profile/account budget rejection before provider call; unknown usage stored as `None`; probe IDs and worker-instance/task tokens distinct from Workers; no graph/EventBus/M7/M8 writes; timeout process-group cleanup; cleanup-unproven invalidating generation; cache hit and invalidation by credential version/image/model/generation; concurrent waiters sharing one paid Probe; auth/model/config no retry; infrastructure one rebuild maximum.

- [ ] **Step 2: Run the tests and observe missing Probe behavior**

Run: `uv run pytest -q tests/test_runtime_probe.py tests/test_phase5_ledger.py -k 'probe or runtime_probe'`

Expected: FAIL because `RuntimeProbe` and `operation_kind="runtime_probe"` context construction do not exist.

- [ ] **Step 3: Implement ordered accounting and Probe classification**

Construct a UUID4 `probe_id`, separate UUID4 worker instance, private HOME/session/workdir below the pool private worker root, and an independent gateway task token or selected direct projection. Call the M5 budget/spawn guard and `UsageWriter.start()` before `executor.run()`; a start failure is fail-closed. In `finally`, write exactly one terminal usage outcome (`succeeded`, `provider_error`, `transport_error`, `timeout`, `cancelled`, or `interrupted`) with `measured|estimated|unknown`; unknown numeric fields remain `None`.

Cache key fields are exactly `(pool_id, pool_instance_id, generation, resolved_image_id, model, provider_binding_id, credential_binding_id, credential_version_digest, probe_contract_version)`. Cache success only; never cache failure. Parse provider errors into the frozen failure classes without raw responses or credentials. Probe output is discarded after parsing and never sent to `EventBus`, SharedGraph, artifacts, solve-rate, M7, or M8.

- [ ] **Step 4: Run Probe and M5 regression tests**

Run: `uv run pytest -q tests/test_runtime_probe.py tests/test_phase5_ledger.py tests/test_usage_journal.py`

Expected: PASS.

- [ ] **Step 5: Commit RuntimeProbe**

```powershell
git add dswarm/solver/runtime_probe.py dswarm/core/usage_journal.py tests/test_runtime_probe.py tests/test_phase5_ledger.py
git commit -m "M9a-3 add accounted runtime pool probe"
```

### Task 12: Add exact cleanup inspection and generation termination proofs

**Files:**
- Create: `dswarm/solver/runtime_cleanup.py`
- Create: `tests/test_runtime_cleanup.py`
- Modify: `dswarm/solver/container_runtime.py`
- Modify: `tests/test_container_runtime.py`

**Interfaces:**
- Consumes: Docker inspect/list/remove adapter, runtime labels, snapshot/private-state identities, `ControlReceiver`, ModelGateway worker-token revocation hook.
- Produces: `RuntimeCleanupInspector.inspect_candidate(...)`; `cleanup_pool_generation(...) -> RuntimeCleanupResult`; `RuntimeTerminationProof`; reusable exact-match predicates for Task 22's run-wide barrier.

- [ ] **Step 1: Write failing exact-proof tests**

```python
# tests/test_runtime_cleanup.py
def test_managed_container_requires_every_exact_label_and_private_state_match():
    inspected = managed_inspect(run_id="run-a", pool_id="pool-a", instance=UUID_A, generation=2)
    verdict = RuntimeCleanupInspector().inspect_candidate(
        inspected, expected=expected_state(run_id="run-a", pool_id="pool-a", instance=UUID_A, generation=2))
    assert verdict.safe_to_remove is True


@pytest.mark.parametrize("field", ["run_id", "pool_id", "pool_instance_id", "generation"])
def test_label_mismatch_is_never_removed(field):
    docker = FakeDocker(inspect=managed_inspect(**{field: "other"}))
    result = cleanup_pool_generation(docker=docker, expected=expected_state())
    assert result.removed is False
    assert result.proven is False
    assert docker.remove_calls == []


def test_name_substring_alone_is_not_cleanup_evidence():
    docker = FakeDocker(names=["dswarm-run-run-a-pool-a"])
    result = cleanup_pool_generation(docker=docker, expected=expected_state())
    assert result.removed is False
    assert docker.remove_calls == []
```

Add tests for already-absent containers, inspect failure, remove failure, post-remove absence proof, pool-local RCP revoke, worker-token revoke, cleanup continuing after one failure, no other-run deletion, and executor timeout cleanup marking `cleanup_unproven`.

- [ ] **Step 2: Run cleanup tests**

Run: `uv run pytest -q tests/test_runtime_cleanup.py tests/test_container_runtime.py`

Expected: FAIL because proof-based cleanup primitives do not exist.

- [ ] **Step 3: Implement proof-first cleanup**

Never remove from a guessed name or substring. A new managed container is removable only when inspect proves `com.dswarm.managed=true`, exact run/pool/instance/generation labels, valid identity syntax, and image/network/mount correspondence with private state. After `docker rm -f`, inspect/list must prove absence before returning `proven=True`. Revoke the exact RCP pool instance and all registered per-worker task tokens even when container removal fails, but retain every sanitized failure in the result.

Update `ContainerRuntimeExecutor.terminate(require_proof=True)` to use this primitive. A timeout/cancellation that cannot prove worker process-group cleanup marks the generation untrusted and forces manager degradation; it cannot be reused for the next Worker.

- [ ] **Step 4: Run cleanup, executor, and pool tests**

Run: `uv run pytest -q tests/test_runtime_cleanup.py tests/test_container_runtime.py tests/test_container_pool.py`

Expected: PASS.

- [ ] **Step 5: Run the M9a-3 phase gate and commit**

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: both exit 0.

```powershell
git add dswarm/solver/runtime_cleanup.py dswarm/solver/container_runtime.py tests/test_runtime_cleanup.py tests/test_container_runtime.py
git commit -m "M9a-3 prove exact runtime generation cleanup"
```
## M9a-4 — Swarm Lease Wiring and Runtime Failure Isolation

### Task 13: Inject RuntimePolicy, frozen snapshot, and one run-scoped PoolManager into Swarm

**Files:**
- Modify: `dswarm/swarm/swarm.py`
- Modify: `dswarm/swarm/worker_runtime_mixin.py`
- Modify: `apps/web/run_manager.py`
- Modify: `apps/tui/__main__.py`
- Create: `tests/test_swarm_runtime_context.py`

**Interfaces:**
- Consumes: `RuntimePolicy`, `RuntimeSnapshot`, `RuntimeSnapshotStore`, `ContainerPoolManager`.
- Produces: `Swarm.runtime_policy`; `Swarm.runtime_snapshot`; `Swarm.pool_manager`; `Swarm.pool_id_for_profile(profile_id: str) -> str`; run-manager/TUI construction paths that build or load exactly one frozen runtime snapshot before a real run starts.

- [ ] **Step 1: Write failing runtime-context construction tests**

```python
# tests/test_swarm_runtime_context.py
def test_swarm_uses_injected_frozen_runtime_context(challenge, snapshot, manager):
    policy = build_runtime_policy(env={})
    swarm = make_swarm(
        challenge,
        runtime_policy=policy,
        runtime_snapshot=snapshot,
        pool_manager=manager,
    )
    assert swarm.runtime_policy is policy
    assert swarm.runtime_snapshot is snapshot
    assert swarm.pool_manager is manager
    assert swarm.pool_id_for_profile("pi-main") == snapshot.profile_to_pool["pi-main"]


def test_python_local_mode_cannot_be_enabled_by_worker_backend_string(challenge):
    with pytest.raises(RuntimePolicyError, match="local_worker_policy_denied"):
        make_swarm(challenge, worker_backend="local", runtime_policy=None)
```

Add tests proving: Docker is the default for real runs; a supplied snapshot run ID must equal `Swarm.run_id`; the manager must own the same run/snapshot; unknown profile lookup is a structured `runtime_profile_not_in_snapshot` error; mock-only TUI does not construct a manager; RunManager create builds and persists one snapshot; process-level rehydrate loads the existing snapshot rather than consulting changed live settings.

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest -q tests/test_swarm_runtime_context.py tests/test_runtime_policy.py tests/test_runtime_snapshot.py`

Expected: FAIL because `Swarm` has no runtime-context contract.

- [ ] **Step 3: Add immutable runtime context to construction paths**

Extend `Swarm.__init__` with keyword-only parameters:

```python
runtime_policy: RuntimePolicy | None = None
runtime_snapshot: RuntimeSnapshot | None = None
pool_manager: ContainerPoolManager | None = None
```

For a real swarm, normalize `runtime_policy` once. In Docker mode require a matching snapshot and manager before a true Worker can be created; RunManager and real TUI must create/load them through `RuntimeSnapshotStore`. In local-dev mode require `runtime_policy.local_workers_allowed`; never infer permission from `worker_backend`, test environment, or container failure. Keep mock UI construction outside this path.

`pool_id_for_profile()` reads only the frozen `profile_to_pool` mapping. Configuration changes after snapshot creation are diagnostics only and cannot mutate the active run. Do not yet acquire leases or remove the old compatibility handle in this task.

- [ ] **Step 4: Run context and frontend construction tests**

Run: `uv run pytest -q tests/test_swarm_runtime_context.py tests/test_run_manager_archive.py tests/test_tui.py`

Expected: PASS.

- [ ] **Step 5: Commit runtime-context injection**

```powershell
git add dswarm/swarm/swarm.py dswarm/swarm/worker_runtime_mixin.py apps/web/run_manager.py apps/tui/__main__.py tests/test_swarm_runtime_context.py
git commit -m "M9a-4 inject frozen runtime context into swarm"
```

### Task 14: Acquire one WorkerRuntimeLease at the async CliSolver boundary

**Files:**
- Modify: `dswarm/solver/cli_solver.py`
- Modify: `dswarm/swarm/worker_runtime_mixin.py`
- Create: `tests/test_worker_runtime_lease.py`
- Modify: `tests/test_swarm.py`

**Interfaces:**
- Consumes: `ContainerPoolManager.acquire(...) -> WorkerRuntimeLease`, `RuntimePolicy`, existing `_make_cli_worker()` and `CliSolver.run()`.
- Produces: `RuntimeLeaseFactory = Callable[[str, str], Awaitable[WorkerRuntimeLease]]`; `CliSolver(..., runtime_lease_factory: RuntimeLeaseFactory | None = None, runtime_policy: RuntimePolicy | None = None)`; exactly-once lease acquire/release around the complete Worker session.

- [ ] **Step 1: Write failing async-boundary tests**

```python
# tests/test_worker_runtime_lease.py
@pytest.mark.asyncio
async def test_cli_solver_acquires_once_and_releases_on_success(scripted_solver):
    factory = FakeLeaseFactory()
    solver = scripted_solver(runtime_lease_factory=factory)
    await solver.run()
    assert factory.acquire_calls == [(solver.solver_id, solver.task_kind or solver.mode)]
    assert factory.lease.release_calls == 1
    assert solver.container is factory.lease.executor


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["cancelled", "raised"])
async def test_cli_solver_releases_lease_on_every_terminal(scripted_solver, terminal):
    factory = FakeLeaseFactory(terminal=terminal)
    solver = scripted_solver(runtime_lease_factory=factory)
    if terminal == "cancelled":
        task = asyncio.create_task(solver.run())
        await factory.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError):
            await solver.run()
    assert factory.lease.release_calls == 1
```

Add tests that acquire happens before the first `run_cli_streaming` invocation; all turns/continuations of one Worker reuse the same lease; `worker_env` comes only from the lease; local-dev uses the explicitly permitted host executor; Docker acquire failure does not call `resolve_engine_bin("pi")`; cancellation while waiting for capacity releases no nonexistent lease and leaks no permit.

- [ ] **Step 2: Run the lease tests**

Run: `uv run pytest -q tests/test_worker_runtime_lease.py tests/test_swarm.py`

Expected: FAIL because `CliSolver` cannot receive a runtime lease factory.

- [ ] **Step 3: Wire acquisition at the executable async boundary**

Keep `_make_cli_worker()` synchronous. It selects the frozen pool and injects an async closure:

```python
async def runtime_lease_factory(worker_instance_id: str, operation_kind: str) -> WorkerRuntimeLease:
    return await self.pool_manager.acquire(
        pool_id=pool_id,
        worker_instance_id=worker_instance_id,
        operation_kind=operation_kind,
    )
```

At the outer edge of `CliSolver.run()`, acquire before emitting a spawned/online status or invoking the driver, then set `self.container = lease.executor`, merge only `lease.worker_env`, and retain the lease for all turns. In the outermost `finally`, call `await lease.release()` exactly once after subprocess cleanup and token/credential cleanup. If acquisition fails, emit only a sanitized runtime status/error and re-raise the structured runtime exception; do not create evidence or a dead-end.

The local-dev branch must be selected solely by `RuntimePolicy` and use the existing host runner without constructing a Docker lease. No Docker failure may switch into that branch.

- [ ] **Step 4: Run Worker and cancellation regressions**

Run: `uv run pytest -q tests/test_worker_runtime_lease.py tests/test_swarm.py tests/test_container_pool.py tests/test_lane_gate.py`

Expected: PASS.

- [ ] **Step 5: Commit lease wiring**

```powershell
git add dswarm/solver/cli_solver.py dswarm/swarm/worker_runtime_mixin.py tests/test_worker_runtime_lease.py tests/test_swarm.py
git commit -m "M9a-4 route cli workers through runtime leases"
```

### Task 15: Route bootstrap, ordinary, review, recon, recovery, standby, resolve, and BTW through the same manager

**Files:**
- Modify: `dswarm/swarm/runtime.py`
- Modify: `dswarm/swarm/review_flow.py`
- Modify: `dswarm/swarm/swarm.py`
- Modify: `dswarm/swarm/worker_runtime_mixin.py`
- Modify: `dswarm/solver/btw.py`
- Modify: `apps/web/routes/btw.py`
- Modify: `apps/web/run_manager.py`
- Create: `tests/test_runtime_spawn_paths.py`
- Modify: `tests/test_btw.py`
- Modify: `tests/test_swarm.py`

**Interfaces:**
- Consumes: `CliSolver.runtime_lease_factory`, `Run.pool_manager`, frozen profile-to-pool lookup, per-worker operation kinds.
- Produces: one audited `RuntimeSpawnRequest(profile_id, worker_instance_id, operation_kind, mode, intent_id)` construction helper; no real shell path that directly calls legacy `ensure_container`, stores a run-global handle, or launches host Pi in Docker mode.

- [ ] **Step 1: Write failing path-audit tests**

```python
# tests/test_runtime_spawn_paths.py
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    "bootstrap", "ordinary", "review", "recon", "recovery", "standby", "resolve", "btw",
])
async def test_every_real_spawn_acquires_the_run_manager(operation, runtime_harness):
    await runtime_harness.invoke(operation)
    assert runtime_harness.manager.acquire_operation_kinds == [operation]
    assert runtime_harness.host_pi_calls == []


def test_production_modules_do_not_import_legacy_container_ownership():
    paths = [
        "dswarm/swarm/swarm.py", "dswarm/swarm/worker_runtime_mixin.py",
        "dswarm/swarm/runtime.py", "dswarm/swarm/review_flow.py",
        "dswarm/solver/btw.py", "apps/web/routes/btw.py",
    ]
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        assert "ensure_container(" not in text
        assert "_container_handle" not in text
```

Extend BTW tests to prove it uses `run.pool_manager`, gets an independent worker identity/token/credential lease, releases on success/error/cancel, and cannot construct a second run-global container. Add standby/resolve tests proving the same manager and frozen snapshot are retained within a live process.

- [ ] **Step 2: Run spawn-path tests**

Run: `uv run pytest -q tests/test_runtime_spawn_paths.py tests/test_btw.py tests/test_swarm.py`

Expected: FAIL on the legacy BTW and single-container paths.

- [ ] **Step 3: Centralize RuntimeSpawnRequest construction**

Define a frozen request at the swarm/runtime boundary:

```python
@dataclass(frozen=True)
class RuntimeSpawnRequest:
    profile_id: str
    worker_instance_id: str
    operation_kind: Literal[
        "bootstrap", "ordinary", "review", "recon", "recovery", "standby", "resolve", "btw"
    ]
    mode: str
    intent_id: str = ""
```

Every path constructs this request, resolves `pool_id` only from `RuntimeSnapshot`, and injects the lease factory into `CliSolver` or the lower-level BTW runner. Replace the BTW route's direct `ensure_container`, chown, gateway-token, and container-env code with one `run.pool_manager.acquire(..., operation_kind="btw")` lease. Preserve the existing BTW limiter and M5 producer classification.

Audit review/recon/recovery bootstrap helpers and post-solve standby/`/resolve` paths explicitly. A path with no compatible frozen pool returns a structured runtime failure without adding graph rows. WorkerLaneGate remains outside and authoritative over the additional pool semaphore.

- [ ] **Step 4: Run all shell-entry regressions**

Run: `uv run pytest -q tests/test_runtime_spawn_paths.py tests/test_btw.py tests/test_swarm.py tests/test_reason_swarm.py tests/test_lane_gate.py`

Expected: PASS.

- [ ] **Step 5: Commit full spawn-path routing**

```powershell
git add dswarm/swarm/runtime.py dswarm/swarm/review_flow.py dswarm/swarm/swarm.py dswarm/swarm/worker_runtime_mixin.py dswarm/solver/btw.py apps/web/routes/btw.py apps/web/run_manager.py tests/test_runtime_spawn_paths.py tests/test_btw.py tests/test_swarm.py
git commit -m "M9a-4 route every real spawn through pool manager"
```

### Task 16: Add pool-local failure isolation and frozen-snapshot failover

**Files:**
- Modify: `dswarm/solver/container_pool.py`
- Modify: `dswarm/swarm/runtime.py`
- Modify: `dswarm/swarm/worker_runtime_mixin.py`
- Modify: `dswarm/swarm/swarm.py`
- Create: `tests/test_runtime_failover.py`
- Modify: `tests/test_direction_diagnostics.py`

**Interfaces:**
- Consumes: `RuntimeFailure`, pool state, frozen snapshot compatibility lists, existing M4 direction diagnostics.
- Produces: `select_runtime_failover(*, snapshot: RuntimeSnapshot, failed_pool_id: str, profile_id: str, route: str) -> str | None`; sanitized runtime-failover diagnostics; run-level `runtime_unavailable` decision only after the frozen candidate set and active/recovery state are exhausted.

- [ ] **Step 1: Write failing isolation/failover tests**

```python
# tests/test_runtime_failover.py
def test_failover_uses_only_compatible_pool_from_frozen_snapshot(snapshot):
    chosen = select_runtime_failover(
        snapshot=snapshot, failed_pool_id="pool-a", profile_id="pi-web-a", route="web")
    assert chosen == "pool-b"


def test_live_configuration_profile_is_never_added_to_active_run(snapshot, live_settings):
    live_settings.add_profile("pi-web-new")
    assert select_runtime_failover(
        snapshot=snapshot, failed_pool_id="pool-a", profile_id="pi-web-a", route="web"
    ) != live_settings.pool_id_for("pi-web-new")
```

Add async tests proving: one degraded pool does not cancel active workers in other pools; a worker-only failure does not degrade its pool; identity/auth/configuration failures do not rebuild; infrastructure gets one recovery generation; unproven cleanup blocks replacement; same-route profile failover emits no M4 `direction_override`; actual route change still emits the existing M4 override; runtime failures create no fact/dead-end/finding; run-level failure requires no active Worker and no allowed recovery.

- [ ] **Step 2: Run failover tests**

Run: `uv run pytest -q tests/test_runtime_failover.py tests/test_direction_diagnostics.py`

Expected: FAIL because selection and terminal criteria are not implemented.

- [ ] **Step 3: Implement deterministic frozen failover**

Use snapshot order as the stable tie-breaker. Filter candidates by route/profile compatibility, enabled state, and non-failed pool state; never consult current settings. Emit a sanitized runtime diagnostic containing failed/chosen pool IDs and failure code. Do not change direction when only profile/runtime changes. If no compatible pool remains, wait for other active workers and the one allowed infrastructure recovery before returning `runtime_unavailable`.

`ContainerPoolManager.mark_failure()` owns failure classification and pool transitions; scheduler code consumes its view rather than duplicating state. No failure handler may invoke local/legacy execution.

- [ ] **Step 4: Run failover, direction, and graph regressions**

Run: `uv run pytest -q tests/test_runtime_failover.py tests/test_direction_diagnostics.py tests/test_shared_graph.py tests/test_swarm.py`

Expected: PASS.

- [ ] **Step 5: Commit failure isolation**

```powershell
git add dswarm/solver/container_pool.py dswarm/swarm/runtime.py dswarm/swarm/worker_runtime_mixin.py dswarm/swarm/swarm.py tests/test_runtime_failover.py tests/test_direction_diagnostics.py
git commit -m "M9a-4 isolate runtime pool failures"
```

### Task 17: Remove production single-container ownership and lock provenance/lane/flag semantics

**Files:**
- Modify: `dswarm/swarm/swarm.py`
- Modify: `dswarm/swarm/worker_runtime_mixin.py`
- Modify: `dswarm/solver/container_exec.py`
- Modify: `tests/test_swarm.py`
- Create: `tests/test_m9a_invariants.py`

**Interfaces:**
- Consumes: completed pool-based spawn wiring.
- Produces: no production `_container_handle`, `_container_runtime_id`, `_container_unavailable`, `_container()`, or run-end `teardown_container()` behavior; a test-only/unsafe-local compatibility facade isolated to `container_exec.py`.

- [ ] **Step 1: Write failing invariant tests**

```python
# tests/test_m9a_invariants.py
def test_swarm_has_no_run_global_container_fields(challenge, runtime_context):
    swarm = make_swarm(challenge, **runtime_context)
    for name in ("_container_handle", "_container_runtime_id", "_container_unavailable"):
        assert not hasattr(swarm, name)


def test_run_finished_does_not_close_pool_manager():
    text = Path("dswarm/swarm/swarm.py").read_text(encoding="utf-8")
    run_block = text[text.index("async def run(self)"):]
    assert "teardown_container" not in run_block
```

Add regression tests proving: ordinary/review lane limits are unchanged; actual worker stdout/stderr still reaches the same provenance gate; guidance is not a flag source; first-valid-flag and multi-flag completion behavior is byte-for-byte equivalent when pool topology is behaviorally identical; SharedGraph remains append-only; no M9 runtime diagnostic is added to Reason prompt input.

- [ ] **Step 2: Run invariant tests**

Run: `uv run pytest -q tests/test_m9a_invariants.py tests/test_swarm.py tests/test_lane_gate.py`

Expected: FAIL while legacy state and run-end teardown remain.

- [ ] **Step 3: Delete production single-handle ownership**

Remove the three fields, `_container()` ownership method, and `Swarm.run()` teardown. Keep only low-level helpers that `ContainerRuntimeExecutor` imports. Put legacy facade entry points behind an explicit function such as:

```python
def legacy_container_allowed(policy: RuntimePolicy) -> bool:
    return policy.mode == "local_dev" and policy.local_workers_allowed
```

The facade must raise `legacy_container_disabled` for production Docker policy and must not be imported by Swarm/Web/BTW production modules.

- [ ] **Step 4: Run the M9a-4 phase gate**

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: both exit 0.

- [ ] **Step 5: Commit removal and invariant lock**

```powershell
git add dswarm/swarm/swarm.py dswarm/swarm/worker_runtime_mixin.py dswarm/solver/container_exec.py tests/test_swarm.py tests/test_m9a_invariants.py
git commit -m "M9a-4 retire production single container ownership"
```

## M9a-5 — Docker-First Web and Real TUI Entry Points

### Task 18: Make `run.sh web` launch the Compose control plane by default

**Files:**
- Modify: `run.sh`
- Modify: `docker-compose.yml`
- Create: `tests/test_run_sh.py`
- Modify: `tests/test_web_server.py`

**Interfaces:**
- Consumes: Docker-first `RuntimePolicy`, existing `web-api`/`ui` Compose services, `DSWARM_HOST_DATA_ROOT` and sessions-root mount contract.
- Produces: `./run.sh web` → `docker compose up --build web-api ui`; `--backend-only` → only `web-api`; explicit `--local-dev` → host path only when the environment gate is also present.

- [ ] **Step 1: Write failing command-construction tests**

```python
# tests/test_run_sh.py
def test_web_defaults_to_compose(fake_commands):
    result = run_sh("web", env=minimal_compose_env(), commands=fake_commands)
    assert result.returncode == 0
    assert fake_commands.calls == [[
        "docker", "compose", "up", "--build", "web-api", "ui"
    ]]
    assert not fake_commands.called("uvicorn")


def test_web_backend_only_starts_only_api(fake_commands):
    run_sh("web", "--backend-only", env=minimal_compose_env(), commands=fake_commands)
    assert fake_commands.calls[-1] == [
        "docker", "compose", "up", "--build", "web-api"
    ]
```

Add tests for signal/exit-code propagation; `--port` and `--ui-port` becoming Compose published-port environment values; default host binds of `127.0.0.1`; Docker daemon/Compose absence producing a nonzero structured message without launching host backend; and `--local-dev` without the environment gate failing before `uvicorn` or Pi resolution.

- [ ] **Step 2: Run shell-entry tests**

Run: `uv run pytest -q tests/test_run_sh.py tests/test_web_server.py`

Expected: FAIL because Web still launches host processes.

- [ ] **Step 3: Replace the default Web launch branch**

Refactor command parsing so the branch is explicit:

```bash
if [[ "$local_dev" == "1" ]]; then
  require_local_worker_gate
  run_web_local
else
  require_docker_compose
  export DSWARM_WEB_PUBLISH_HOST="${DSWARM_WEB_PUBLISH_HOST:-127.0.0.1}"
  docker compose up --build "${services[@]}"
fi
```

Compose must mount the host Docker socket only into `web-api`, keep the host data root at the same absolute path inside the control plane, set Docker runtime policy explicitly, and leave Worker Pools as sibling `docker run` containers rather than Compose services. Publish API/UI to `${DSWARM_WEB_PUBLISH_HOST}`; do not publish the RCP receiver or ModelGateway to non-control networks. Preserve same-origin UI proxy behavior and backend-only semantics.

- [ ] **Step 4: Validate shell and Compose syntax**

Run: `bash -n ./run.sh`

Run: `docker compose config`

Run: `uv run pytest -q tests/test_run_sh.py tests/test_web_server.py tests/test_web_auth.py`

Expected: all exit 0.

- [ ] **Step 5: Commit Docker-first Web**

```powershell
git add run.sh docker-compose.yml tests/test_run_sh.py tests/test_web_server.py
git commit -m "M9a-5 make web control plane docker first"
```

### Task 19: Add an interactive `tui-control` Compose service for real swarm runs

**Files:**
- Create: `docker/tui/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `run.sh`
- Modify: `apps/tui/__main__.py`
- Modify: `tests/test_run_sh.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: real-TUI arguments, Docker-first runtime policy, shared sessions/data root, Docker socket and compose network.
- Produces: `./run.sh tui --swarm ...` → interactive `docker compose run --rm tui-control ...`; `./run.sh tui` remains the host mock UI with no Docker Worker setup.

- [ ] **Step 1: Write failing TUI launch-boundary tests**

```python
def test_real_tui_uses_interactive_control_container(fake_commands):
    run_sh("tui", "--swarm", "--desc", "x", "--target", "http://target",
           env=minimal_compose_env(), commands=fake_commands)
    assert fake_commands.calls[-1][:6] == [
        "docker", "compose", "run", "--rm", "tui-control", "--swarm"
    ]


def test_mock_tui_stays_host_only(fake_commands):
    run_sh("tui", env={}, commands=fake_commands)
    assert fake_commands.called("uv", "run", "python", "-m", "apps.tui")
    assert not fake_commands.called("docker")
```

Add tests for TTY/stdin preservation, Ctrl+C exit propagation, all arguments forwarded without shell re-parsing, control-container shutdown awaiting `pool_manager.close()`, and a real TUI refusing startup when Docker is unavailable rather than using host Pi.

- [ ] **Step 2: Run TUI entry tests**

Run: `uv run pytest -q tests/test_run_sh.py tests/test_tui.py`

Expected: FAIL because real and mock TUI share the host path.

- [ ] **Step 3: Add the TUI control image and lifecycle**

The image contains only the D-Swarm control application and Python dependencies; Worker tooling remains in Worker images. Configure the service with:

```yaml
tui-control:
  build:
    context: .
    dockerfile: docker/tui/Dockerfile
  stdin_open: true
  tty: true
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - ${DSWARM_HOST_DATA_ROOT}:${DSWARM_HOST_DATA_ROOT}
  environment:
    DSWARM_RUNTIME_MODE: docker
    DSWARM_WORKER_NETWORK: dswarm_net
  networks: [dswarm_net]
```

Do not publish TUI ports. The control container may access the Docker socket; sibling Worker Pools may not. In `_swarm_driver()`, construct the runtime context and call `await swarm.pool_manager.close()` in the outer lifecycle `finally` after worker cancellation and sandbox shutdown. The mock driver must not construct a snapshot, manager, or Docker client.

- [ ] **Step 4: Validate TUI behavior and Compose config**

Run: `bash -n ./run.sh`

Run: `docker compose config`

Run: `uv run pytest -q tests/test_run_sh.py tests/test_tui.py`

Expected: all exit 0.

- [ ] **Step 5: Commit real-TUI control container**

```powershell
git add docker/tui/Dockerfile docker-compose.yml run.sh apps/tui/__main__.py tests/test_run_sh.py tests/test_tui.py
git commit -m "M9a-5 add docker first real tui control plane"
```

### Task 20: Enforce dual-gate local development, loopback/password policy, and fail-closed launch behavior

**Files:**
- Modify: `run.sh`
- Modify: `docker-compose.yml`
- Modify: `dswarm/core/runtime_env.py`
- Modify: `apps/web/server.py`
- Modify: `.env.example`
- Create: `tests/test_runtime_launch_security.py`
- Modify: `tests/test_web_auth.py`
- Modify: `tests/test_runtime_env.py`

**Interfaces:**
- Consumes: `RuntimePolicy`, Web bind/auth configuration, Compose-published host/address, control-plane marker.
- Produces: one launch validator used by shell and Python construction; non-loopback publish requires `DSWARM_WEB_PASSWORD`; Docker failures remain terminal; secrets are never included in command echoes or diagnostics.

- [ ] **Step 1: Write failing launch-security tests**

```python
# tests/test_runtime_launch_security.py
@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_non_loopback_publish_requires_password(host, fake_commands):
    result = run_sh("web", "--host", host, env=minimal_compose_env(), commands=fake_commands)
    assert result.returncode != 0
    assert "web_password_required_for_non_loopback" in result.stderr
    assert fake_commands.calls == []


def test_docker_failure_never_executes_host_pi(fake_commands):
    fake_commands.fail("docker", stderr="daemon unavailable")
    result = run_sh("web", env=minimal_compose_env(), commands=fake_commands)
    assert result.returncode != 0
    assert not fake_commands.called("pi")
    assert not fake_commands.called("uvicorn")
```

Add tests for each half of the local-dev gate, Python `Swarm` construction bypass attempts, default loopback without password, explicit non-loopback with password, password/token redaction, Worker containers lacking Docker socket, control-plane internal `0.0.0.0` not being mistaken for a public bind when host publication is loopback, and no environment fallback after image/RCP/Probe errors.

- [ ] **Step 2: Run security tests**

Run: `uv run pytest -q tests/test_runtime_launch_security.py tests/test_web_auth.py tests/test_runtime_env.py`

Expected: FAIL until launch policy has one authoritative validation path.

- [ ] **Step 3: Implement fail-closed validation**

Add pure validation helpers in `runtime_env.py` for loopback recognition and control-plane/public-bind distinction. `run.sh` validates the public host before Compose or host-local startup. The backend receives both the internal bind and sanitized public-publish mode; it may bind `0.0.0.0` inside the private Compose network without requiring a password only when the published host is loopback and the trusted compose-control marker is set by Compose, never by a Worker.

Do not print environment dumps. Redact any password, RCP token, M5 task token, provider key, or credential path from failures. Inspect rendered Compose config in tests to assert Docker socket appears only on `web-api`/`tui-control`, not on Worker launch templates.

- [ ] **Step 4: Run the M9a-5 phase gate**

Run: `bash -n ./run.sh`

Run: `docker compose config`

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: all exit 0.

- [ ] **Step 5: Commit launch security**

```powershell
git add run.sh docker-compose.yml dswarm/core/runtime_env.py apps/web/server.py .env.example tests/test_runtime_launch_security.py tests/test_web_auth.py tests/test_runtime_env.py
git commit -m "M9a-5 enforce docker first launch security"
```

## M9a-6 — Private Diagnostics, Reopen Barrier, Lifecycle Cleanup, and Release Proof

### Task 21: Persist sanitized private runtime diagnostics and expose a read-only pool API

**Files:**
- Create: `dswarm/solver/runtime_diagnostics.py`
- Create: `apps/web/routes/runtime_pools.py`
- Modify: `dswarm/solver/container_pool.py`
- Modify: `dswarm/solver/container_runtime.py`
- Modify: `apps/web/server.py`
- Create: `tests/test_runtime_diagnostics.py`
- Modify: `tests/test_web_server.py`
- Modify: `apps/web/ui/lib/events.test.ts`

**Interfaces:**
- Consumes: `RuntimePoolView`, `RuntimeExecRecord.snapshot()`, manager transition callbacks, existing `WORKER_STATUS` and `PROVIDER_ERROR` payloads.
- Produces: `RuntimeDiagnosticsStore`; private `state.v1.json` and `diagnostics/lifecycle.jsonl`; `GET /api/runs/{run_id}/runtime-pools`; allowlisted `WORKER_STATUS.runtime` payloads.

- [ ] **Step 1: Write failing persistence, redaction, and endpoint tests**

```python
# tests/test_runtime_diagnostics.py
def test_private_state_and_jsonl_are_secret_free(tmp_path, pool_view):
    store = RuntimeDiagnosticsStore(run_root=tmp_path, run_id="run-a")
    store.record_transition(pool_view, error="Bearer secret-token at C:/Users/me/.pi")
    payload = json.loads(store.state_path("pool-a").read_text(encoding="utf-8"))
    line = json.loads(store.lifecycle_path("pool-a").read_text(encoding="utf-8").splitlines()[0])
    serialized = json.dumps([payload, line])
    assert "secret-token" not in serialized
    assert "C:/Users/me" not in serialized
    assert payload["pool_id"] == "pool-a"


@pytest.mark.asyncio
async def test_runtime_pools_get_is_read_only(web_client, run_with_manager):
    before = run_with_manager.pool_manager.transition_count
    response = await web_client.get(f"/api/runs/{run_with_manager.id}/runtime-pools")
    assert response.status_code == 200
    assert response.json()["pools"][0]["pool_id"] == "pool-a"
    assert run_with_manager.pool_manager.transition_count == before
```

Add tests for atomic state writes (`temp → flush → fsync → replace`), append-only JSONL, partial-tail tolerance, one pool directory per sanitized pool ID, file mode/private-root expectations where supported, endpoint auth/not-found parity, exact API allowlist, image ID shortening, no host paths/secrets/raw stderr, `actor=""` or existing worker actor behavior that does not create phantom UI workers, and byte-identical Reason prompt/evidence rows with diagnostics enabled/disabled.

- [ ] **Step 2: Run diagnostics tests**

Run: `uv run pytest -q tests/test_runtime_diagnostics.py tests/test_web_server.py`

Run: `npm --prefix apps/web/ui test -- lib/events.test.ts`

Expected: Python tests fail on missing store/router; UI regression must remain green or expose an unsafe payload assumption.

- [ ] **Step 3: Implement the private store and allowlisted projection**

Use these coordinator-private paths only:

```text
sessions/<run-id>/.runtime/pools/<pool-id>/state.v1.json
sessions/<run-id>/.runtime/pools/<pool-id>/diagnostics/lifecycle.jsonl
```

`RuntimeDiagnosticsStore` receives already-typed identities and sanitizer codes; it never accepts arbitrary event/model dumps. State includes only pool/run-safe IDs, state/generation, active/waiting/capacity, image short ID, probe status, timestamps, and reason code. JSONL records transition kind and the same allowlist. Persistence failure is operationally visible but cannot create graph evidence or change scheduler direction.

The endpoint obtains `Run.pool_manager.snapshot_view()` and frozen snapshot metadata without creating a pool, running Probe, or touching credentials. Register the router explicitly in `server.py`. Runtime status uses the existing event types only; `PROVIDER_ERROR` carries a bounded error code/message, and no new canonical graph event is introduced.

- [ ] **Step 4: Run diagnostics, API, UI, and prompt-isolation tests**

Run: `uv run pytest -q tests/test_runtime_diagnostics.py tests/test_web_server.py tests/test_reason_swarm.py tests/test_shared_graph.py`

Run: `npm --prefix apps/web/ui test -- lib/events.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit observability**

```powershell
git add dswarm/solver/runtime_diagnostics.py dswarm/solver/container_pool.py dswarm/solver/container_runtime.py apps/web/routes/runtime_pools.py apps/web/server.py tests/test_runtime_diagnostics.py tests/test_web_server.py apps/web/ui/lib/events.test.ts
git commit -m "M9a-6 add private runtime pool diagnostics"
```

### Task 22: Enforce the run-wide reopen cleanup barrier and lifecycle-owned manager close

**Files:**
- Modify: `dswarm/solver/runtime_cleanup.py`
- Modify: `dswarm/solver/container_pool.py`
- Modify: `apps/web/run_manager.py`
- Modify: `apps/web/server.py`
- Modify: `apps/tui/__main__.py`
- Modify: `dswarm/swarm/swarm.py`
- Create: `tests/test_runtime_reopen_barrier.py`
- Modify: `tests/test_run_manager_archive.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: exact generation cleanup from Task 12, private runtime state, Docker inspection, RCP and M5 token revocation, `ContainerPoolManager.close()`.
- Produces: `RuntimeCleanupInspector.cleanup_run_before_reopen(run_id: str, run_root: Path) -> RuntimeCleanupResult`; mandatory pre-dispatch barrier; close ownership on delete/archive/server shutdown/TUI exit/explicit dispose.

- [ ] **Step 1: Write failing reopen and teardown ownership tests**

```python
# tests/test_runtime_reopen_barrier.py
@pytest.mark.asyncio
async def test_reopen_barrier_precedes_probe_cycle_and_dispatch(reopen_harness):
    await reopen_harness.reopen()
    assert reopen_harness.order[:4] == [
        "cleanup_barrier", "runtime_context_ready", "reason_cycle", "worker_acquire"
    ]


@pytest.mark.asyncio
async def test_unproven_stale_runtime_rejects_reopen(reopen_harness):
    reopen_harness.cleanup_result = RuntimeCleanupResult(proven=False, failures=("inspect_failed",))
    with pytest.raises(RuntimeError, match="stale_runtime_cleanup_unproven"):
        await reopen_harness.reopen()
    assert reopen_harness.probe_calls == 0
    assert reopen_harness.dispatch_calls == 0
```

Add tests for exact-label new-container cleanup; already-absent success; legacy exact-name plus mount/state corroboration; legacy name-only refusal; cleanup continuing across pools while aggregate failure rejects reopen; exact RCP pool and worker-token revocation; idempotent manager close; close not deleting another run; `RUN_FINISHED` preserving pools; delete/archive/server shutdown/TUI exit calling close; close failure persisting private state for the next barrier.

- [ ] **Step 2: Run lifecycle tests**

Run: `uv run pytest -q tests/test_runtime_reopen_barrier.py tests/test_run_manager_archive.py tests/test_tui.py`

Expected: FAIL because reopen and ownership paths do not call the proof barrier/manager close.

- [ ] **Step 3: Implement the barrier and lifecycle ownership**

`cleanup_run_before_reopen()` enumerates candidates from private state plus exact managed labels, never a name substring. For legacy state, require the exact legacy name and corroborating run workspace mount/state; insufficient evidence is a hard failure. Attempt every candidate and every token/link revocation, aggregate sanitized failures, and return `proven=True` only when every old runtime is proven absent and identities revoked.

At process-level reopen/rehydrate, execute the barrier before constructing a new manager, before RuntimeProbe, before Reason, and before any Worker dispatch. A failed barrier leaves the run stopped and retryable.

Remove teardown ownership from `Swarm.run()`/`RUN_FINISHED`. Call `await pool_manager.close()` from RunManager delete, archive, shutdown and explicit dispose; from TUI outer shutdown; and from any create failure after a manager exists. `close()` is idempotent, drains/cancels acquisition waiters, terminates exact generations, revokes identities, and writes residual state when proof is incomplete.

- [ ] **Step 4: Run lifecycle and full regressions**

Run: `uv run pytest -q tests/test_runtime_reopen_barrier.py tests/test_run_manager_archive.py tests/test_tui.py tests/test_swarm.py tests/test_container_pool.py`

Expected: PASS.

- [ ] **Step 5: Commit reopen and close ownership**

```powershell
git add dswarm/solver/runtime_cleanup.py dswarm/solver/container_pool.py apps/web/run_manager.py apps/web/server.py apps/tui/__main__.py dswarm/swarm/swarm.py tests/test_runtime_reopen_barrier.py tests/test_run_manager_archive.py tests/test_tui.py
git commit -m "M9a-6 enforce runtime reopen cleanup barrier"
```

### Task 23: Lock down legacy compatibility, document forward-only operations, and add real Docker pool integration

**Files:**
- Modify: `dswarm/solver/container_exec.py`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `.env.example`
- Create: `docs/runtime-pools.md`
- Create: `tests/integration/test_container_pools.py`
- Create: `tests/integration/fixtures/fake_pi.py`
- Create: `tests/integration/fixtures/fake_provider.py`
- Create: `tests/integration/fixtures/Dockerfile.worker`
- Modify: `tests/test_container_exec.py`

**Interfaces:**
- Consumes: complete M9a runtime path, compatibility facade policy, Docker Compose/network, fake local provider.
- Produces: forward-only operator runbook; explicit rollback cleanup order; opt-in `DSWARM_RUN_DOCKER_TESTS=1` integration proof for two Pools, Probe ordering, usage attribution, stale-link rejection, and residue-free close.

- [ ] **Step 1: Write failing compatibility and integration tests**

```python
# tests/test_container_exec.py
def test_legacy_facade_rejects_production_policy():
    with pytest.raises(RuntimeError, match="legacy_container_disabled"):
        ensure_container_legacy_for_tests(
            "run-a", "/workspace", policy=build_runtime_policy(env={})
        )


# tests/integration/test_container_pools.py
@pytest.mark.skipif(os.getenv("DSWARM_RUN_DOCKER_TESTS") != "1", reason="opt-in Docker test")
@pytest.mark.asyncio
async def test_two_pool_fake_pi_end_to_end(docker_harness):
    outcome = await docker_harness.run_two_pool_fixture()
    assert outcome.max_simultaneous_workers >= 2
    assert outcome.probe_before_worker == {"pool-a": True, "pool-b": True}
    assert outcome.usage_operation_kinds == {"runtime_probe", "ordinary"}
    assert outcome.worker_mounts_exclude_docker_socket is True
    assert outcome.remaining_managed_containers == []
```

Add Docker assertions for exact labels/image/network/mounts; same UID/GID; separate HOME/session/workdir/token; no host HOME/`.pi`/`.runtime`/other Pool credential; one long-lived container per PoolKey; concurrent processes inside one Pool; generation rebuild rejecting stale RCP link; Probe tool-disable marker; no real provider key/network billing; cleanup after success/error/cancel; and integration skip behavior when the opt-in flag is absent.

- [ ] **Step 2: Run compatibility tests and confirm integration is opt-in**

Run: `uv run pytest -q tests/test_container_exec.py tests/integration/test_container_pools.py`

Expected: compatibility test FAIL until facade lockdown; integration test SKIP without the opt-in flag.

- [ ] **Step 3: Seal the legacy facade and write the operator contract**

The legacy facade must be named as unsafe/test-only, require the explicit local-dev policy, and have no production caller. `docs/runtime-pools.md` must document:

```text
pool identity and per-run ownership
Docker-first Web and real TUI commands
local-dev dual gate
image/UID/GID/network/credential requirements
private diagnostics and runtime-pools endpoint
failure classes and one-rebuild limit
reopen cleanup barrier
RUN_FINISHED versus close ownership
forward-only upgrade
rollback: stop dispatch → close all pools with new binary → verify no managed labels → then run old binary
```

README/README_CN link the runbook and explain that Worker containers never receive the Docker socket or host Pi state. `.env.example` lists only non-secret knobs/defaults and documented secret variable names with redacted example values.

- [ ] **Step 4: Build and run the opt-in Docker integration**

Run: `docker compose config`

Run: `$env:DSWARM_RUN_DOCKER_TESTS='1'; uv run pytest -q tests/integration/test_container_pools.py`

Expected: PASS using only the fake Pi and fake provider; no real credential is read.

- [ ] **Step 5: Run the M9a-6 phase gate and commit**

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: both exit 0.

```powershell
git add dswarm/solver/container_exec.py README.md README_CN.md .env.example docs/runtime-pools.md tests/test_container_exec.py tests/integration/test_container_pools.py tests/integration/fixtures/fake_pi.py tests/integration/fixtures/fake_provider.py tests/integration/fixtures/Dockerfile.worker
git commit -m "M9a-6 document and verify runtime container pools"
```

### Task 24: Run the final 130-contract invariant audit and repository verification

**Files:**
- Create: `tests/test_m9a_contract_audit.py`
- Modify only if a failing invariant identifies a concrete omission: files already owned by Tasks 1–23.

**Interfaces:**
- Consumes: all M9a modules and tests.
- Produces: executable static/dynamic audit for acceptance items 1–130 and final evidence that the repository, Go protocol, shell/Compose, UI, and opt-in Docker path are green.

- [ ] **Step 1: Add the final cross-cutting audit tests**

```python
# tests/test_m9a_contract_audit.py
from pathlib import Path
from dswarm.core.events import EventType

PRODUCTION_SHELL_ENTRY_FILES = (
    Path("dswarm/swarm/swarm.py"),
    Path("dswarm/swarm/worker_runtime_mixin.py"),
    Path("dswarm/swarm/runtime.py"),
    Path("dswarm/swarm/review_flow.py"),
    Path("dswarm/solver/btw.py"),
    Path("apps/web/routes/btw.py"),
)


def test_no_production_host_or_legacy_fallback_imports():
    forbidden = ("ensure_container(", "teardown_container(", "resolve_engine_bin(\"pi\")")
    for path in PRODUCTION_SHELL_ENTRY_FILES:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_m9a_adds_no_canonical_runtime_event_type():
    assert not any(name.startswith("RUNTIME_") for name in EventType.__members__)
    for path in (
        Path("dswarm/solver/container_pool.py"),
        Path("dswarm/solver/runtime_diagnostics.py"),
        Path("dswarm/solver/runtime_cleanup.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "add_evidence(" not in text
        assert "append_event(" not in text
```

Add assertions linking every acceptance number to at least one named deterministic test; no production import of test fixtures; no raw secret/path fields in public snapshots; all real spawn operation kinds covered; `RUN_FINISHED` has no close side effect; runtime diagnostics do not enter graph/Reason; and M5 usage/event schema remains unchanged.

- [ ] **Step 2: Run focused language and contract checks**

Run: `uv run pytest -q tests/test_m9a_contract_audit.py tests/test_m9a_invariants.py tests/test_runtime_spawn_paths.py tests/test_runtime_reopen_barrier.py`

Run: `go test ./cmd/runtime-agent/...`

Run: `npm --prefix apps/web/ui test -- lib/events.test.ts`

Expected: all exit 0.

- [ ] **Step 3: Run shell, Compose, formatting, and full Python verification**

Run: `bash -n ./run.sh`

Run: `docker compose config`

Run: `git diff --check`

Run: `uv run pytest -q`

Expected: all exit 0. On a Windows checkout, do not run `init.sh` through WSL `/mnt/c`; the native PowerShell `uv run pytest -q` result is the authoritative equivalent required by `AGENTS.md`.

- [ ] **Step 4: Run the final opt-in Docker proof**

Run: `$env:DSWARM_RUN_DOCKER_TESTS='1'; uv run pytest -q tests/integration/test_container_pools.py`

Expected: PASS with fake Pi/provider, two concurrent Pools, Probe-before-Worker, separated M5 attribution, stale-link rejection, exact labels/mounts/network, and no managed-container residue.

- [ ] **Step 5: Commit the executable acceptance audit**

```powershell
git add tests/test_m9a_contract_audit.py
git commit -m "test: audit M9a runtime pool invariants"
```

---

## Spec and Acceptance Coverage Map

| Design requirement | Implemented and tested by |
|---|---|
| §§1–4 decisions, RuntimePolicy, PoolKey and identity | Tasks 1–2, 6–7 |
| §5 frozen snapshot and configuration-change semantics | Tasks 3–4, 13, 16 |
| §6 secret, credential and mount boundaries | Tasks 3, 5, 8–12, 20, 23–24 |
| §§7–8 component architecture and RCP v2 | Tasks 6–12 |
| §§9–10 process capacity and UID/GID | Tasks 3, 9, 14–15, 23 |
| §11 real tool-disabled Probe and M5 accounting | Tasks 10–11, 23–24 |
| §12 all true dispatch paths | Tasks 13–17 |
| §13 Docker-first Web/TUI and local-dev gate | Tasks 18–20 |
| §14 failure isolation, recovery and frozen failover | Tasks 9, 11–12, 16 |
| §§15–16 reopen barrier and lifecycle teardown | Tasks 12, 17, 22–24 |
| §17 private observability and safe API | Tasks 8, 21, 24 |
| §18 forward-only/legacy boundary | Tasks 17, 22–24 |
| §§19–20 code boundary and six implementation phases | Tasks 1–24 and their phase gates |
| Acceptance 1–22: policy, identity, snapshot | Tasks 1–4, 13 |
| Acceptance 23–40: UID/GID, mounts, credentials | Tasks 3, 5, 8, 12, 23–24 |
| Acceptance 41–54: RCP v2 | Tasks 6–7, 12, 23 |
| Acceptance 55–80: pool lifecycle, Probe and cleanup | Tasks 8–12 |
| Acceptance 81–97: all Swarm spawn paths and isolation | Tasks 13–17 |
| Acceptance 98–110: reopen, cleanup and close ownership | Tasks 12, 17, 22 |
| Acceptance 111–123: Web/TUI, observability and security | Tasks 18–23 |
| Acceptance 124–129: real Docker fake-Pi/provider proof | Tasks 23–24 |
| Acceptance 130: full regressions and protected invariants | Task 24 |

## Execution Checkpoints

After each task, run its focused tests and commit only the listed files. At the end of each M9a phase, run `git diff --check` and `uv run pytest -q`. M9a-2 also runs Go tests; M9a-5 and M9a-6 also run `bash -n ./run.sh` and `docker compose config`; M9a-6 runs the explicitly enabled fake Docker integration suite.

Do not start the next implementation phase while the prior phase is red. Do not amend a prior phase's commit with unrelated work; use a new fix commit if verification exposes a cross-phase defect.
