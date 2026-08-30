# Regression index for kernel comments

> Status: maintained index. Updated 2026-08-28 for fixlist C4.
>
> This file is the durable home for incident identifiers that still appear in
> production comments. The identifier is a pointer to the original observation,
> not a runtime mode, feature flag, compatibility path, or source of truth.
> In particular, `Race`/`Coordinator` wording in an old incident description does
> **not** authorize bringing either retired mode back into the implementation.

## Contract

1. A regression comment may use the canonical form `regression: run-NNNN` (or
   retain an existing `run-NNNN` reference while the surrounding comment is being
   edited).
2. A new incident identifier in `dswarm/**/*.py` must add a row here in the same
   change. The deterministic `tests/test_regression_index.py` check enforces that
   no identifier is silently lost during a refactor.
3. The index records the affected invariant and representative source locations;
   behavioral authority remains the code and its tests. It is not a flag source,
   a worker prompt, or a compatibility registry.
4. Standalone labels such as `BUG-1` and `BUG-2` are normalized here as named
   regression classes. They do not create a second event or scheduling mode.

## Incident map

| Incident | Affected invariant / regression summary | Representative code references |
|---|---|---|
| `run-0011` | Deduplicate operator hints so repeated identical guidance cannot create a hint storm. | `dswarm/swarm/insight_bus.py:96` |
| `run-0405` | Reject loose flag-format placeholders such as UUID-shaped values instead of treating them as accepted flags. | `dswarm/solver/gate.py:66` |
| `run-0835` | Do not accept a literal f-string/template expression as a real flag. | `dswarm/solver/gate.py:105` |
| `run-1619` | Generic brace extraction must not promote arbitrary `{...}` text to a flag. | `dswarm/solver/cli_solver.py:2542`; `dswarm/solver/gate.py:65` |
| `run-1763` | Placeholder-looking values with letters and digits still require the hard provenance gate. | `dswarm/solver/gate.py:100` |
| `run-1804` | Worker artifact/blackboard CLI capability may lag the image; capability probing must degrade safely. | `dswarm/solver/cli_solver.py:1070` |
| `run-3154` | Empty stream tails are not facts; worker accounting and intent dispatch must not starve or silently lose ownership. | `dswarm/solver/cli_solver.py:443`; `dswarm/solver/gate.py:120`; `dswarm/swarm/shared_graph.py:1598`; `dswarm/swarm/swarm.py:1659`; `dswarm/swarm/worker_runtime_mixin.py:515` |
| `run-3155` | Planner/provider transient failure and empty-board/recon drought must remain observable and recoverable without false conclusions. | `dswarm/solver/cli_solver.py:444`; `dswarm/solver/reason_scheduler.py:511`; `dswarm/solver/reason_scheduler.py:997` |
| `run-3156` | Development placeholders and stale worker attribution must not become valid evidence. | `dswarm/solver/gate.py:39`; `dswarm/solver/gate.py:127`; `dswarm/swarm/shared_graph.py:1707` |
| `run-7345` | A worker exit must not finish the whole run; re-bootstrap must not loop on a dead worker. | `dswarm/solver/cli_solver.py:662`; `dswarm/swarm/worker_runtime_mixin.py:705` |
| `run-7349` | Truncated planner output, zero-intent cycles, and retry starvation require bounded recovery and re-proposal from current evidence. | `dswarm/solver/reason.py:456`; `dswarm/solver/reason.py:473`; `dswarm/swarm/reason_scheduler.py` |
| `run-7352` | Worker context and bootstrap/retry policy must avoid long-lived death spirals and shared-fate bursts. | `dswarm/solver/cli_solver.py:3544`; `dswarm/swarm/swarm.py:159` |
| `run-10067` | Long unlock-chain evidence must remain visible to Reason rather than being buried by summary truncation. | `dswarm/swarm/shared_graph.py:4276`; `dswarm/swarm/swarm.py:1708` |
| `run-10070` | Solved/collect-mode completion and false-positive evidence must stay consistent; avoid unbounded re-bootstrap. | `dswarm/swarm/swarm.py:462`; `dswarm/swarm/swarm.py:536`; `dswarm/swarm/shared_graph.py:4322` |
| `run-11189` | Multi-flag completion, streamed/intermediate output, and operator-facing pause state must agree; a clean found marker remains provenance-bound. | `dswarm/solver/cli_solver.py:783`; `dswarm/solver/cli_solver.py:1686`; `dswarm/solver/cli_solver.py:3588`; `apps/web/run_manager.py:238` |
| `run-11190` | Prevent repeated request/phrase churn, stale intent re-discovery, and false operator-needed loops. | `dswarm/swarm/shared_graph.py:3858`; `dswarm/swarm/shared_graph.py:3904`; `dswarm/swarm/swarm.py:1272` |
| `run-11550` | Grep patterns and verifier expressions are not execution evidence and must not be laundered into flags. | `dswarm/solver/cli_solver.py:1587`; `dswarm/solver/gate.py:222` |
| `run-11551` | Reading a prior session or local file and restating its marker is not target-derived evidence. | `dswarm/solver/cli_solver.py:1902`; `dswarm/solver/cli_solver.py:1947`; `dswarm/solver/cli_solver.py:2003` |
| `run-11553` | Worker-read writeups/verifier docs cannot create a cooldown, lock, or other target fact. | `dswarm/solver/cli_solver.py:479`; `dswarm/solver/cli_solver.py:1878` |
| `run-40726` | Cancellation and steering must stop not-yet-started workers without causing uncontrolled respawn or spend. | `dswarm/solver/cli_solver.py:693`; `dswarm/solver/cli_solver.py:1242` |
| `run-42598` | Spawn-death loops must be bounded; cancellation/steering is distinct from a worker-found terminal result. | `dswarm/solver/cli_solver.py:701`; `dswarm/solver/cli_solver.py:1526` |
| `run-42599` | A poisoned shared board cannot collectively rationalize a solved result. | `dswarm/solver/cli_solver.py:4216` |
| `run-75375` | Route-less candidate/open-intent accumulation needs bounded projection and cleanup. | `dswarm/swarm/shared_graph.py:933`; `dswarm/swarm/shared_graph.py:2948` |
| `run-75377` | Candidate/review echo and repeated challenge emissions must not inflate the evidence backlog. | `dswarm/swarm/shared_graph.py:209`; `dswarm/swarm/shared_graph.py:2868`; `dswarm/swarm/review_flow.py:619` |
| `run-75378` | Blackboard skill materialization must refresh from the repository; stale user-scope copies are not trusted. | `dswarm/solver/blackboard_skill.py:28`; `dswarm/solver/blackboard_skill.py:108`; `dswarm/swarm/swarm.py:617` |
| `run-4408` | Pool containers that die during startup must leave their terminal state and agent logs in the backend log before cleanup; silent hello/identity failures were undiagnosable. | `dswarm/solver/container_runtime.py` (`_log_pool_container_death`) |
| `run-75379` | The accepted flag must trace to real command output; invalidation/reopen, streamed output, and multi-flag fuel must not split into contradictory states. | `dswarm/solver/cli_solver.py:1582`; `dswarm/solver/cli_solver.py:1689`; `dswarm/solver/cli_solver.py:1975`; `dswarm/swarm/swarm.py:540`; `dswarm/swarm/swarm.py:571` |
| `run-6964` | A failed readiness probe must persist its classified reason and worker output snippet to the backend log before the pool container is torn down; transport-class probe failures otherwise vanish with the container. | `dswarm/solver/runtime_probe.py` (`_log_probe_evidence`) |

## Legacy labels

| Label | Canonical index entry | Source |
|---|---|---|
| `BUG-1` | Markdown bold-marker pollution in stream extraction. | `dswarm/solver/cli_stream.py:14` |
| `BUG-2` | Space/truncation loss in stream flag extraction. | `dswarm/solver/cli_stream.py:15` |
| `BUG-3` / `BUG-4` | Historical split-brain variants; indexed under the owning `run-75379` provenance/completion entry. | `dswarm/solver/cli_solver.py:3683`; `dswarm/swarm/swarm.py:571` |

The index intentionally stores no flag values, secrets, challenge solutions, or
worker prompts. It may be updated when a source line moves, but a new regression
identifier still requires a new row and a regression test.
