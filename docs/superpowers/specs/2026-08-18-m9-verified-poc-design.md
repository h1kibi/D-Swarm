# M9 Verified-PoC Gate Design

**Date:** 2026-08-18  
**Status:** Approved for implementation  
**Scope:** M9 OSS legacy item 1 — pentest-only reproducible Proof-of-Concept verification  
**Out of scope:** flag gate changes, CTF behavior changes, scope audit, cleanup registry, runtime-pool lifecycle changes, M5 accounting-schema changes

---

## 1. Decision summary

A pentest PoC becomes **verified** only after D-Swarm reruns the already-registered `POC_SAVE.entry_command` inside the current run's Docker runtime pool and observes the registered `indicator` in real verifier stdout, stderr, or a verifier-created artifact referenced by that output. The verifier is not an arbitrary-command endpoint: it may execute only the immutable command that was registered with that PoC.

Verification is represented by append-only graph events plus replayable projections. It is deliberately distinct from flag acceptance and fact verification:

- `gate.py` and `CliSolver._flag_ok()` remain byte-for-byte semantically unchanged.
- A verified PoC is not a verified CTF flag and never bypasses the hard provenance gate.
- A verified PoC does not change `fact_effective`, fact confidence, or `verified_evidence()`.
- The feature is enabled only when `Challenge.mode == "pentest"`; CTF output and dispatch behavior remain unchanged.

## 2. Current facts and gap

The existing implementation already provides useful substrate:

1. `CliSolver._handle_poc_save()` validates that a saved path stays inside the worker working directory, sanitizes saved bytes, materializes a CAS artifact, and calls `SharedGraph.save_poc()`.
2. A PoC currently stores `poc_id`, `intent_id`, artifact ID, `entry_command`, status, and note. It can be claimed or concluded, but it has no reproduction indicator or verification lifecycle.
3. `CliSolver._provenance_corpus()` contains live raw tool output and is already the authoritative searchable corpus for flag provenance.
4. M9a provides Docker-first run-scoped runtime pools and per-worker leases. Real worker execution has no permitted production host-Pi fallback.
5. Review Flow can emit append-only `review_finding` events, but review findings currently have no independent verified/unverified lifecycle.

The missing capability is a reproducible, auditable proof that a saved pentest PoC actually produces its claimed observable result.

## 3. Goals and non-goals

### Goals

- Store a bounded, auditable reproduction indicator for a saved pentest PoC.
- Execute only that PoC's registered `entry_command` inside the current run's Docker pool.
- Require actual verifier output/artifact provenance for successful verification.
- Produce append-only start/success/failure events, deterministic replay projections, and blackboard deltas.
- Allow Review Flow to dispatch a focused verifier intent for an eligible high-severity pentest finding that cites a PoC.
- Preserve worker/runtime isolation and correctly report all verifier execution outcomes.

### Non-goals

- No arbitrary shell command supplied after PoC registration.
- No execution on the host, no local-dev fallback, and no Docker socket exposure to workers.
- No automatic exploit escalation, no repeated retry loop, no flag submission, and no network policy change.
- No changes to the hardcoded flag gate, anti-laundering checks, evidence-graph append-only invariant, or M5 canonical usage schema.
- No scope authorization decision here; M9 scope audit is a later independent item.
- No claim that a successful indicator proves impact beyond the exact registered observable result.

## 4. Data and event contract

### 4.1 Reproduction registration

`POC_SAVE` remains backward compatible. A second explicit marker registers the indicator after the PoC artifact has been saved:

```text
POC_REPRO=<saved-path>|<indicator>
```

The command is never duplicated or supplied by this marker: it is the `entry_command` already registered by `POC_SAVE`. The worker must use the same normalized saved path. A reproduction marker that arrives before its matching `POC_SAVE` is held only in a bounded in-memory pending map for that worker stream; it is discarded at worker completion if no matching PoC exists.

Validation rules:

- only pentest-mode workers may register it;
- saved path resolves beneath the current worker directory using the existing path confinement rules;
- `indicator` is UTF-8 text after control-character removal, one logical line, non-empty, and at most 512 characters / 2048 UTF-8 bytes;
- the indicator is never a flag-format secret and never persisted if it matches the existing secret/flag redaction policy;
- one PoC has at most one registered reproduction identity: duplicate identical registrations are idempotent; a conflicting second indicator appends a rejected registration event and does not replace the first;
- the command, indicator, and PoC artifact digest form the immutable reproduction identity.

Canonical registration event:

```json
{
  "kind": "poc_reproduction_registered",
  "payload": {
    "poc_id": "poc-…",
    "intent_id": "I-…",
    "artifact_id": "sha256…",
    "command": "<the existing POC_SAVE entry_command>",
    "indicator": "<bounded observable string>",
    "reproduction_id": "poc-repro::<artifact_id>::<sha256(command+indicator)>"
  }
}
```

