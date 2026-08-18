# M9 Verified-PoC Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pentest-only, append-only Verified-PoC lifecycle that registers a bounded reproduction indicator, reruns only the immutable `POC_SAVE.entry_command` inside the current run's Docker pool, and accepts verification only from real verifier provenance.

**Architecture:** Extend the existing PoC event/projection path without changing the flag gate, fact projection, or M5 ledger. `CliSolver` parses and registers `POC_REPRO`; `SharedGraph` owns canonical reproduction/verification events and replayable projections; Review Flow creates a stable verifier intent; a container-only adapter executes the registered command through the run-scoped runtime lease; terminal graph durability precedes any blackboard/UI delta.

**Tech Stack:** Python 3.13, asyncio, SQLite event-sourced `SharedGraph`, existing `EventBus`, M9a `RuntimePolicy`/run-scoped Docker pool and `WorkerRuntimeLease`, pytest/`uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-08-18-m9-verified-poc-design.md`

## Global Constraints

- `gate.py` and the semantic behavior of `CliSolver._flag_ok()`/anti-laundering remain unchanged.
- `POC_REPRO` is accepted only in `Challenge.mode == "pentest"`; CTF mode must retain existing behavior and output.
- A verifier may execute only the immutable `entry_command` stored by `POC_SAVE`; there is no post-registration command parameter.
- Production verifier execution requires the current run's Docker-first runtime pool and a valid runtime lease; no host subprocess or local fallback.
- Success requires exit code `0` plus exact indicator occurrence in verifier stdout/stderr or a verifier-created artifact referenced by that output; model text, source, notes, prompts, and graph payloads never count.
- Canonical graph data is append-only and replayable. No direct update/delete of canonical event rows.
- Durable terminal event append must succeed before emitting a verified blackboard/UI delta.
- The feature does not modify `fact_effective`, `verified_evidence()`, fact confidence, flag outcomes, or the M5 canonical usage schema; use existing accounting injection only.
- All new public/error payload text is bounded, sanitized, and redacted; raw command output, credentials, and unsafe local paths never enter public deltas.
- Implement test-first: each task writes a failing deterministic test, observes the expected failure, implements the minimum behavior, reruns focused tests, then commits.

---

## Task 1: Freeze reproduction data models, event vocabulary, and replay projection

**Files:**
- Modify: `dswarm/core/events.py` (new event types and payload documentation following existing conventions)
- Modify: `dswarm/swarm/shared_graph.py` (canonical event constants, schema/projection setup, replay fold, registration/status APIs)
- Create: `dswarm/swarm/poc_verification.py` (bounded normalization, `ReproductionRegistration`, `VerificationResult`, closed failure enum, deterministic IDs, public sanitization helpers)
- Test: `tests/test_verified_poc_graph.py`

**Interfaces:**
- `normalize_reproduction_indicator(value: str) -> str` rejects empty, control/newline, oversized, flag-like or redacted values without persisting raw input.
- `reproduction_id_for(*, artifact_id: str, command: str, indicator: str) -> str` is stable and digest-based.
- `SharedGraph.register_poc_reproduction(...) -> dict` is idempotent for an identical identity and rejects conflicts without replacing the first registration.
- `SharedGraph.get_poc_reproduction(poc_id: str) -> dict | None` and `poc_verification_status(poc_id: str) -> dict | None` return immutable copies.
- `SharedGraph.begin_poc_verification(...) -> dict | None` provides a graph-backed per-reproduction activity lease; an active lease prevents a second start.
- `SharedGraph.append_poc_verification_terminal(...) -> int` appends exactly one terminal event and folds it into the projection.
- Replay folds `poc_reproduction_registered`, `poc_verification_started`, `poc_verified`, and `poc_verification_failed`; `review_finding_verified` is status-only and never changes fact projections.

- [ ] **Step 1: Write failing tests** for normalization, deterministic identity, valid registration, duplicate idempotency, conflicting registration, replay equivalence, activity lease exclusivity, and terminal-state transitions.
- [ ] **Step 2: Run focused tests**
  - Run: `uv run pytest -q tests/test_verified_poc_graph.py`
  - Expected: FAIL because the new event/model/API surface is absent.
