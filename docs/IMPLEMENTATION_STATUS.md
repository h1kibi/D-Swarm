# D-Swarm Implementation Status

> Updated: 2026-09-05. This is the current execution status for docs/12–15.
> Historical research and session reports remain under `docs/archive/` or their
> original filenames; this file is the short operational index.

## Product focus

D-Swarm is a personal CTF-solving automation system. Priority is solve efficiency
and correctness: parallel worker exploration, useful cross-run context, low-noise
operator control, and zero false flag acceptance. Production provenance, the shared
evidence graph, and the usage ledger are not weakened for convenience.

## Status matrix

| Area | Status | Evidence | Remaining |
|---|---|---|---|
| Provider/model gateway | Implemented baseline | `apps/web/run_manager.py`, `dswarm/solver/modelgateway.py`, `tests/test_modelgateway.py` | Unify resolver semantics and improve diagnostics |
| Secret-free binding snapshots | Implemented | `public_binding`, run summary and observability tests | Add broader leak regression corpus |
| Target → Direction → Run | Implemented v1 | `apps/web/targets/`, `tests/test_web_targets.py` | Lifecycle/recovery/concurrency audit |
| Run observability | Implemented v1 | `apps/web/run_observability.py`, `tests/test_run_observability.py` | More failure/restart cases |
| Target hints | Implemented v1 | Target launch context and hint tests | Keep guidance/evidence visibly separate |
| Verified fact inheritance | Implemented v1, manual/selective only | `Run.inherited_verified_facts`, Target projection and tests | stale/invalidation/source-unavailable states |
| UI Template A | Implemented baseline | Target workspace, Run inspector, Vitest | Responsive/runtime smoke verification |
| UI Templates B/C | Planned | `docs/14-target-run-ui-visual-templates.md` | Do not build until evidence/review workflows require them |
| Provider fallback | Not implemented by design | Explicitly forbidden as implicit behavior | Separate RFC with policy, audit, cost and replay semantics |
| Automatic fact inheritance | Not implemented by design | Manual selection remains default | Decide only after stale/invalidation model |
| Cross-target graph merge | Not implemented | Boundary preserved | No current plan |
| Advisor production wiring | No-Go | `docs/advisor-status-assessment.md` | Keep offline-only |

## P1 — current engineering queue

1. Finish Target/Direction/Run lifecycle tests: failed launch, restart recovery,
   archive/delete, stale relationships, and duplicate attachment.
2. Keep all role bindings on the same configured → frozen → effective resolution
   contract; a Run snapshot wins after launch.
3. Audit silent exception handlers in scheduler, runtime, credential, graph, and
   evidence/ledger write paths. Best-effort cleanup may remain silent; durable
   state failures must be observable and fail closed.
4. Run the full Python suite and frontend unit/build checks before each milestone.

## P2 — next efficiency improvements

1. Add inherited-fact freshness and invalidation metadata without copying graph
   events or treating context as current-run evidence.
2. Add a broader secret-leak regression test over public Run, Target, nested Run,
   observability, SSE and error projections.
3. Add evidence navigation from inherited facts to the source Run/event/artifact.
4. Add deterministic measurements for solve throughput: time-to-first-valid-flag,
   worker cost, duplicate work, intent completion, and useful-fact reuse.

## Verification contract

```powershell
uv run pytest -q
cd apps/web/ui
npm test
npm run build
```

A capability is not considered complete from a model claim or UI state alone. For
solver claims, retain a black-box trace showing the accepted flag in real worker
stdout/stderr/artifact output and passing the hard provenance gate.