`pocs` remains a mutable projection only. A new replayable `poc_reproductions` projection may cache `reproduction_id`, command, indicator, registration sequence, and current verification state, but no canonical event row is updated or deleted.

### 4.2 Verification lifecycle

For one reproduction identity, exactly one active verifier may run at a time. The following immutable events are emitted in order:

1. `poc_verification_started` — includes `verification_id`, `reproduction_id`, `poc_id`, source finding ID (if any), verifier intent ID, worker instance ID, and pool identity.
2. terminal one of:
   - `poc_verified` — includes exit code, bounded elapsed time, indicator digest, provenance artifact IDs, and the observed provenance location; or
   - `poc_verification_failed` — reason from the closed enum below, exit code when available, and bounded sanitized diagnostics.
3. Optional `review_finding_verified` — emitted only when the verifier was dispatched from one eligible review finding and the PoC terminal event is `poc_verified`.

Failure reasons are a closed enum:

```text
missing_reproduction | docker_runtime_unavailable | lease_unavailable |
artifact_unavailable | command_rejected | timed_out | execution_error |
nonzero_exit | indicator_not_observed | provenance_unavailable | cancelled
```

An event whose canonical append fails is not considered successful. The run reports an infrastructure failure using existing runtime diagnostic paths; it must never emit an online "verified" blackboard delta without a durable `poc_verified` graph event.

### 4.3 Review-finding relationship

A high-severity pentest review finding is eligible only when all are true:

- `severity == "blocker"` under the existing normalized review severity vocabulary;
- it cites exactly one PoC through an explicit `poc_id` field;
- that PoC has an accepted reproduction registration;
- the finding is not already linked to a terminal verification for that reproduction identity.

Review Flow creates a `worker_class="verifier"` intent with a fixed goal describing the PoC ID and expected indicator. It does not put a shell command in the LLM prompt as an instruction to invent or alter. The runtime execution layer resolves the command from the graph by `reproduction_id`.

`review_finding_verified` is a review-status event only. It does not make a fact verified, does not change `verified_evidence()`, and does not alter flag/finding confidence semantics. Findings without a verified PoC remain visible but are marked unverified in pentest reporting.

## 5. Execution and provenance contract

### 5.1 Docker-only verifier execution

The verifier resolves the run's frozen `RuntimePolicy`, `RuntimeSnapshot`, pool manager, and a per-worker runtime lease exactly as real workers do. It runs only in a `RuntimePolicy(mode="docker_first")` pool.

Execution has these preconditions:

1. `Challenge.mode == "pentest"`.
2. A durable reproduction registration exists and its artifact is materializable into the verifier workspace.
3. The existing `entry_command` is a non-empty, bounded single command string that passes command syntax validation.
4. The current run has a healthy compatible container pool and a verifier lease.

The implementation must route the command through a dedicated container-only execution adapter. It must not call host `subprocess`, `resolve_engine_bin("pi")`, the legacy container facade, or any local fallback. The adapter invokes a fixed shell wrapper inside the leased container with the verifier workspace as CWD, `stdin` closed, bounded timeout, and the same runtime identity diagnostics as a worker operation.

The original artifact is staged read-only; verifier output and any generated artifacts are written to an isolated verifier directory. The execution environment never receives host HOME, host `.pi`, Docker socket, raw account stores, or another run/pool credential projection.

### 5.2 Success criterion

Success requires all of:

1. command execution reaches a known terminal state without timeout/cancellation;
2. exit code is exactly zero;
3. `indicator` occurs verbatim in the verifier's full stdout/stderr corpus, or in a CAS artifact referenced by that output;
4. the matching output/artifact text is captured before truncation and made available to the verifier provenance checker;
5. `poc_verified` is durably appended before its blackboard/UI delta is emitted.

An indicator merely appearing in the PoC source, model prose, prompt, note, review finding, or graph payload is never enough. The comparison uses exact substring matching after registration normalization; it does not regex-match or infer semantically equivalent output.

### 5.3 Timeouts, cancellation, and concurrency

- Default verifier timeout is a bounded pentest configuration value (initially 120 seconds); it is capped at 600 seconds.
- There is no automatic retry. A later new verifier intent may be created only by explicit Review Flow/operator action and has a new `verification_id`.
- A cancellation appends `poc_verification_failed(reason="cancelled")` when the graph remains writable, then re-raises cancellation to preserve swarm lifecycle behavior.
- Per reproduction identity, a graph-backed activity lease prevents concurrent verification; lease loss returns `lease_unavailable`, never a second run.
- Failure never blocks coordinator finalization and never changes the PoC body or registration.

## 6. Interfaces and ownership

### 6.1 `CliSolver`

Owns parsing of `POC_SAVE` and `POC_REPRO` markers, normalizes input at the worker boundary, materializes only the existing PoC artifact, and appends the registration event through `SharedGraph`. It must not itself directly invoke a host command.

### 6.2 `SharedGraph`