- [ ] **Step 3: Implement the smallest model/event/projection surface**; use existing `_append`/event replay patterns and preserve `pocs` as a derived mutable projection only.
- [ ] **Step 4: Run focused tests again** and add regression assertions that `verified_evidence()` and `fact_effective` are unchanged after `poc_verified`.
- [ ] **Step 5: Commit**
  - `git add dswarm/core/events.py dswarm/swarm/shared_graph.py dswarm/swarm/poc_verification.py tests/test_verified_poc_graph.py`
  - `git commit -m "feat: add verified PoC graph lifecycle"`

## Task 2: Parse and register `POC_REPRO` at the pentest worker boundary

**Files:**
- Modify: `dswarm/solver/cli_solver.py` (marker extraction, pending map, worker-completion cleanup, registration call)
- Modify: `dswarm/swarm/blackboard_bridge.py` (compact registration/terminal delta mapping)
- Modify: `skills/dswarm-blackboard/SKILL.md` and `skills/dswarm-blackboard/blackboard.py` (document/read-only display of PoC reproduction state without flag/fact semantics)
- Test: `tests/test_verified_poc_markers.py`
- Test: extend existing PoC/CLI solver tests where marker parsing is already covered

**Interfaces:**
- `_extract_poc_repros(text: str) -> Iterable[tuple[str, str]]` only participates in pentest mode and never exposes a command field.
- A pending registration is keyed by the existing normalized saved path and bounded per worker stream; unresolved entries are discarded at worker completion.
- Registration uses the `entry_command` already stored by `POC_SAVE`; direct host execution is impossible from this path.
- Duplicate identical markers are no-op/idempotent; conflicts append a rejected registration event and preserve the original.
- Blackboard deltas include only stable IDs, status/reason, digests, and bounded display fields; no raw output, credentials, full unsafe paths, or flag-like strings.

- [ ] **Step 1: Write failing tests** for CTF ignore behavior, valid pentest save-then-repro, repro-before-save pending resolution, unresolved pending discard, path confinement, invalid indicator rejection, duplicate idempotency, and conflicting indicator rejection.
- [ ] **Step 2: Run focused marker tests** and verify failure.
- [ ] **Step 3: Implement marker parsing and registration** by extending the existing `_extract_poc_saves` flow, preserving all existing `POC_SAVE` semantics and redaction rules.
- [ ] **Step 4: Update blackboard documentation/bridge and rerun focused plus existing PoC tests.** Confirm the event never becomes a flag/fact delta.
- [ ] **Step 5: Commit**
  - `git add dswarm/solver/cli_solver.py dswarm/swarm/blackboard_bridge.py skills/dswarm-blackboard tests/test_verified_poc_markers.py`
  - `git commit -m "feat: register pentest PoC reproduction indicators"`

## Task 3: Add Review Flow eligibility and verifier intent construction

**Files:**
- Modify: `dswarm/swarm/review_flow.py` (eligible finding lookup and one-shot verifier intent creation)
- Modify: `dswarm/solver/reason.py` only if the existing typed `Intent` needs a stable reproduction reference field; do not alter direction/gate semantics
- Modify: `dswarm/swarm/agents.py` (append positional-compatible `reproduction_id` and `source_finding_id` fields to DispatchDecision)
- Modify: `dswarm/swarm/reason_scheduler.py` and `dswarm/swarm/swarm.py` (carry the structured verifier metadata through registration, dispatch, and worker creation)
- Test: `tests/test_verified_poc_review_flow.py`

**Interfaces:**
- `eligible_poc_verification(finding_id: str) -> dict | None` requires pentest mode, normalized `severity == "blocker"`, exactly one explicit `poc_id`, an accepted reproduction registration, and no terminal verification for that reproduction.
- Verifier intent has `worker_class="verifier"`, stable `reproduction_id`, source `finding_id`, and a fixed bounded goal; it contains no free-form shell command.
- Repeated review events or already-terminal status produce no duplicate active verifier intent.
- Scheduler routes verifier through existing review/ordinary lane policy; no priority or gate behavior changes.

