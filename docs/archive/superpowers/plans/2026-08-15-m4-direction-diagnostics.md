> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M4 direction diagnostics implementation plan

Date: 2026-08-15

## Review verdict

M4 has no architecture-blocking objection and can proceed, but the v4 text needs these corrections during implementation:

1. `explicit_auto` must only describe `auto/any/unknown/unclear`; a lower-case canonical value is `explicit_canonical`.
2. Alias resolution is recorded as `recognized_alias` (the v4 enum and existing terminology); no alias may be mechanically overridden.
3. The registry owns only static canonical IDs, aliases, fallback keywords, and default profile IDs. Image, credential account, runtime, and endpoint facts remain in the existing profile/runtime resolver.
4. Raw direction is sanitized at parse time: trim, remove control characters, and cap at 40 characters before it reaches an event or UI.
5. Diagnostics must travel through `Intent -> DispatchDecision -> intent_proposed payload -> intents projection -> dispatchable_intents`; dataclass-only changes are insufficient.
6. Mechanical fallback is allowed only for empty/auto/invalid direction. A valid canonical or alias result is never replaced by keyword suggestion.
7. New dataclass fields are appended with defaults to preserve positional fixtures.

## Scope

In scope:
- Add a typed `DirectionRegistry` in `dswarm/solver/direction_rules.py`.
- Keep worker profile compatibility functions as delegating wrappers.
- Preserve both raw and canonical direction diagnostics per intent.
- Persist diagnostics in immutable `intent_proposed` event payloads and expose them from the materialized intents projection.
- Add deterministic keyword fallback only when the model direction is empty, auto-like, or invalid.
- Emit an operator-visible `direction_override` blackboard delta when fallback changes the effective direction.

Out of scope:
- Priority, route lineage, energy, Advisor, token accounting, provenance gate, or M3 fact-event semantics.
- Dynamic image/account/runtime selection in the direction registry.
- Rewriting existing historical events or adding UPDATEs to the event log.

## Files expected

- `dswarm/solver/direction_rules.py` (new): typed static registry and sanitization/canonicalization helpers.
- `dswarm/solver/worker_profiles.py`: compatibility wrappers delegated to registry.
- `dswarm/solver/reason.py`: append diagnostics fields, parse raw direction without losing it, include fields in payload.
- `dswarm/swarm/agents.py`: append diagnostics to `DispatchDecision`.
- `dswarm/swarm/reason_scheduler.py`: carry diagnostics and apply only permitted fallback; emit delta.
- `dswarm/swarm/shared_graph.py`: persist/expose diagnostics in the intents projection.
- `tests/test_direction_diagnostics.py` (new): registry, parse, fallback and projection tests.
- `docs/10-v4-kernel-improvement-implementation.md`: update M4 wording to match the corrected contract.

## Test-first sequence

1. Registry resolution tests for empty, auto-like, canonical, alias, invalid, sanitation and deterministic keyword suggestions.
2. Reason parsing tests for mixed intents and raw/canonical/resolution preservation.
3. Scheduler tests proving valid model direction wins and fallback only applies to empty/auto/invalid.
4. SQLite graph tests proving event payload and `dispatchable_intents()` expose the same diagnostics.
5. Existing full suite and M3 regression suite.