Owns append-only canonical PoC reproduction/verification events, deterministic projections, deduplication, active verification activity claims, and replay read APIs. Required read APIs are conceptually:

```python
get_poc_reproduction(poc_id: str) -> dict | None
poc_verification_status(poc_id: str) -> dict | None
eligible_poc_verification(finding_id: str) -> dict | None
```

Returned data are immutable copies. Projection changes are derived solely from events; reopening an existing database treats old PoCs without registration as `missing_reproduction`, not implicitly verified.

### 6.3 Review Flow and scheduler

Own Review-Flow eligibility detection and creation of a verifier intent. Scheduler routes `worker_class="verifier"` through the ordinary/review lane policy without altering existing priority/lane semantics. A verifier intent uses a stable `reproduction_id`, not free-form shell text, as its executable authority.

### 6.4 Container verifier adapter

Owns only the controlled container invocation and result normalization. It receives an immutable resolved command/indicator/artifact staging record and a runtime lease. It returns a structured result with raw output corpus, artifact references, terminal status, exit code, elapsed time, and sanitized error detail. It does not write graph events itself.

### 6.5 Reporting and blackboard

The bridge emits compact deltas for reproduction registration and verification terminal events. UI/report consumers may display PoC verification state, but must not present it as a CTF flag or a verified fact. Raw command output, indicator, secrets, and full artifact paths are never exposed in public event payloads.

## 7. Security and compatibility invariants

1. The hardcoded flag gate remains unchanged.
2. All canonical graph rows remain append-only; projection tables/views are rebuildable from events.
3. Only a command registered by `POC_SAVE` is executable; no post-registration free-form command field exists.
4. Only Docker-first runtime pools execute verifier commands in production.
5. No verifier result can become success based on model text, stored PoC source, review summary, or an indicator embedded in its own event payload.
6. `poc_verified` is not a fact promotion and cannot enter `verified_evidence()`.
7. CTF mode sees no marker parsing, event emission, intent creation, command execution, prompt delta, or output change from this feature.
8. All terminal event payload text is bounded, sanitized, and free of flag-like/secret literals by the existing redaction policy.
9. Verifier execution uses existing M5 worker/runtime accounting integration; it does not define a second usage ledger.
10. Runtime failure is a runtime diagnostic, not a fact or dead-end.

## 8. Test matrix

The implementation must add deterministic tests before production code. At minimum:

1. CTF mode ignores `POC_REPRO` and retains existing POC behavior byte-for-byte.
2. Valid pentest `POC_SAVE` + `POC_REPRO` registers one reproduction event and replay projection.
3. Path escape, empty/oversized/control-character/flag-like indicator is rejected without registration.
4. Duplicate identical registration is idempotent; conflicting registration is rejected and leaves the original immutable.
5. A finding without exactly one eligible PoC cannot create a verifier intent.
6. An eligible blocker finding creates one verifier intent with `reproduction_id` and no free-form command.
7. The verifier adapter receives only the registered command and a Docker runtime lease.
8. A test double proving host subprocess/local fallback is never called on the production verifier path.
9. Zero exit plus indicator in captured stdout appends `poc_verified`.
10. Zero exit plus indicator only in source/note/prompt does not verify.
11. Non-zero exit, timeout, cancellation, runtime failure, missing artifact, and missing indicator each append the correct terminal failure reason.
12. A terminal graph append failure never emits a verified UI/blackboard delta.
13. Concurrent verifier attempts for the same reproduction run exactly once.
14. Cold replay produces the same reproduction and verification projection as online operation.
15. `poc_verified` never changes `verified_evidence()` or flag outcome.
16. `review_finding_verified` appears only after a durable `poc_verified` for its linked reproduction.
17. Public delta/report payloads omit raw output, full command text when classified sensitive, raw indicator when classified sensitive, CAS local source paths, and credentials.
18. M9a Docker integration test uses a fake PoC image/command and proves execution occurs inside the run pool.
19. Existing provenance gate, append-only event tests, pentest-mode tests, M5 tests, and full `uv run pytest -q` remain green.

## 9. Rollout and rollback

The feature ships disabled by behavior: no registered `POC_REPRO` means no verifier intent and no new execution. Existing databases remain readable; old PoCs are simply reproduction-unregistered. There is no destructive migration and no backfill that infers indicators from notes or artifact contents.

Rollback is forward-only: disable Review Flow's PoC-verifier trigger and stop accepting new `POC_REPRO` markers. Previously appended events remain historical audit records, and projections may continue to render them read-only. Runtime pools are not torn down by this feature; normal M9a lifecycle ownership remains authoritative.

## 10. Completion criteria

The item is complete only when all tests in Section 8 pass, `uv run pytest -q` is green, an opt-in fake-Docker integration proves the command stays inside the run pool, and code review confirms that `gate.py`, anti-laundering behavior, append-only events, M5 ledger schema, and host-local production execution paths were not weakened or expanded.