- [ ] **Step 1: Write failing tests** for missing/ambiguous/ineligible PoCs, non-blocker findings, already-terminal verification, one eligible finding, duplicate triggering, and command-free intent payloads.
- [ ] **Step 2: Run focused tests** and confirm expected failure.
- [ ] **Step 3: Implement eligibility and intent creation** using graph read APIs from Task 1 and existing Review Flow marker handling.
- [ ] **Step 4: Run focused review-flow/scheduler tests** and verify CTF and non-verifier paths remain unchanged.
- [ ] **Step 5: Commit**
  - `git add dswarm/swarm/review_flow.py dswarm/solver/reason.py dswarm/swarm/agents.py dswarm/swarm/reason_scheduler.py dswarm/swarm/swarm.py tests/test_verified_poc_review_flow.py`
  - `git commit -m "feat: dispatch eligible PoC verifier intents"`

## Task 4: Implement the Docker-only verifier adapter over the run-scoped lease

**Files:**
- Create: `dswarm/solver/poc_verifier.py` (immutable resolved registration, controlled adapter, result normalization)
- Modify: `dswarm/solver/container_runtime.py` (add the narrow registered-command execution seam on `ContainerRuntimeExecutor`; do not add host subprocess fallback)
- Modify: `dswarm/solver/runtime_policy.py` only if a verifier-specific Docker-first assertion is needed; preserve existing policy semantics
- Modify: `dswarm/swarm/worker_runtime_mixin.py`/runtime factory only at the existing lease injection seam if the verifier needs the current pool lease
- Test: `tests/test_verified_poc_verifier.py`
- Test: `tests/test_verified_poc_docker_integration.py` (fake Docker/runtime pool, opt-in if real Docker is unavailable)

**Interfaces:**
- `ContainerPocVerifier.verify(registration: ResolvedPocRegistration, lease: WorkerRuntimeLease, *, timeout: float) -> VerifierExecutionResult`.
- `ResolvedPocRegistration` contains only the graph-resolved `poc_id`, `reproduction_id`, artifact staging reference, immutable registered command, and normalized indicator.
- The adapter accepts no caller-provided command and no local executor; it invokes only the registered command through the current container executor/lease.
- `VerifierExecutionResult` distinguishes `verified`, `nonzero_exit`, `timed_out`, `cancelled`, `execution_error`, `artifact_unavailable`, `provenance_unavailable`, and `indicator_not_observed`; unknown/truncated output never upgrades to verified.
- Verifier provenance corpus is only runtime stdout/stderr and verifier-created artifact output; source/note/prompt/event payload text is excluded.
- Existing M5 usage context/writer is passed through the existing runtime/worker accounting seam; no second ledger is introduced.

- [ ] **Step 1: Write failing tests** proving command identity enforcement, Docker lease requirement, host/local fallback rejection, artifact staging, exact indicator matching, zero-exit success, source-only false positive rejection, and each terminal failure mapping.
- [ ] **Step 2: Run focused verifier tests** and confirm failure.
- [ ] **Step 3: Implement the narrow container adapter** using the existing per-run PoolKey long-lived container and lease; ensure timeout/cancellation releases the lease and no untracked host task remains.
- [ ] **Step 4: Run unit tests and the fake-Docker integration test**; skip only the real Docker test when Docker is unavailable, while keeping the fake pool test mandatory.
- [ ] **Step 5: Commit**
  - `git add dswarm/solver/poc_verifier.py dswarm/solver/container_runtime.py dswarm/solver/runtime_policy.py dswarm/swarm/worker_runtime_mixin.py tests/test_verified_poc_verifier.py tests/test_verified_poc_docker_integration.py`
  - `git commit -m "feat: run PoC verification in Docker runtime pools"`

## Task 5: Orchestrate durable verification and terminal reporting

**Files:**
- Create: `dswarm/swarm/poc_verification_runtime.py` (orchestration, durable lifecycle, and terminal classification)
- Modify: `dswarm/swarm/reason_scheduler.py` (invoke the orchestration module for `worker_class="verifier"` while preserving all ordinary/review dispatch paths)
- Modify: `dswarm/swarm/shared_graph.py` if a terminal helper or `review_finding_verified` append is needed
- Modify: `dswarm/swarm/blackboard_bridge.py` and `dswarm/swarm/swarm.py` event subscriber allowlists for compact PoC lifecycle deltas
- Test: `tests/test_verified_poc_orchestration.py`
- Test: extend `tests/test_shared_graph.py` for cold replay and append-failure ordering

