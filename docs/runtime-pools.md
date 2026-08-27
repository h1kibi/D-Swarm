# Runtime Pool Operations

M9a uses a Docker-first, run-scoped runtime model. A run owns one immutable
runtime snapshot and one long-lived container for every `(run_id, pool_id)`.
Pool identity is frozen when the run is created; changing a profile, image,
network, UID/GID, or credential binding does not mutate an existing run.

## Operator defaults

- Web and real TUI control planes use `RuntimePolicy(mode="docker")`.
- Each PoolKey has exactly one active container generation at a time.
- A pool is not ready until the real tool-disabled Pi probe succeeds.
- `RUN_FINISHED` records solve lifecycle only; it does **not** tear down runtime
  pools. Delete/archive, process shutdown, TUI exit, or explicit disposal closes
  every pool.
- Runtime diagnostics are private sidecars and sanitized API views. They are not
  SharedGraph facts, EventBus runtime facts, Reason prompt input, or provenance.

## Start Docker-first control planes

Local Web/TUI development can run the control plane natively while workers are
still sibling containers:

```bash
./run.sh web
./run.sh tui --swarm
```

For a fully containerized control plane, set an absolute host data root and use
Compose:

```bash
DSWARM_HOST_DATA_ROOT=/opt/dswarm/data \
DSWARM_WEB_PASSWORD='use-a-secret-password' \
docker compose up --build
```

The Compose deployment deliberately mounts the Docker socket only into the
trusted control plane. Worker containers never receive that socket.

## Runtime and security contract

Each managed worker container has exact `com.dswarm.*` labels for run, pool,
instance, and generation identity. The snapshot records the immutable image
identity, normalized network/resources, numeric UID/GID, protocol/features,
PoolKey, and limits. It never stores secret bytes, host HOME, `.pi`, the full
credential store, or unsanitized host paths.

Workers receive only their private workspace/session/HOME and the selected
credential projection. They do not receive another run, another pool's
credentials, the coordinator `.runtime` directory, reference/solution files,
or the host Pi installation. All images in one run must prove the same numeric
UID/GID and contain `kali`; `chmod 777` is not a repair strategy.

## Local development escape hatch

Host-local workers require **both** gates:

```bash
DSWARM_ALLOW_LOCAL_WORKERS=1
# and an explicit launcher/caller local-dev flag
```

The production default remains Docker. The old one-container-per-run
`container_exec` constructor is a named, policy-gated compatibility facade for
low-level tests and explicitly authorized local development only. It is not a
rollback path for production dispatch and must not be selected by ambient
backend environment variables.

## Failure and recovery behavior

Failures are classified as infrastructure, identity, auth, configuration,
capacity, or worker failures. A failed pool is isolated from other pools. At
most one generation rebuild is attempted per failure episode; identity,
authentication, and configuration failures do not trigger paid probe loops.
Probe/accounting failures fail closed and do not invoke host `pi`.

## Reopen cleanup barrier

Process-level run reopen first proves that every stale runtime candidate has
been cleaned up. Candidate matching requires the exact managed labels and the
private runtime identity; legacy name-only evidence is rejected. All candidate
pools are inspected even when one cleanup fails, and reopen is rejected unless
all proof dimensions are complete. No probe, Reason cycle, or worker dispatch
may start before the barrier succeeds.

## Observability

Use the sanitized runtime-pools view exposed by the control plane for pool
state, generation, capacity, active workers, and typed failure codes. Private
state/diagnostic files may contain operational identity needed for cleanup, but
must not be projected as graph evidence or raw credentials.

## Forward-only upgrade and rollback

Runtime snapshot and protocol changes are forward-only. Upgrade in this order:

1. Stop new dispatch and let active workers finish or cancel them.
2. Close all pools with the new binary.
3. Verify no containers carrying `com.dswarm.managed` remain for the run.
4. Back up the run/session data and private runtime metadata.
5. Start the new binary and reopen only after the cleanup barrier passes.

If a release must be rolled back, stop dispatch, close all pools with the new
binary, verify the managed-label query is empty, and only then start the old
binary. Never start an old binary while a newer managed container or runtime
protocol link is still alive.

## Integration proof

The fake-Pi/fake-provider Docker integration is deliberately opt-in and never
reads real provider credentials:

```powershell
$env:DSWARM_RUN_DOCKER_TESTS = '1'
uv run pytest -q tests/integration/test_container_pools.py
```

Without the flag, the suite is skipped. The test creates two isolated pools,
checks probe-before-worker ordering, labels, mounts, network, concurrent
worker execution, and residue-free cleanup.