**Interfaces:**
- `run_poc_verification(intent_metadata, *, graph, verifier, runtime_lease_factory, usage_context) -> VerificationOutcome`.
- Orchestration sequence: resolve registration → acquire Docker runtime lease → append `poc_verification_started` durably → execute immutable command → classify exact provenance/terminal result → append one terminal event durably → only then emit blackboard/UI delta; on successful linked review verification append `review_finding_verified` only after durable `poc_verified`.
- Concurrent starts return `lease_unavailable` and do not execute twice.
- Graph append failure never emits a success delta; cancellation appends bounded failure when writable and re-raises; runtime failures are diagnostics and never facts/dead-ends.
- Cold replay yields the same reproduction/verification projection; `poc_verified` does not alter `verified_evidence()` or flag status.

- [ ] **Step 1: Write failing tests** for durable ordering, success/failure terminal events, append failure suppression of deltas, cancellation, duplicate/concurrent attempts, linked review event ordering, replay equivalence, and no fact/flag contamination.
- [ ] **Step 2: Run focused orchestration tests** and confirm failure.
- [ ] **Step 3: Implement orchestration at the existing verifier execution seam** without changing ordinary/review lane semantics or the provenance gate.
- [ ] **Step 4: Run focused orchestration, event, and runtime tests.**
- [ ] **Step 5: Commit**
  - `git add dswarm/swarm/poc_verification_runtime.py dswarm/swarm/reason_scheduler.py dswarm/swarm/shared_graph.py dswarm/swarm/blackboard_bridge.py dswarm/swarm/swarm.py tests/test_verified_poc_orchestration.py tests/test_shared_graph.py`
  - `git commit -m "feat: make PoC verification durable and replayable"`

## Task 6: Compatibility, security regression, documentation, and full verification

**Files:**
- Modify: `docs/10-v4-kernel-improvement-implementation.md` (mark only the Verified-PoC M9 sub-item implemented, leaving other M9 legacy items separate)
- Modify: `README.md`/`README_CN.md` only if the existing PoC/blackboard usage documentation requires the new marker to be discoverable
- Test: `tests/test_verified_poc_compatibility.py` (new)
- Test: existing `tests/test_gate.py`, PoC, append-only, runtime-pool, M5, pentest, and frontend event-contract suites as applicable

- [ ] **Step 1: Write failing compatibility/security tests** asserting unchanged gate behavior, unchanged CTF output, no host subprocess path, no sensitive public payload fields, and correct root `.gitignore`/test fixture handling if new integration artifacts are added.
- [ ] **Step 2: Run focused regression tests and verify any failure is attributable to the new contract.
- [ ] **Step 3: Implement only documentation/event allowlist cleanup needed by the tests; do not broaden scope into M9 scope-audit, cleanup registry, or upstream patches.
- [ ] **Step 4: Run the full suite:** `uv run pytest -q`.
- [ ] **Step 5: Run hygiene checks:** `git diff --check`, inspect `git status --short`, and verify no secrets, local runtime fallback, or changes to `gate.py` semantics.
- [ ] **Step 6: Commit**
  - `git add docs/10-v4-kernel-improvement-implementation.md README.md README_CN.md tests/test_verified_poc_compatibility.py`
  - `git commit -m "test: verify M9 PoC gate compatibility"`

## Verification Checklist

- [ ] Every test in the design spec's Section 8 has a deterministic implementation or an explicit mapped integration test.
- [ ] Pentest-only marker and verifier behavior is proven; CTF mode is unchanged.
- [ ] Only `POC_SAVE.entry_command` can execute; no free-form verifier command reaches production.
- [ ] Every production verifier execution has a current run Docker pool identity and runtime lease.
- [ ] Success requires exit code zero and exact indicator in real verifier provenance.
- [ ] Canonical events are append-only; cold replay matches online projection.
- [ ] `poc_verified` cannot enter `verified_evidence()` or flag acceptance.
- [ ] Durable terminal append precedes all success blackboard/UI deltas.
- [ ] Full `uv run pytest -q` and `git diff --check` pass with a clean working tree before completion is claimed.




