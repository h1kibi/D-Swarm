> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../../00-architecture-spec.md) 取代；本文保留作为时代记录。

# M8 Advisor Offline Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a completely offline, paired baseline-versus-Advisor experiment that measures whether a flag-scout suggestion improves the next Reason cycle's planning output, without adding production watcher, graph schema, prompt, dispatch, UI, or lifecycle behavior.

**Architecture:** An operator-provided frozen fixture feeds two fresh planner instances created from the same immutable configuration: baseline receives the exact opaque production Reason summary, while Advisor receives that byte-identical prefix plus an experimental, explicitly untrusted suggestion block appended only inside the benchmark runner. The frozen summary is not redacted because current production `SharedGraph.to_reason_summary()` may include `_captured_flags_block()` and baseline fidelity requires preserving exactly what Reason saw. Each case writes an independent append-only local sidecar trace; a read-only reporter compares intents against hidden reference objectives and aggregates paired run-level deltas. The experiment never opens or writes `SharedGraph`, never dispatches a worker, and never imports the production scheduler.

**Tech Stack:** Python 3.11+, frozen dataclasses, `asyncio`, stdlib JSON/`hashlib.blake2b`, append-only JSONL with `flush` + `os.fsync`, existing `ReasonResult`/`Intent` models, pytest, operator-local `module:factory` benchmark suites.

**Spec:** `docs/09-kernel-improvement-review-feedback.md` §12.10, with the dependency/no-go context in §12.12 and `docs/08-oss-research-and-kernel-improvements.md`. The stale production-oriented M8 sketch in `docs/10-v4-kernel-improvement-implementation.md` is superseded by this plan.

**Status:** Contract v2 — approved for implementation. Implementation status: Not Implemented. Production Advisor remains No-Go.

## Global Constraints

- M8 is **offline evidence collection only**. Production Advisor remains No-Go.
- Do not modify `reason_scheduler.py`, `shared_graph.py`, production `reason.py`, EventBus, Web/TUI, the provenance gate, worker spawning, pause/stop/resume, budget gates, or cost-ledger semantics.
- Do not add a production `suggestions` table or a graph event for Advisor data.
- `## Open suggestions` exists only in the experiment runner, never in the production Reason summary.
- A fixture is a frozen next-cycle snapshot. Do not reconstruct it from the final graph: `to_reason_summary()` has no as-of-seq contract and would leak future information.
- `AdvisorFixture` has no dedicated `flag`, `raw_flag`, or `flag_value` field. Its opaque `graph_summary` may contain an already-captured raw flag because that is part of the real production planner input; preserve that prefix byte-for-byte and never parse, redact, copy, or reinterpret it.
- The sidecar never stores planner request text or model-generated free text. `ReasonResult`/`Intent` must not be persisted through `asdict()`, `model_dump()`, `__dict__`, `to_payload()`, or generic recursive serialization.
- Planner output is converted in memory to an explicit safe allowlist. Free-text fields such as `goal`, `rationale`, `audit_notes`, `drift`, `complete_why`, `reopen_because`, `surface_target`, provider messages, and arbitrary dispatch payloads never enter the sidecar.
- `AdvisorySuggestion`, source identity, and the appended Advisor block must never contain verbatim raw flag values, hidden reference labels, opaque-summary fragments, credentials, or provider echoes. Safe planner traces and reports additionally must not contain the appended block or any experimental-prompt fragment.
- Goal/route/intent fingerprints are data-minimization aids, not anonymization or secrecy boundaries: low-entropy values may be dictionary-linkable. Every fixture, sidecar, and report remains local-sensitive and must stay under ignored artifact roots.
- Sensitive-output detection occurs before durable append. A rejected string is never copied into an error payload. The case becomes `incomplete/sensitive_output_redacted`; discovering prohibited content in an existing trace makes the dataset `corrupt` and report-ineligible.
- `summary_digest` is computed from the exact UTF-8 summary. It is the only direct summary-content representation allowed in trace identity. It remains local-sensitive metadata and is not anonymous/public data.
- Hidden reference objectives are reporter-only inputs. The runner and sidecar must not serialize reference IDs, goals, routes, token bags, or reference-derived digests/assessment fields.
- Frozen fixtures, sidecars, safe planner traces, and reports remain challenge-sensitive local artifacts. Prepared suite modules live under git-ignored `/local_benchmarks/`; run data lives under ignored `eval_runs/` or `sessions/`; none enter the distribution.
- CLI `--output` is accepted only when the fully resolved target is a strict descendant of the resolved repository-root `eval_runs/` or `sessions/` directory. Reject equality with either root, `..` traversal, repository-root or `docs/` targets, and symlink/junction escapes. Rejection must preserve any existing target and leave no owned temp file. Stdout is an explicit operator export channel and therefore has no Git-path protection.
- Both arms use the same immutable planner configuration but different per-arm planner instances created by `PlannerFactory`. Their only permitted **model-visible prompt-byte** difference is the appended experimental suggestion block; `arm`, fixture IDs, and usage identities are runner control metadata and must not be serialized into model-visible content.
- Each arm has one runner-owned `asyncio.Task`. A timeout is not complete until cancellation cleanup finishes. An unresponsive task is a suite-level isolation failure; no later arm or case may start.
- Audit timestamps `started_ts` and `finished_ts` use `time.time()` only for display. `wall_seconds` is the finite, non-negative difference of `time.monotonic()` readings; timeout and cancellation-cleanup deadlines use the event loop's monotonic clock. Wall-clock timestamps never drive timeout, ordering, or duration claims.
- `PlannerFactory` is trusted local setup code: each call must be synchronous, bounded, and side-effect-free apart from constructing one fresh adapter. It must not perform provider I/O or spawn tasks, threads, or child processes; provider work begins only inside the runner-owned task.
- Planner implementations must not create untracked background tasks, reuse mutable session/conversation/client state across arms, or write trace/usage after their arm terminal. M8 v1 does not claim process-level isolation.
- Suggestion lifecycle is append-only: `suggestion_created`, followed by exactly one of `suggestion_consumed` or `suggestion_rejected`.
- Trace lifecycle is governed by the deterministic DFA in Task 2 and by `case_started.arm_order`; both baseline-first and Advisor-first cases are valid.
- One resolved `case_root` has one writer process and one fixture identity. A second writer is rejected; M8 v1 does not support concurrent writers or stale-lock takeover.
- Usage states are `measured | estimated | unknown`. Unknown has all numeric values `None`; estimated is never reported as measured; exact deltas require measured values on both arms for that dimension.
- Unknown or missing numeric usage is never treated as zero.
- Allowed claims: next-cycle reference-objective coverage, deterministic intent differences, planner wall-time/cost deltas, and “offline next-cycle planning estimate”.
- Always report real flag latency, solve-rate delta, worker starts saved, tokens saved, focused-dispatch latency, race outcome, production lifecycle correctness, and OODA wakeup latency as literal `"N/A"`.
- Finish or commit the current M7 supplementation before implementation. Never mix M7 and M8 in one commit.
- Every task follows RED → minimal GREEN → focused tests → `git diff --check` → independent commit.

## File Map

| File | Responsibility |
|---|---|
| `dswarm/swarm/advisor_experiment.py` | Frozen fixture/reference/suggestion types, validation, IDs, trigger, suggestion block, intent comparison |
| `dswarm/swarm/advisor_sidecar.py` | Append-only JSONL lifecycle storage, fsync durability, fold/corruption rules |
| `dswarm/swarm/advisor_runner.py` | Paired planner calls, deterministic arm ordering, timeout/error isolation, trace emission |
| `dswarm/swarm/advisor_report.py` | Per-case quality estimates, run-level paired bootstrap, N/A discipline |
| `dswarm/swarm/advisor_benchmark.py` | Operator suite/case types and sequential execution |
| `scripts/advisor_benchmark.py` | `module:factory` CLI and deterministic JSON output |
| `tests/test_advisor_*.py` | Deterministic contract and integration tests |
| `.gitignore` | Ignore operator-local `local_benchmarks/` fixture/suite material |
| `docs/superpowers/plans/2026-08-16-m8-advisor-offline-experiment.md` | This approved implementation contract; tracked with M8-0 |
| `docs/10-v4-kernel-improvement-implementation.md` | Updated only after implementation to preserve production No-Go |

---

### Task 1: Freeze safe experiment types, source identity, and comparison rules

**Files:**
- Create: `dswarm/swarm/advisor_experiment.py`
- Create: `tests/test_advisor_experiment.py`
- Track: `docs/superpowers/plans/2026-08-16-m8-advisor-offline-experiment.md`

**Interfaces:**
- Produces `AdvisorReferenceObjective`, `AdvisorFixture`, `AdvisorySuggestion`, `SuggestionTrigger`, `AdvisorIntentTrace`, `AdvisorReasonTrace`, `IntentComparison`, and `CaseAssessment`.
- Produces `make_advisor_fixture()`, `flag_scout_trigger()`, `build_experimental_summary()`, `safe_reason_trace()`, `intent_trace_equivalent()`, `compare_intent_traces()`, and `assess_suggestion()`.
- Consumes only `dswarm.solver.reason.Intent` and `ReasonResult`; no graph, scheduler, EventBus, gate, Web, or TUI import.

- [ ] **Step 1: Write failing fixture, source-safety, and architecture tests**

```python
from dataclasses import fields
from pathlib import Path

from dswarm.swarm.advisor_experiment import (
    AdvisorReferenceObjective, make_advisor_fixture,
)


def _fixture(**overrides):
    values = dict(
        benchmark_run_id="bench-run-1", challenge_id="multi-flag-1",
        challenge_mode="ctf", expected_flags=3,
        captured_flags_before_source=1, source_event_seq=42,
        source_event_ts=1000.0, source_intent_id="intent-web-1",
        source_route_hash="route-web", next_cycle_id="reason-2",
        graph_summary=("[#10] verified web behavior\n"
                       "## Flags already captured\n  - flag{opaque_fixture_secret}"),
        fact_index="10: verified web behavior", available_fact_seqs=(10,),
        max_intents=4, goal="capture all flags",
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="obj-admin", route_hash="route-admin",
            goal="inspect the admin branch"),),
    )
    values.update(overrides)
    return make_advisor_fixture(**values)


def test_fixture_has_no_dedicated_raw_flag_field_but_preserves_opaque_summary():
    fixture = _fixture()
    names = {item.name for item in fields(type(fixture))}
    assert {"flag", "raw_flag", "flag_value"}.isdisjoint(names)
    assert "flag{opaque_fixture_secret}" in fixture.graph_summary
    assert fixture.summary_digest.startswith("m8-summary::")


def test_advisor_delta_does_not_copy_flag_from_opaque_summary():
    fixture = _fixture()
    trigger = flag_scout_trigger(fixture)
    rendered = build_experimental_summary(fixture, trigger.suggestion)
    assert rendered.startswith(fixture.graph_summary)
    delta = rendered[len(fixture.graph_summary):]
    assert "flag{opaque_fixture_secret}" not in delta
    assert rendered.count("flag{opaque_fixture_secret}") == 1


def test_module_does_not_import_production_scheduler_or_graph():
    source = Path("dswarm/swarm/advisor_experiment.py").read_text("utf-8")
    for forbidden in ("reason_scheduler", "shared_graph", "event_bus", "solver.gate"):
        assert forbidden not in source
```

Also cover empty IDs, non-finite timestamps, `expected_flags < 1`, negative captured count, captured count beyond expected, duplicate reference IDs, empty reference fingerprints, invalid fact seqs, empty summary, control characters, multiline source identity, over-limit UTF-8 source identity, and unregistered direction values.

- [ ] **Step 2: Run RED**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_experiment.py -q
```

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement immutable fixture and suggestion schemas**

Use these exact public fields:

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorReferenceObjective:
    objective_id: str
    route_hash: str = ""
    goal: str = ""

@dataclass(frozen=True, kw_only=True)
class AdvisorFixture:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    challenge_mode: str
    expected_flags: int
    captured_flags_before_source: int
    source_event_seq: int
    source_event_ts: float
    source_kind: Literal["flag_found"]
    source_intent_id: str
    source_route_hash: str
    next_cycle_id: str
    graph_summary: str
    fact_index: str
    available_fact_seqs: tuple[int, ...]
    max_intents: int
    goal: str
    reference_objectives: tuple[AdvisorReferenceObjective, ...]

@dataclass(frozen=True, kw_only=True)
class AdvisorySuggestion:
    suggestion_id: str
    fixture_id: str
    source_event_seq: int
    kind: Literal["flag_scout"]
    source_intent_id: str
    source_route_hash: str
    route_attribution: Literal["explicit", "unattributed"]
    prompt_text: str

@dataclass(frozen=True, kw_only=True)
class SuggestionTrigger:
    eligible: bool
    reason: Literal["eligible", "single_flag_run", "no_remaining_flag_after_source"]
    suggestion: AdvisorySuggestion | None
```

Canonical identity helpers use typed canonical JSON, `allow_nan=False`, UTF-8, and `blake2b(digest_size=16)`. `summary_digest = _text_digest("m8-summary", graph_summary)`. `fixture_id` includes typed non-prompt metadata plus digests of planner-visible `graph_summary`, `fact_index`, and `goal`; hidden references and their digests are excluded. `suggestion_id` binds only to fixture/source typed identity and never to prompt text or a raw flag field.

Source identities are normalized at the fixture boundary:

- Unicode NFKC, strip surrounding whitespace, exactly one line.
- Reject Unicode control characters and newline/carriage-return characters.
- `source_intent_id`: at most 128 UTF-8 bytes; characters limited to ASCII letters, digits, `.`, `_`, `:`, `/`, and `-`.
- `source_route_hash`: at most 256 UTF-8 bytes; same token alphabet after canonical route normalization.
- Empty values are valid and render as the fixed token `unattributed`.
- Never infer missing attribution from keywords, summary text, or hidden references.
- `reference_objectives` is reporter-only: objective IDs must be non-empty and unique; each objective must provide a canonicalizable route or a non-empty normalized goal. These values are excluded from `fixture_id`, request construction, sidecar identity, and runner branching.

Render the suggestion block from a fixed JSON object using `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`; never interpolate source strings into Markdown delimiters.

- [ ] **Step 4: Implement the exact flag-scout trigger**

Eligibility is exactly:

```python
remaining_after_source = expected_flags - (captured_flags_before_source + 1)
```

- `expected_flags == 1` → `single_flag_run`.
- `remaining_after_source <= 0` → `no_remaining_flag_after_source`.
- Otherwise create one `flag_scout` suggestion.

The block says a provenance-verified flag event completed one branch while more flags remain, lists only encoded event seq/intent/route, asks Reason to consider sibling/neighboring/distinct remaining objectives, identifies the block as untrusted and not evidence, and requires citations only from frozen graph facts. It never includes event payload, raw flag, inferred route, hidden reference labels, or text copied/extracted from `graph_summary`.

- [ ] **Step 5: Write failing safe-trace tests**

Tests must prove:

1. `safe_reason_trace()` never calls `asdict`, `model_dump`, `__dict__`, or `Intent.to_payload()`.
2. Raw `goal`, `rationale`, `audit_notes`, `drift`, `complete_why`, `surface_target`, `reopen_because`, and arbitrary `dispatches` strings do not appear in canonical trace JSON.
3. A synthetic flag sentinel repeated by the planner is absent from trace bytes.
4. A hidden-reference sentinel and an experimental-prompt sentinel are absent from trace bytes.
5. Invalid enum/fact/fingerprint inputs raise `AdvisorSensitiveOutput` before append; the exception carries only a fixed error code and field name, never the rejected value.
6. `goal_fingerprint` is stable for equivalent normalized token bags and contains no original goal text.
7. `from_facts` rejects bool, zero, negative, float, and string members as malformed planner output; a positive integer absent from `available_fact_seqs` is retained as a safe numeric citation and later counts as one unsupported intent rather than failing the arm.

- [ ] **Step 6: Implement explicit safe planner trace allowlists**

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorIntentTrace:
    intent_key: str
    goal_fingerprint: str
    route_fingerprint: str
    worker_class: Literal["code", "shell_agent", "verifier", "review"]
    priority: float
    from_facts: tuple[int, ...]
    direction: Literal["", "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"]
    requires_recon: bool
    host_scan: bool

@dataclass(frozen=True, kw_only=True)
class AdvisorReasonTrace:
    goal_met: bool
    verdict: Literal["complete", "course_correct", "explore"]
    intents: tuple[AdvisorIntentTrace, ...]
    audit_note_count: int
    pinned_facts: tuple[int, ...]
    dispatch_count: int
```

`safe_reason_trace(result, *, available_fact_seqs, forbidden_fragments=())` builds these objects field by field. It may inspect raw strings in memory only to validate, normalize, detect an exact runner-known forbidden fragment, or compute a domain-separated `blake2b` fingerprint. It never returns or logs a rejected value. `forbidden_fragments` may contain only non-reference values already known to the runner, such as the exact appended Advisor block and adapter-supplied test/provider canaries; it must never be populated from `fixture.reference_objectives`. The opaque summary is never split, tokenized for denylisting, or copied into trace fields. Hidden-reference and raw-flag safety comes from never persisting unconstrained text: those fields are discarded or fingerprinted, and the fingerprints remain explicitly local-sensitive rather than being treated as anonymous.

`intent_key`, `goal_fingerprint`, and `route_fingerprint` are domain-separated digests of normalized model values; raw `intent_id`, `goal`, `route_hash`, `mode`, and `task_kind` are never persisted. `worker_class` is accepted only from the parser's four canonical values, and `direction` only from the registered canonical direction enum. `priority` must be finite. `pinned_facts` and `from_facts` require `type(seq) is int`, positive values, and sorted stable dedupe. Positive but unavailable `from_facts` values remain in the safe trace so unsupported-citation quality can be measured; malformed scalar types/non-positive values fail the arm instead of being silently dropped.

No exception message from this layer includes model text. Allowed diagnostics are fixed enums such as `invalid_worker_class`, `unsafe_direction`, `invalid_fact_reference`, `invalid_fingerprint_input`, and `sensitive_output_redacted`.

- [ ] **Step 7: Implement deterministic comparison and citation semantics**

```python
@dataclass(frozen=True, kw_only=True)
class IntentComparison:
    baseline_count: int
    advisor_count: int
    overlap_count: int
    baseline_duplicate_count: int
    advisor_duplicate_count: int
    baseline_unsupported_citation_count: int
    advisor_unsupported_citation_count: int
    advisor_only_intent_indexes: tuple[int, ...]
    baseline_only_intent_indexes: tuple[int, ...]
    jaccard: float

@dataclass(frozen=True, kw_only=True)
class CaseAssessment:
    verdict: Literal[
        "accepted_reference_gain", "unchanged", "mixed", "regressed",
        "rejected_baseline_already_equivalent", "rejected_no_supported_delta",
        "rejected_advisor_empty", "indeterminate_planner_error",
    ]
    reason: Literal[
        "new_supported_reference_without_loss", "no_planning_delta",
        "gain_with_regression", "lost_reference", "baseline_already_covers_target",
        "delta_has_no_supported_citation", "advisor_has_no_intents",
        "planner_arm_not_successful",
    ]
```

Intent equivalence is true when non-empty `route_fingerprint` values match or `goal_fingerprint` matches. Match baseline index then Advisor index deterministically; `intent_key` is audit identity only and does not define semantic equivalence.

A supported intent has non-empty `from_facts` and every member is present in `fixture.available_fact_seqs`. Empty citations or any positive-but-unavailable seq make that intent unsupported. Count at most one unsupported unit per intent, not one unit per bad fact. Structurally malformed citation members (`bool`, non-integer, or non-positive) fail safe-trace conversion for the whole arm and are never silently dropped. Duplicate fact seqs are deterministically deduped and separately covered by validation tests; hidden references are never citation sources.

Reference coverage and `CaseAssessment` are computed only by the reporter in Task 4 from the in-memory fixture plus safe traces. The runner and sidecar use only `IntentComparison`; they never serialize reference IDs or reference-derived assessment data.

- [ ] **Step 8: Run GREEN and commit M8-0**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_experiment.py -q
git diff --check
git add docs/superpowers/plans/2026-08-16-m8-advisor-offline-experiment.md dswarm/swarm/advisor_experiment.py tests/test_advisor_experiment.py
git commit -m "M8-0 safe advisor experiment types and trigger"
```

---

### Task 2: Add the single-writer append-only sidecar and deterministic DFA

**Files:**
- Create: `dswarm/swarm/advisor_sidecar.py`
- Create: `tests/test_advisor_sidecar.py`

**Interfaces:**
- Consumes fixture/suggestion IDs and safe trace dataclasses from Task 1.
- Produces `AdvisorTraceEvent`, `AdvisorTraceFold`, `AdvisorTraceSink`, `AdvisorTraceCorrupt`, `AdvisorTraceAlreadyExists`, `AdvisorWriterBusy`, `advisor_trace_path()`, and `fold_advisor_trace()`.

- [ ] **Step 1: Write failing durability, writer-ownership, and identity tests**

```python
def test_trace_path_is_metrics_sidecar(tmp_path):
    assert advisor_trace_path(tmp_path) == (
        tmp_path / "metrics" / "advisor-experiment.jsonl"
    )


def test_append_flushes_and_fsyncs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))
    with AdvisorTraceSink(
        tmp_path, fixture_id="fixture::1",
        summary_digest="m8-summary::1", benchmark_run_id="run-1",
    ) as sink:
        sink.append(
            kind="case_started", identity="case",
            payload=_case_started_payload(), ts=1.0,
        )
    assert len(calls) >= 1


def test_second_writer_for_same_case_root_is_rejected(tmp_path):
    first = AdvisorTraceSink(
        tmp_path, fixture_id="fixture::1",
        summary_digest="m8-summary::1", benchmark_run_id="run-1",
    )
    with pytest.raises(AdvisorWriterBusy):
        AdvisorTraceSink(
            tmp_path, fixture_id="fixture::1",
            summary_digest="m8-summary::1", benchmark_run_id="run-1",
        )
    first.close()
```

Also cover lock release by context-manager close, stale lock refusing automatic takeover, exact duplicate idempotency, conflicting duplicate IDs, fixture change in an existing root, summary-digest mismatch, benchmark-run mismatch, malformed middle line, final partial line, a second-process sink refusing every pre-existing non-empty trace with `AdvisorTraceAlreadyExists`, lock cleanup when that refusal is raised, and read-only fold creating no directories/files.

- [ ] **Step 2: Run RED**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_sidecar.py -q
```

- [ ] **Step 3: Implement the event envelope, payload allowlists, and single-writer contract**

```python
_TRACE_KINDS = {
    "case_started", "suggestion_created",
    "baseline_started", "baseline_completed", "baseline_failed",
    "advisor_started", "advisor_completed", "advisor_failed",
    "suggestion_consumed", "suggestion_rejected",
    "case_interrupted", "case_completed",
}

@dataclass(frozen=True, kw_only=True)
class AdvisorTraceEvent:
    schema_version: int
    event_id: str
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    kind: str
    ts: float
    payload: Mapping[str, object]
```

`event_id = _stable_id("m8-event", [fixture_id, summary_digest, benchmark_run_id, kind, identity])`. Every event envelope repeats the three case-binding fields so a middle line can be validated without borrowing identity from `case_started`. Lifecycle identity is fixed: `case_started`/`case_interrupted`/`case_completed` use `identity="case"`; suggestion events use `identity=suggestion_id`; arm events use `identity=f"{arm}:{arm_index}"`. Event kind remains part of the ID, while the fold treats completed/failed with the same arm identity as one exclusive terminal role. Construct an event once and reuse the same object/`ts` for an append retry; changing bytes under the same event ID is corruption. Copy each validated payload before wrapping with `MappingProxyType`; reject non-finite times and non-JSON values. Serialize one canonical UTF-8 object per line with `allow_nan=False`.

Do not accept arbitrary payload keys. Define a required/optional key allowlist for every kind. In particular:

```text
case_started required:
  fixture_id, summary_digest, benchmark_run_id, challenge_id,
  source_kind="flag_found", source_event_seq, source_intent_id, source_route_hash,
  eligible, trigger_reason, arm_order

baseline_started/advisor_started required:
  arm, arm_index, stage="setup"

baseline_completed/advisor_completed required:
  arm, arm_index, call_outcome="succeeded", started_ts, finished_ts, wall_seconds,
  safe_reason_trace, usage

baseline_failed/advisor_failed required:
  arm, arm_index, call_outcome="planner_error"|"timeout"|"setup_error",
  failure_stage="pre_submit"|"post_submit", error_code,
  started_ts, finished_ts, wall_seconds, usage

suggestion_created required:
  suggestion_id, source_event_seq, route_attribution

suggestion_consumed required:
  suggestion_id, arm="advisor"

suggestion_rejected required:
  suggestion_id, reason_code

case_interrupted required:
  interruption_code, lifecycle_stage

case_completed required:
  fixture_id, summary_digest, benchmark_run_id,
  trace_result_digest, comparison_digest, terminal_status="clean"|"incomplete"
```

The payload validator rejects request text, planner free text, hidden-reference fields, generic exception messages, and keys including `graph_summary`, `experimental_summary`, `prompt`, `prompt_text`, `raw_flag`, `flag_value`, `reference_objectives`, `reference_ids`, `goal`, `rationale`, `audit_notes`, `drift`, `complete_why`, `dispatches`, and `error_message` at any nesting depth. Safe trace dataclasses must be serialized by dedicated functions, not a generic recursive dataclass dumper.

M8 v1 is single-writer process only. On construction, create `<case_root>/metrics/advisor-experiment.writer.lock` using exclusive create and write a random owner UUID plus PID for diagnostics. A second sink fails with `AdvisorWriterBusy`. `close()` removes only the lock owned by that sink. A stale lock is never automatically broken; the operator must use a new case root or explicitly remove the stale local artifact outside the benchmark. Within one process, also use a resolved-path keyed shared `threading.Lock` so two sink instances cannot race before lockfile creation.

A case trace is write-once per `case_root`. Sink construction refuses any pre-existing non-empty trace with `AdvisorTraceAlreadyExists`; M8 v1 never truncates, repairs, resumes, or appends after a prior process. The harness handles an existing clean/complete trace by read-only reuse and handles an existing incomplete/corrupt/partial trace as a case-local fixed-code result that requires a new case root for another attempt. This rule prevents a partial tail from being fused with a later append and prevents nondeterministic planner reruns from creating semantic duplicates.

Within the one live sink, `append()` tracks/scans complete lines, verifies every line has the same fixture/summary/run identity, returns the prior event for an exact duplicate, raises `AdvisorTraceCorrupt` for conflicting duplicate IDs or lifecycle roles, appends/flushes/fsyncs before returning, and never emits graph/EventBus events.

- [ ] **Step 4: Write failing DFA tests**

Cover each legal arm order and these exact classifications:

```text
incomplete:
  case_started without case_completed
  arm_started without terminal
  suggestion_created without consumed/rejected
  case_interrupted before completion
  final unterminated JSONL line
  sensitive_output_redacted
  sidecar_append_failed

corrupt:
  semantic duplicate case_started
  different event IDs occupying one lifecycle role
  terminal without corresponding arm_started
  terminal arm/index disagrees with case_started.arm_order
  case_completed followed by any semantic event
  consumed and rejected both present
  ineligible case with suggestion or Advisor lifecycle
  advisor provider terminal/usage without suggestion_consumed
  duplicate arm terminal
  fixture/summary/run identity mismatch
  case_completed digest mismatch
  malformed complete line
  same event ID with different bytes

clean:
  eligible baseline-first complete sequence
  eligible Advisor-first complete sequence
  ineligible baseline-only complete sequence
  pre-submit Advisor setup failure represented by advisor_started +
    advisor_failed(call_outcome="setup_error") + suggestion_rejected
```

Also prove an identical event ID/content passed to `append()` twice is idempotent and does not create a second physical line; two different event IDs for the same lifecycle role remain corrupt.

- [ ] **Step 5: Implement the physical-order DFA**

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorTraceFold:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    complete: bool
    reasons: tuple[str, ...]
    events: tuple[AdvisorTraceEvent, ...]
    case_started: AdvisorTraceEvent | None
    suggestion_created: AdvisorTraceEvent | None
    suggestion_terminal: AdvisorTraceEvent | None
    baseline_started: AdvisorTraceEvent | None
    baseline_terminal: AdvisorTraceEvent | None
    advisor_started: AdvisorTraceEvent | None
    advisor_terminal: AdvisorTraceEvent | None
    case_interrupted: AdvisorTraceEvent | None
    case_completed: AdvisorTraceEvent | None
```

Fold in physical append order. Ignore only an unterminated final line and mark incomplete; malformed complete lines are corrupt.

The DFA is parameterized by the physically persisted `case_started.payload.arm_order`:

```text
eligible=false:
  arm_order == ["baseline"]
  case_started
  -> baseline_started(stage="setup")
  -> baseline_completed | baseline_failed
  -> case_completed

eligible=true:
  arm_order is exactly one permutation of ["baseline", "advisor"]
  case_started
  -> suggestion_created
  -> execute each arm slot in arm_order:
       baseline slot:
         baseline_started(stage="setup")
         -> baseline_completed | baseline_failed
       advisor slot, submitted path:
         advisor_started(stage="setup")
         -> suggestion_consumed
         -> advisor_completed | advisor_failed(post_submit)
       advisor slot, pre-submit setup failure:
         advisor_started(stage="setup")
         -> advisor_failed(call_outcome="setup_error", failure_stage="pre_submit")
         -> suggestion_rejected(reason_code="planner_setup_failed_before_submit")
  -> case_completed
```

The next arm may start only after the prior arm terminal (and, for the Advisor pre-submit path, after `suggestion_rejected`). `*_started.stage` is fixed to `setup` and is written exactly once before invoking that arm's factory. `suggestion_consumed` is the sole durable Advisor task-admission witness; it proves the runner accepted the suggestion for the owned planner task, not that an upstream provider received bytes. No second `advisor_started` event is written. A post-admission Advisor terminal or any provider usage without `suggestion_consumed` is corrupt. A clean eligible case must attempt both arm slots even when the first arm has an ordinary setup/planner failure. Only interruption, sidecar failure, or isolation failure may stop before the second slot, and that case is incomplete.

Cancellation before Advisor submission may append `suggestion_rejected(reason_code="runner_cancelled_before_submit")`, followed by `case_interrupted`, but it must not fabricate an Advisor terminal or `case_completed`. Cancellation/interruption after any arm start leaves that start orphaned; `case_interrupted` plus the orphan makes the case incomplete.

`case_completed` is the last semantic event. Its `trace_result_digest` is recomputed from canonical safe arm outcomes and usage. `comparison_digest` is recomputed from the non-reference `IntentComparison` when both arms have valid safe traces; baseline-only or unsuccessful-arm cases use the fixed canonical digest of JSON `null`, never an omitted/empty ad hoc value. Hidden-reference assessment is not present in the trace.

`complete=True` iff all of these hold:

1. Exactly one valid `case_started` exists and is physically first.
2. Fixture ID, summary digest, benchmark run ID, challenge ID, and source event identity are consistent.
3. Eligibility, suggestion lifecycle, attempted arms, and arm order satisfy the DFA.
4. Every started arm has exactly one valid terminal; no terminal lacks a start.
5. Exactly one valid `case_completed` exists and is physically last.
6. Result/comparison digests match independent reconstruction, and `case_completed.terminal_status` equals the fold status computed immediately before that event.
7. No incomplete or corrupt reason exists.

Read-only fold must not acquire a writer lock, create directories, truncate files, or alter timestamps.

- [ ] **Step 6: Run GREEN and commit M8-1**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_sidecar.py tests/test_advisor_experiment.py -q
git diff --check
git add dswarm/swarm/advisor_sidecar.py tests/test_advisor_sidecar.py
git commit -m "M8-1 deterministic advisor sidecar lifecycle"
```

---

### Task 3: Implement paired arms with owned-task isolation and interruption closure

**Files:**
- Create: `dswarm/swarm/advisor_runner.py`
- Create: `tests/test_advisor_runner.py`

**Interfaces:**
- Consumes Tasks 1–2.
- Produces `AdvisorUsage`, `AdvisorPlannerRequest`, `AdvisorPlannerResult`, `AdvisorArmOutcome`, `AdvisorCaseOutcome`, `PlannerCallable`, `PlannerFactory`, `AdvisorIsolationFailure`, `arm_order_for()`, and `run_advisor_case()`.

- [ ] **Step 1: Write failing planner-boundary and usage tests**

Lock these facts: eligible fixtures create two distinct planner instances; baseline receives the frozen summary byte-for-byte even when it contains a synthetic raw-flag sentinel; Advisor receives that exact prefix plus one block; the appended delta contains no copied sentinel; hidden references never appear in a request or trace; arm order is deterministic and counterbalanced by fixture-hash parity without claiming exact balance for a finite suite; and no worker/graph method is invoked.

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorUsage:
    usage_status: Literal["measured", "estimated", "unknown"]
    input_tokens: int | None = None
    output_tokens: int | None = None
    usd: float | None = None

@dataclass(frozen=True, kw_only=True)
class AdvisorPlannerRequest:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    arm: Literal["baseline", "advisor"]
    graph_summary: str
    fact_index: str
    max_intents: int
    goal: str
    mode: str

@dataclass(frozen=True, kw_only=True)
class AdvisorPlannerResult:
    result: ReasonResult
    usage: AdvisorUsage = field(
        default_factory=lambda: AdvisorUsage(usage_status="unknown"))

PlannerCallable = Callable[[AdvisorPlannerRequest], Awaitable[AdvisorPlannerResult]]
PlannerFactory = Callable[[Literal["baseline", "advisor"]], PlannerCallable]

# Runtime contract beyond the type alias: invoking PlannerCallable(request) only
# creates a cold coroutine; it performs no provider I/O and schedules no work
# until the runner wraps that coroutine in its owned asyncio.Task.
```

Usage validation:

- `unknown` requires all numeric fields `None`.
- `measured` and `estimated` require at least one numeric field.
- Present token values require `type(value) is int` and `value >= 0`.
- Present USD values must be finite and non-negative.
- Estimated values remain estimated in trace/report and never satisfy a measured delta.

- [ ] **Step 2: Run RED**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_runner.py -q
```

- [ ] **Step 3: Implement deterministic ordering and per-arm factories**

```python
def arm_order_for(fixture_id: str) -> tuple[str, str]:
    byte = hashlib.blake2b(fixture_id.encode("utf-8"), digest_size=1).digest()[0]
    return ("baseline", "advisor") if byte % 2 == 0 else ("advisor", "baseline")
```

`PlannerFactory` must return a newly constructed callable for each arm. The runner calls it once for baseline and once for Advisor; returning the same callable object is rejected. Factory execution is synchronous trusted setup only: it must return promptly, perform no provider/network/file I/O, and create no task, thread, subprocess, mutable global registration, or cross-arm cache. The contract requires equivalent immutable model/configuration but forbids shared mutable conversation, client session, retry state, response cache, or usage accumulator.

The planner callable must not create tasks that are not handed to the runner, and must not continue writing usage/trace after returning. This is a required provider-adapter contract, not a capability M8 can prove for arbitrary Python code. Tests use instrumented factories to verify separate instances and no cross-arm state; documentation must not describe owned-task cleanup as protection against a malicious or non-conforming factory/adapter.

- [ ] **Step 4: Implement owned-task timeout and cancellation cleanup**

For each arm:

1. Build a fresh immutable request.
2. Append exactly one `<arm>_started(stage="setup")` before invoking that arm's factory. Do not append a second start when the callable begins.
3. Create exactly one runner-owned task with `asyncio.create_task()` and retain the handle. Require `asyncio.get_running_loop().get_task_factory() is None`; M8 v1 rejects every custom task factory, not only known eager implementations, because it cannot prove their pre-yield execution semantics. Under the Python 3.11+ default task factory, task creation schedules but does not run the coroutine until the runner yields control.
4. For Advisor, synchronously append and fsync `suggestion_consumed` immediately after task creation but before the first `await`/event-loop yield. This is the durable task-admission witness. If this append fails, cancel the not-yet-run task before yielding, complete bounded cleanup, and do not call the provider. If factory/request setup fails before task creation, append the setup-failure terminal and `suggestion_rejected` instead.
5. Wait with `asyncio.wait({task}, timeout=timeout_s)` so the timeout does not implicitly replace runner cleanup policy.
6. On timeout, call `task.cancel()` and wait with a second bounded `asyncio.wait({task}, timeout=cleanup_timeout_s)`.
7. If cleanup finishes, discard any late normal result and record `call_outcome="timeout"`; timeout usage is `unknown` unless the adapter had already returned a complete validated usage object before the deadline (a result observed only after cancellation is not trusted as measured).
8. If cleanup does not finish, best-effort append `case_interrupted(interruption_code="timeout_cleanup_failed")`, mark the case incomplete, and raise `AdvisorIsolationFailure`.
9. No second arm or later benchmark case may start after `AdvisorIsolationFailure`.

Do not use bare `asyncio.wait_for()` as the isolation proof. It may wait beyond its deadline when cancellation is swallowed, and it does not own child/background tasks.

If `run_advisor_case()` itself is cancelled/interrupted while an owned task is active, it must preserve the first BaseException, call `task.cancel()`, and create one runner-owned cleanup waiter with the monotonic `cleanup_timeout_s` deadline. Await that waiter through `asyncio.shield()` until it finishes or the deadline expires; a repeated outer cancellation is remembered but must not abandon the cleanup waiter. Only after the owned planner task is known terminal, or cleanup is declared failed at the deadline, may the original `CancelledError`, `KeyboardInterrupt`, or `SystemExit` be re-raised. If cleanup does not finish, append fixed interruption code `external_interrupt_cleanup_failed` best-effort, mark isolation compromised, propagate the original BaseException, and allow no later arm/case or successful report serialization. M8 v1 does not attempt to kill arbitrary child tasks/processes created in violation of the planner-adapter contract.

Default values are `timeout_s=180.0` and `cleanup_timeout_s=5.0`; both must be finite and positive.

- [ ] **Step 5: Implement safe arm outcomes and ordinary failure behavior**

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorArmOutcome:
    arm: Literal["baseline", "advisor"]
    call_outcome: Literal[
        "succeeded", "planner_error", "timeout", "setup_error", "not_run",
    ]
    started_ts: float | None
    finished_ts: float | None
    wall_seconds: float | None
    result: AdvisorReasonTrace | None
    usage: AdvisorUsage
    error_code: Literal[
        "", "planner_factory_failed", "planner_call_failed",
        "invalid_planner_output", "sensitive_output_redacted",
        "planner_timeout", "sidecar_append_failed",
    ] = ""

@dataclass(frozen=True, kw_only=True)
class AdvisorCaseOutcome:
    fixture_id: str
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    trigger_reason: str
    suggestion_id: str
    baseline: AdvisorArmOutcome
    advisor: AdvisorArmOutcome
    comparison: IntentComparison | None
    failure_code: str = ""
```

No outcome or trace field stores an exception message or traceback. Exception class names are mapped to fixed error codes; unexpected classes map to `planner_call_failed` without embedding `str(exc)`.

Factory/build failure before Advisor submission produces this lifecycle:

```text
advisor_started(stage="setup")
advisor_failed(call_outcome="setup_error", failure_stage="pre_submit")
suggestion_rejected(reason_code="planner_setup_failed_before_submit")
```

It contains no provider usage and no `suggestion_consumed`. A baseline setup failure uses the analogous baseline start/failed pair. Ordinary per-arm failures do not erase the other arm's already durable trace.

Before durable append, convert `ReasonResult` with `safe_reason_trace()`. If conversion detects prohibited content, discard all raw strings, write only `error_code="sensitive_output_redacted"`, and mark the case incomplete. Never write the rejected value into diagnostics.

- [ ] **Step 6: Implement full lifecycle and interruption semantics**

- Append `case_started` before any planner factory/call.
- Eligible: append `suggestion_created`; build the Advisor request; create the owned task; then append/fsync `suggestion_consumed` before the first event-loop yield. If that append fails, cancel the not-yet-run task and never submit provider work.
- Ineligible: no suggestion or Advisor lifecycle; record the trigger reason only in `case_started` and run baseline once.
- Append exactly one `stage="setup"` started event and one terminal for each normally attempted arm; `suggestion_consumed`, not a second start event, marks Advisor task admission and does not claim upstream provider receipt.
- Compute only non-reference `IntentComparison`; append `case_completed` with independently reproducible result/comparison digests.
- The runner must not read, serialize, hash, or branch on `fixture.reference_objectives`.
- A sidecar append failure makes that case incomplete and stops further arms for that case; the benchmark may continue with later cases unless isolation is uncertain.

Cancellation and process interruption are not ordinary arm failures:

```text
CancelledError before case_completed:
  if an owned task exists, cancel it and finish bounded cleanup first
  best-effort case_interrupted(interruption_code="task_cancelled")
  do not fabricate arm terminal or case_completed
  re-raise the original cancellation; no later case starts

KeyboardInterrupt/SystemExit before case_completed:
  if an owned task exists, cancel it and finish bounded cleanup first
  best-effort case_interrupted(interruption_code="process_interrupted")
  do not fabricate arm terminal or case_completed
  re-raise the original BaseException; no later case starts

suggestion_created before submission interruption:
  best-effort suggestion_rejected(reason_code="runner_cancelled_before_submit")

interruption after suggestion_consumed/arm start:
  do not fabricate *_failed or *_timeout
  orphan start folds to incomplete/interrupted_after_arm_start

cancellation after case_completed is durable:
  completed case remains clean
  cancellation affects only remaining suite execution
```

- [ ] **Step 7: Add isolation, cancellation, and leak regression tests**

Tests must include:

1. Baseline-only ineligible flow.
2. Both deterministic arm orders.
3. Two separate factory-produced planner instances; same-object return is rejected, any non-default event-loop task factory is rejected before `case_started`, instrumented factories prove no provider I/O/task/thread/process begins during factory execution, and invoking each callable creates a cold coroutine without starting work before runner task ownership.
4. Advisor task creation followed by durable `suggestion_consumed` before the first event-loop yield; append failure cancels the not-yet-run task and produces zero provider calls.
5. Cooperative timeout with terminal timeout and retained other arm.
6. Callable swallowing first cancellation but finishing within cleanup deadline; result discarded, still timeout.
7. Callable not finishing within cleanup deadline; `AdvisorIsolationFailure`, no second arm.
8. No later benchmark case after isolation failure.
9. Cancellation at each lifecycle boundary: after case start, suggestion create, arm start, consume, task start, terminal, and durable case completion; active-task cases assert bounded cleanup before propagation.
10. `KeyboardInterrupt` and `SystemExit` best-effort trace plus propagation.
11. Unknown/estimated/measured usage validation and per-dimension preservation.
12. Raw flag, hidden-reference label, opaque-summary fragment, prompt fragment, provider echo, and exception message absent from sidecar bytes.
13. Malformed `ReasonResult`/`Intent` becomes `invalid_planner_output`; no intent is silently dropped.
14. Runner sidecar bytes remain identical when only hidden reference objectives change.

- [ ] **Step 8: Add production-isolation AST tests**

Scan M8 modules and reject imports of `shared_graph`, `reason_scheduler`, EventBus, Web/TUI, or gate. Also assert production files do not import M8 modules. Reject generic calls to `asdict`, `model_dump`, `vars`, `__dict__`, or `Intent.to_payload` in sidecar serialization modules.

- [ ] **Step 9: Run GREEN and commit M8-2**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_experiment.py tests/test_advisor_sidecar.py tests/test_advisor_runner.py -q
git diff --check
git add dswarm/swarm/advisor_runner.py tests/test_advisor_runner.py
git commit -m "M8-2 isolated paired advisor planner runner"
```

---

### Task 4: Build the conservative offline quality report

**Files:**
- Create: `dswarm/swarm/advisor_report.py`
- Create: `tests/test_advisor_report.py`

**Interfaces:**
- Consumes an in-memory `AdvisorFixture`, its read-only `AdvisorTraceFold`, and Task 1 safe comparison semantics.
- Hidden references enter only `build_case_estimate()` and reference-matching helpers in this module; they never enter the runner, planner request, sidecar, trace digest, or error payload.
- Produces `AdvisorCaseEstimate`, `AdvisorAggregateReport`, `build_case_estimate(fixture, trace_path)`, `build_missing_trace_estimate(fixture, failure_code)`, `build_advisor_report()`, and `advisor_report_json()`.

- [ ] **Step 1: Write failing identity, reconstruction, and hidden-reference tests**

Tests must prove:

1. `build_case_estimate()` is read-only: no writer lock, directory creation, file truncation, planner call, graph access, or timestamp change.
2. `fixture_id`, `summary_digest`, `benchmark_run_id`, `challenge_id`, `source_event_seq`, and trigger eligibility agree with `case_started`; any mismatch is `corrupt/identity_mismatch`.
3. `trace_result_digest` and the non-reference `comparison_digest` are independently rebuilt from safe trace allowlist fields; disagreement is `corrupt/digest_mismatch`.
4. No `ReasonResult`/`Intent` deserialization occurs. The reporter reads only `AdvisorReasonTrace`/`AdvisorIntentTrace` payloads validated by the sidecar.
5. Changing only hidden reference IDs/goals/routes changes the in-memory assessment/report as expected but does not change sidecar bytes or non-reference comparison digest.
6. Report JSON never contains hidden objective IDs, raw goals, raw routes, hidden-reference digests, planner request text, provider errors, opaque-summary fragments, or raw flags.
7. A trace marked incomplete/corrupt remains excluded even if the supplied fixture would otherwise show a gain.

Use this exact per-case public model:

```python
NA = "N/A"

@dataclass(frozen=True, kw_only=True)
class AdvisorCaseEstimate:
    fixture_id: str
    summary_digest: str
    benchmark_run_id: str
    challenge_id: str
    source_event_seq: int
    dataset_status: Literal["clean", "incomplete", "corrupt"]
    trigger_eligible: bool
    trace_only: bool
    eligible_for_quality: bool
    exclusion_reasons: tuple[str, ...]
    assessment_verdict: str
    assessment_reason: str
    baseline_reference_coverage: float | str
    advisor_reference_coverage: float | str
    reference_coverage_delta: float | str
    baseline_intent_count: int | str
    advisor_intent_count: int | str
    intent_jaccard: float | str
    advisor_first_reference_count: int | str
    baseline_only_reference_count: int | str
    baseline_duplicate_count: int | str
    advisor_duplicate_count: int | str
    baseline_unsupported_citation_count: int | str
    advisor_unsupported_citation_count: int | str
    wall_seconds_delta: float | str
    input_tokens_delta: int | str
    output_tokens_delta: int | str
    usd_delta: float | str
```

- [ ] **Step 2: Run RED**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_report.py -q
```

- [ ] **Step 3: Implement strict read-only case reconstruction and reference assessment**

`build_case_estimate(fixture, trace_path)` must call `fold_advisor_trace()` directly and must not construct an `AdvisorTraceSink`. It validates the complete case identity, rebuilds both safe arm outcomes, recomputes the non-reference `IntentComparison`, and checks both `case_completed` digests before consulting hidden references. `build_missing_trace_estimate(fixture, failure_code)` creates an explicitly incomplete, all-metrics-`"N/A"` estimate for a case that could not create a sidecar; it does not inspect hidden references and accepts only fixed failure codes.

Reference matching is deterministic and reporter-local:

```text
A reference objective is valid only when:
  objective_id is non-empty and unique within the fixture; and
  at least one of canonical route_hash or normalized goal token bag is non-empty.

A safe intent covers a reference when:
  its non-empty route_fingerprint equals the reporter-computed fingerprint of the reference route_hash; or
  its goal_fingerprint equals the reporter-computed fingerprint of the reference goal.

A supported gain requires:
  an Advisor-covered reference not covered by baseline; and
  at least one Advisor-only matching intent whose from_facts is non-empty and fully
  contained in fixture.available_fact_seqs.
```

One intent may cover multiple references, but each reference contributes at most one unit. Reference IDs and any digest derived from hidden-reference fields are used only as in-memory set identities and are never emitted to the sidecar or report. This avoids presenting a low-entropy reference digest as an anonymity boundary.

`eligible_for_quality=True` iff the fold is clean, the trigger is eligible, both arms succeeded with valid safe traces, the fixture has at least one valid reference objective, and all identity/digest checks pass. Assessment fields follow an explicit gate: incomplete/corrupt, trigger-ineligible, identity/digest-invalid, and missing-reference cases use `assessment_verdict="N/A"` plus their fixed exclusion reason and do not run reference matching; a correctly terminalized planner failure/timeout uses `indeterminate_planner_error/planner_arm_not_successful`; only the remaining cases execute the reference-aware precedence below. A trigger-ineligible baseline-only case is valid trace evidence but has reason `trigger_ineligible`. A clean case with no references is `trace_only=True`, reason `missing_reference_objectives`, and is excluded from reference-quality metrics.

Compute `CaseAssessment` in this precedence order so verdicts cannot overlap:

1. Either arm not successful -> `indeterminate_planner_error`.
2. Advisor succeeded with no intents -> `rejected_advisor_empty`.
3. Both gained and lost reference sets non-empty -> `mixed`.
4. Lost non-empty and gained empty -> `regressed`.
5. Gained non-empty and lost empty:
   - at least one gained reference has a supported Advisor-only matching intent -> `accepted_reference_gain`;
   - otherwise -> `rejected_no_supported_delta`.
6. No gained/lost references:
   - no safe-intent planning delta -> `unchanged`;
   - otherwise -> `rejected_baseline_already_equivalent`.

Assessment reason enums remain those frozen in Task 1. No assessment or reference-derived field is retroactively written to the sidecar.

Usage and timing deltas use exact field-level rules:

```text
wall_seconds_delta:
  advisor.wall_seconds - baseline.wall_seconds only when both successful arms
  have finite non-negative wall_seconds; otherwise "N/A".

input_tokens_delta/output_tokens_delta/usd_delta:
  advisor - baseline only when both arms have usage_status="measured" and both
  values for that exact dimension are present and valid; otherwise "N/A".

estimated or unknown on either side:
  never coerced to zero and never satisfies a measured pair.
```

- [ ] **Step 4: Write aggregate/bootstrap, denominator, and claim-discipline tests**

Cover:

1. Sampling unit is `benchmark_run_id`, not fixture.
2. Multiple eligible fixtures in one run are averaged before cross-run aggregation/bootstrap.
3. Zero-delta runs remain in every applicable paired vector.
4. Runs with no quality-eligible case are excluded from the quality vector but remain in total run/case coverage.
5. Incomplete, corrupt, trace-only, trigger-ineligible, and planner-failed cases are counted in their coverage buckets and excluded from reference-quality denominators.
6. `accepted_reference_gain_rate = accepted_reference_gain_cases / quality_eligible_cases`; denominator zero yields literal `"N/A"`.
7. Wall/token/USD means each use their own field-level eligible case and run counts; missing/estimated values do not remove the case from unrelated metrics.
8. `<5` quality-eligible runs -> `insufficient`, point estimate allowed, CI `"N/A"`.
9. `5..19` quality-eligible runs -> `exploratory`, point estimate present, CI `"N/A"`.
10. `>=20` quality-eligible runs -> `reportable`, 95% percentile CI from 2000 paired run-level resamples.
11. Seed `20260816` produces byte-identical JSON and affects resampling only, never case/run order.
12. All forbidden production/causal metrics remain literal `"N/A"`.

- [ ] **Step 5: Implement aggregate model, field-level denominators, and deterministic JSON**

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorAggregateReport:
    kind: Literal["m8_offline_next_cycle_planning_estimate"]
    total_cases: int
    total_run_count: int
    clean_cases: int
    incomplete_cases: int
    corrupt_cases: int
    trigger_ineligible_cases: int
    trace_only_cases: int
    planner_unsuccessful_cases: int
    quality_eligible_cases: int
    quality_eligible_run_count: int
    accepted_reference_gain_cases: int
    accepted_reference_gain_denominator_cases: int
    wall_pair_cases: int
    wall_pair_run_count: int
    input_tokens_measured_pair_cases: int
    input_tokens_measured_pair_run_count: int
    output_tokens_measured_pair_cases: int
    output_tokens_measured_pair_run_count: int
    usd_measured_pair_cases: int
    usd_measured_pair_run_count: int
    evidence_tier: Literal["insufficient", "exploratory", "reportable"]
    mean_reference_coverage_delta: float | str
    reference_coverage_delta_ci95_low: float | str
    reference_coverage_delta_ci95_high: float | str
    accepted_reference_gain_rate: float | str
    mean_wall_seconds_delta: float | str
    mean_input_tokens_delta: float | str
    mean_output_tokens_delta: float | str
    mean_usd_delta: float | str
    real_flag_latency_improvement: Literal["N/A"]
    solve_rate_delta: Literal["N/A"]
    worker_starts_saved: Literal["N/A"]
    tokens_saved: Literal["N/A"]
    actual_focused_dispatch_latency: Literal["N/A"]
    race_outcome: Literal["N/A"]
    production_pause_stop_budget_correctness: Literal["N/A"]
    ooda_wakeup_latency: Literal["N/A"]
    cases: tuple[AdvisorCaseEstimate, ...]
```

Count invariants are explicit: `total_cases == len(cases)`; `total_run_count` is the number of distinct `benchmark_run_id` values across all case estimates; and `clean_cases + incomplete_cases + corrupt_cases == total_cases`. Trigger-ineligible, trace-only, planner-unsuccessful, and quality-eligible counts are named overlapping subsets rather than a partition. `accepted_reference_gain_denominator_cases == quality_eligible_cases`, and `accepted_reference_gain_cases` is a subset of that denominator.

For each metric, first average eligible case deltas within each `benchmark_run_id`, then average those run means so each run has equal weight. Bootstrap only the reference-coverage run-delta vector. The field-specific `*_pair_cases` and `*_pair_run_count` fields are the exact denominators for wall/token/USD means; a `*_pair_run_count` counts distinct runs containing at least one eligible pair for that field. `accepted_reference_gain_rate` is deliberately case-weighted and exposes its exact case denominator. Never label signed additional tokens/USD as savings.

Sort cases by `(benchmark_run_id, fixture_id)`. Serialize with `ensure_ascii=False`, `sort_keys=True`, compact separators, and `allow_nan=False`. No exception or validation path emits fixture summary/reference text.

- [ ] **Step 6: Run GREEN and commit M8-3**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_experiment.py tests/test_advisor_sidecar.py tests/test_advisor_runner.py tests/test_advisor_report.py -q
git diff --check
git add dswarm/swarm/advisor_report.py tests/test_advisor_report.py
git commit -m "M8-3 offline advisor planning-quality report"
```
---

### Task 5: Add the operator-local benchmark harness and CLI

**Files:**
- Create: `dswarm/swarm/advisor_benchmark.py`
- Create: `scripts/advisor_benchmark.py`
- Create: `tests/test_advisor_benchmark.py`
- Create: `tests/test_advisor_benchmark_cli.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes Tasks 1-4.
- Produces `AdvisorBenchmarkCase`, `AdvisorBenchmarkSuite`, `AdvisorBenchmarkCaseResult`, `AdvisorBenchmarkResult`, `AdvisorSuiteConstructionError`, `run_advisor_benchmark()`, and `benchmark_result_json()`.

- [ ] **Step 1: Write failing suite-validation and failure-taxonomy tests**

Use these exact case/suite fields:

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkCase:
    case_root: str | Path
    fixture: AdvisorFixture
    planner_factory: PlannerFactory
    timeout_s: float = 180.0
    cleanup_timeout_s: float = 5.0

@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkSuite:
    artifact_root: str | Path
    cases: Sequence[AdvisorBenchmarkCase]
    bootstrap_seed: int = 20260816
    bootstrap_samples: int = 2000
```

Normalize `suite.cases` to an immutable tuple before validation. Before any sink, factory, or planner is created, validate:

- `fixture_id` is unique across the suite.
- `artifact_root` resolves once and must itself be a strict descendant of the repository's ignored `eval_runs/m8-advisor/` or `sessions/` root. Every resolved absolute `case_root` is unique and is a strict descendant of that artifact root (never equal to it and never escapes through `..` or symlinks). Validation happens before creating directories.
- Source key `(benchmark_run_id, challenge_id, source_event_seq)` is unique. A duplicate source key is a suite-construction failure even when payloads are identical, because duplicate cases would change statistical weight. If attribution/summary differs, report only fixed code `source_identity_conflict`.
- `summary_digest` recomputes from the exact opaque summary.
- Timeout/cleanup values and bootstrap settings are finite positive values/in-range integers.
- Existing case roots are inspected in a read-only per-case preflight after structural suite validation. A matching clean/complete trace is reusable; incomplete/corrupt/partial or identity-mismatched data becomes a fixed-code case-local result. It is never silently rebound, appended, or treated as a suite-construction failure merely because old local evidence exists.

Test the exact failure layers:

```text
case execution outcome; continue when isolation is known clean:
  durably folded planner setup/call failure or cooperative timeout
    -> wrapper status="reported"; exclusion lives in AdvisorCaseEstimate
  sensitive planner output with a durable fixed-code terminal
    -> wrapper status="reported"; no rejected text
  case sidecar unavailable before any arm starts
    -> wrapper status="case_local_failure" with a synthetic incomplete estimate
  pre-existing or newly folded incomplete/corrupt/partial trace
    -> wrapper status="case_local_failure" with a fixed-code estimate

suite-fatal; no later case starts:
  duplicate fixture/root/source key or invalid suite settings
  AdvisorIsolationFailure / unresponsive cancelled task
  uncertainty about an owned task still running
  unexpected systemic reporter/serialization invariant failure

propagate unchanged:
  asyncio.CancelledError
  KeyboardInterrupt
  SystemExit

CLI-fatal, non-zero exit:
  module import/factory construction/type validation failure
  suite_factory_wrote_output or benchmark_wrote_output
  report JSON serialization failure
  output directory/temp/fsync/replace failure
```

A case-local sidecar initialization failure may have no durable case trace; create `build_missing_trace_estimate(...)`, return an `AdvisorBenchmarkCaseResult` with a fixed failure code, include that incomplete estimate in aggregate coverage, and continue because no planner task was created. Never put exception text, traceback, fixture content, or path-sensitive challenge text in that result.

Use these exact safe result models; paths, exceptions, fixture content, planner text, and hidden references are not fields:

```python
@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkCaseResult:
    fixture_id: str
    benchmark_run_id: str
    status: Literal["reported", "case_local_failure"]
    failure_code: str
    estimate: AdvisorCaseEstimate

@dataclass(frozen=True, kw_only=True)
class AdvisorBenchmarkResult:
    kind: Literal["m8_advisor_benchmark_result"]
    declared_case_count: int
    reported_case_count: int
    case_local_failure_count: int
    case_results: tuple[AdvisorBenchmarkCaseResult, ...]
    report: AdvisorAggregateReport
```

`failure_code` is empty only for `status="reported"`; case-local failures use exactly one of `sidecar_unavailable`, `advisor_writer_busy`, `existing_trace_incomplete`, `existing_trace_corrupt`, `existing_trace_partial`, or `existing_trace_identity_mismatch`. A planner failure/timeout that was durably folded and reported is still `status="reported"`—its exclusion reason lives in `AdvisorCaseEstimate`, not in the harness wrapper. The result invariants are `declared_case_count == len(case_results)`, `reported_case_count + case_local_failure_count == declared_case_count`, and each counter equals the number of wrappers carrying that status. The aggregate report receives exactly one estimate per declared case, including synthetic missing-trace estimates.

- [ ] **Step 2: Run RED**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_benchmark.py -q
```

- [ ] **Step 3: Implement sequential execution and source-event de-duplication**

Run cases in declared order. For each validated case:

1. Read-only preflight the writer lock and trace path before constructing a writer. A present lockfile—live or stale—returns fixed code `advisor_writer_busy` without folding, appending, or invoking a factory; M8 v1 never guesses ownership or breaks the lock.
2. If an existing trace is clean/complete and identity-matched, do not rerun either planner; build its case estimate read-only.
3. If an existing trace is incomplete, corrupt, partial, or cannot establish the expected identity, do not append or rerun; create a fixed-code case-local result/estimate and require a new case root for another attempt.
4. Only for an absent/empty trace, construct one `AdvisorTraceSink` for that case root.
5. Call `run_advisor_case(..., planner_factory=case.planner_factory, timeout_s=case.timeout_s, cleanup_timeout_s=case.cleanup_timeout_s)`.
6. Close/release the writer before read-only reporting.
7. Call `build_case_estimate(case.fixture, advisor_trace_path(case.case_root))`.
8. Add a fixed-code case result and continue only for the explicitly case-local failures above.
9. On `AdvisorIsolationFailure`, cancellation, `KeyboardInterrupt`, or `SystemExit`, do not start another case.

The harness must not auto-load `.env`, credentials, challenges, graphs, workers, network clients, or hidden references from disk. It never serializes an `AdvisorFixture` and never prints opaque summaries, hidden references, planner request/response text, provider errors, credentials, or environment values.

The operator suite may provide a real LLM planner. For M5-compatible attribution, it should use distinct solver identities such as `m8-baseline` and `m8-advisor` and return `AdvisorUsage` honestly. Canonical measured usage is `measured`; local estimates remain `estimated`; unavailable usage is `unknown`. The harness does not convert estimated/unknown values to measured.

Add root-anchored `/local_benchmarks/` to `.gitignore`. Keep suite modules there and set `artifact_root` below already ignored `eval_runs/m8-advisor/` or `sessions/`; every case root must remain below that resolved root. Tests must execute, not merely inspect text:

```powershell
git check-ignore -q local_benchmarks/example.py
git check-ignore -q eval_runs/m8-advisor/example.json
```

- [ ] **Step 4: Write failing CLI safety and atomic-output tests**

Cover:

1. Invalid `module:factory`, missing module/attribute, non-callable factory, factory exception, and wrong return type.
2. Python-level stdout/stderr emitted during module import or suite-factory construction is routed to a bounded discard guard that retains only a `wrote_any` flag and causes fixed code `suite_factory_wrote_output`; Python-level output emitted during benchmark/planner execution is guarded the same way and causes fixed code `benchmark_wrote_output`. Sensitive output bytes are neither retained nor echoed.
3. Without `--output`, success emits exactly one deterministic UTF-8 JSON line to stdout and nothing to stderr.
4. With `--output`, success leaves stdout/stderr empty and writes byte-identical JSON plus newline.
5. Output uses a same-directory temp file, flush, `os.fsync`, `os.replace`, and best-effort parent-directory fsync; failed replacement preserves any old report and leaves no successful exit.
6. Existing clean/complete case traces are reused read-only with zero planner/factory calls; existing incomplete/corrupt/partial traces are never appended or rerun and become fixed-code case-local estimates.
7. Parent creation/write/fsync/replace failures produce non-zero exit, empty stdout, and only a fixed error code on stderr.
8. `KeyboardInterrupt`/`SystemExit` are not converted into ordinary case failures.
9. No credential, fixture summary, hidden reference, prompt, provider response, exception message, or environment value appears in stdout/stderr.

- [ ] **Step 5: Implement `scripts/advisor_benchmark.py` with bounded import-side-effect claims**

```text
usage: advisor_benchmark.py [-h] [--output OUTPUT] suite

positional arguments:
  suite            operator suite factory as module:factory

options:
  --output OUTPUT  atomically write UTF-8 JSON to this file instead of stdout
```

`main()` redirects Python-level `sys.stdout`/`sys.stderr` during module import and suite-factory construction to a bounded discard guard that retains only whether any write occurred, never the sensitive text. Any write fails with only `suite_factory_wrote_output`. After suite validation, it installs fresh discard guards around the entire `asyncio.run(run_advisor_benchmark(suite))` execution as well. Any Python-level planner/provider/harness write fails with only `benchmark_wrote_output`; the possibly completed sidecars remain local evidence, but no report is emitted from that CLI invocation. The final report is serialized only after those guards have closed, so the CLI owns stdout/stderr exclusively.

This does not intercept native `os.write()`, child-process inherited handles, or malicious import hooks, so M8 v1 explicitly requires operator-local suite modules and planner adapters to be silent and trusted for process-level output side effects. Do not claim process-level sandboxing. On a silent successful run, serialization is exactly `benchmark_result_json(result) + "\n"`.

Before creating a parent or temp file, resolve the repository root and the requested output path without permitting a symlink/junction escape. The target must be a strict descendant of `<repo>/eval_runs/` or `<repo>/sessions/`; equality with either root and every other target are fixed-code `output_path_not_allowed` failures. Validation failure must not create directories, overwrite an old report, or leave a temp file. Stdout remains an explicit operator-selected export channel and is not subject to this path check.

For `--output`, create the parent, write a uniquely named temp file in the same directory, flush and fsync it, atomically `os.replace()` the target, and best-effort fsync the parent directory where supported. On failure, best-effort remove only the owned temp file, preserve the previous target, emit a fixed stderr code, and return non-zero. Without `--output`, write the single final line only after serialization succeeds.

Docstring examples:

```text
uv run python scripts/advisor_benchmark.py local_benchmarks.m8_suite:build_suite
uv run python scripts/advisor_benchmark.py local_benchmarks.m8_suite:build_suite --output eval_runs/m8-advisor/report.json
```

- [ ] **Step 6: Run GREEN and commit M8-4**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_benchmark.py tests/test_advisor_benchmark_cli.py -q
git check-ignore -q local_benchmarks/example.py
git check-ignore -q eval_runs/m8-advisor/example.json
git diff --check
git add .gitignore dswarm/swarm/advisor_benchmark.py scripts/advisor_benchmark.py tests/test_advisor_benchmark.py tests/test_advisor_benchmark_cli.py
git commit -m "M8-4 operator-local advisor benchmark harness"
```
---

### Task 6: Close documentation, architecture checks, and full verification

**Files:**
- Modify: `docs/10-v4-kernel-improvement-implementation.md` M8 section
- Modify: `tests/test_advisor_experiment.py`
- Verify all M8 files and the full suite

**Interfaces:**
- No new runtime interface.
- Post-implementation status only: `Implemented (offline evidence collection only); production Advisor remains No-Go.`

- [ ] **Step 1: Add final static boundary and serialization tests**

AST-scan all M8 modules and reject imports of production graph/scheduler/EventBus/UI/gate modules. Inspect production files and reject M8 imports:

```python
PRODUCTION_FILES = (
    Path("dswarm/swarm/reason_scheduler.py"),
    Path("dswarm/swarm/shared_graph.py"),
    Path("dswarm/solver/reason.py"),
    Path("dswarm/solver/gate.py"),
)


def test_production_paths_do_not_import_m8_experiment():
    for path in PRODUCTION_FILES:
        source = path.read_text("utf-8")
        assert "advisor_experiment" not in source
        assert "advisor_runner" not in source
        assert "advisor_sidecar" not in source
        assert "advisor_report" not in source
        assert "advisor_benchmark" not in source
```

Also statically reject generic planner-output serialization (`asdict`, `model_dump`, `vars`, `__dict__`, `Intent.to_payload`) in M8 trace/runner modules. Exercise every sidecar event encoder and recursively assert that prohibited keys/free-text fields, hidden reference IDs, `error_message`, request text, prompt text, raw flags, and arbitrary exception strings cannot be encoded. `advisor_experiment.py` may define, freeze, and structurally validate `reference_objectives`; `advisor_report.py` is the only module allowed to semantically inspect them for coverage/assessment. AST tests must reject `reference_objectives` attribute reads or serialization in `advisor_runner.py`, `advisor_sidecar.py`, `advisor_benchmark.py`, and the CLI.

Add exact lifecycle regressions for:

- single-writer lock and fixture-root binding;
- both arm orders using one `stage="setup"` start per attempted arm;
- `suggestion_consumed` as the only Advisor task-admission witness, without claiming upstream provider receipt;
- interruption/orphan-start incomplete folding;
- duplicate terminal/post-completion event corruption;
- reporter-only assessment and hidden-reference non-persistence;
- field-level usage denominators and unknown/estimated non-zero semantics;
- suite-fatal isolation failure preventing all later cases.

- [ ] **Step 2: Replace the stale M8 section in docs/10 after implementation**

Document the implemented offline modules, sidecar path, frozen opaque fixtures, `summary_digest`, explicit safe trace allowlist, deterministic physical-order DFA, single-writer rule, per-arm planner factory, owned-task timeout cleanup, reporter-only hidden references, field-specific measured-pair denominators, local-sensitive storage, CLI atomic-output behavior, and reportable/N/A metrics.

State explicitly:

- Production summaries may already contain `_captured_flags_block()`; baseline preserves the opaque prefix unchanged.
- The Advisor delta never copies/extracts raw flags or hidden reference content.
- No free-text `ReasonResult`/`Intent` fields or exception messages are persisted.
- `CaseAssessment` is computed in memory by the reporter and is not written back to sidecar.
- Production schema, scheduler, Reason prompt, dispatch, UI, pause/stop/budget, wakeup behavior, provenance gate, and cost ledger are unchanged.
- M8 v1 has owned asyncio task cleanup but not process-level sandboxing; operator suite modules and planner adapters are trusted. Python-level output is captured and converted to fixed CLI failure codes, while native/child-process output remains a declared unsupported boundary and therefore must be silent.

Retain future production RFC prerequisites: watcher ownership, event wakeup, gather cancellation/wait strategy, formal Intent conversion gate, budget/cooldown/cursor, provider/process isolation, and a real end-to-end latency experiment.

Only after code and all verification are green, set status exactly:

```text
Implemented (offline evidence collection only); production Advisor remains No-Go.
```

Before final verification is green, this plan and `docs/10` must continue to say approved for implementation/Not Implemented. Never describe this as “Advisor unlocked” or “OODA fast path implemented”.

- [ ] **Step 3: Run all focused M8 tests**

```powershell
$env:PYTHONUTF8='1'; uv run pytest tests/test_advisor_experiment.py tests/test_advisor_sidecar.py tests/test_advisor_runner.py tests/test_advisor_report.py tests/test_advisor_benchmark.py tests/test_advisor_benchmark_cli.py -q
```

- [ ] **Step 4: Run syntax, ignore-rule, and architecture checks**

```powershell
$env:PYTHONUTF8='1'; uv run python -m py_compile dswarm/swarm/advisor_experiment.py dswarm/swarm/advisor_sidecar.py dswarm/swarm/advisor_runner.py dswarm/swarm/advisor_report.py dswarm/swarm/advisor_benchmark.py scripts/advisor_benchmark.py
git grep -n "advisor_\|AdvisorTrace\|flag_scout_trigger" -- dswarm/swarm/reason_scheduler.py dswarm/swarm/shared_graph.py apps dswarm/solver/gate.py
git check-ignore -q local_benchmarks/example.py
git check-ignore -q eval_runs/m8-advisor/example.json
git diff --check
```

Expected: compilation and both ignore checks succeed; `git grep` has no matches; diff check has no output.

- [ ] **Step 5: Run the full repository suite**

This Windows checkout must use PowerShell rather than WSL `./init.sh`:

```powershell
$env:PYTHONUTF8='1'; uv run pytest -q
```

Expected: exit code 0. Record skips and warnings without treating existing warnings as M8 failures.

- [ ] **Step 6: Review final scope, local artifacts, and secrets**

```powershell
git status --short
git diff --stat
git diff -- dswarm/swarm/reason_scheduler.py dswarm/swarm/shared_graph.py dswarm/solver/reason.py dswarm/solver/gate.py apps
git ls-files local_benchmarks eval_runs sessions
```

Expected: the production-path diff and tracked-local-artifact listing are empty. M8 consists only of five offline modules, one CLI, six test files, `.gitignore`, this implementation plan, and the M8 documentation section. No secret, opaque summary, local fixture/suite, report, metrics JSONL, planner request/output, hidden reference, or `.env` file is tracked.

- [ ] **Step 7: Commit M8-5**

```powershell
git add docs/10-v4-kernel-improvement-implementation.md tests/test_advisor_experiment.py
git commit -m "M8-5 document offline advisor experiment boundary"
```

---

## Acceptance Mapping to §12.10

| §12.10 requirement | Implementation evidence |
|---|---|
| No production `suggestions` table/event | Static boundary tests; no SharedGraph/EventBus import or schema change |
| No production `_run_reason()`/summary change | Baseline opaque-prefix byte-equality test; production files import no M8 modules |
| Suggestion block only in experiment runner | Fixed JSON-encoded block, source-token validation, raw-flag no-copy, and hidden-reference isolation tests |
| No sensitive/free-text trace leakage | Explicit `AdvisorReasonTrace`/`AdvisorIntentTrace` allowlist; generic dump rejection; fixed error codes only |
| Append-only suggestion and arm lifecycle | Single-writer fsync sidecar plus deterministic physical-order DFA for both arm orders |
| Reliable admission/timeout/interruption semantics | `suggestion_consumed` task-admission witness (not provider-receipt proof); runner-owned task; bounded cancellation cleanup; interruption folding; isolation failure stops suite |
| Complete baseline/Advisor evidence without raw output | Safe structured trace, timing, three-state usage, fixed failure codes, independently verified digests |
| Acceptance/rejection reasons | Reporter-only deterministic `CaseAssessment`; no reference-derived assessment persisted to sidecar |
| Next-cycle intent differences | Equivalence, overlap, hidden-reference coverage, duplicate counts, and one-unit-per-intent citation rules |
| Fixture/source identity and replay integrity | Fixture/summary/run/source binding, unique source key, case-root binding, idempotent exact duplicates, corrupt semantic duplicates |
| Honest usage/cost deltas | `measured | estimated | unknown`; exact per-dimension measured-pair denominators; missing never becomes zero |
| Honest statistical aggregation | Cases averaged within run, run-level bootstrap, zero deltas retained, explicit coverage/denominator fields |
| Operator-local safety boundary | Single writer, write-once trace roots, git-ignored suite/run roots, controlled import and benchmark-execution output, atomic report write, fixed-code CLI failures |
| Honest claim boundary | Fixed N/A fields and report kind `m8_offline_next_cycle_planning_estimate` |
| Pause/stop/budget deferred | No production lifecycle wiring; future RFC prerequisites retained |
| Production remains No-Go | Final status text and no-import/no-diff regression |

## Definition of Done

- This document records Contract v2 as approved for implementation before the first M8 production file is created.
- Six independent, test-backed commits exist in order: M8-0 through M8-5; no M7 file or commit is mixed into them.
- Safe fixture/source validation, opaque-summary preservation, raw-flag no-copy, prompt/reference isolation, safe trace allowlists, lifecycle DFA, single-writer locking, owned-task cleanup, interruption closure, reporter, benchmark, and CLI tests are deterministic and green.
- Every planner arm comes from a distinct `PlannerFactory` instance; every started arm has a deterministic terminal or leaves an explicitly incomplete interrupted trace; an unclean cancellation cannot permit a later arm/case.
- Hidden references are structurally frozen/validated by `advisor_experiment.py` and semantically read only by the reporter; sidecar bytes remain unchanged when only hidden references change; no hidden-reference digest, free-text planner output, or exception message is durable or emitted in the report.
- Identity/digest reconstruction, citation rules, usage three-state semantics, field-specific denominators, run-level aggregation, and all fixed `"N/A"` claims are covered by tests.
- `git check-ignore` proves `/local_benchmarks/` and `eval_runs/` examples are ignored; import-time and benchmark-execution Python output are contained; atomic CLI output and fixed-code failure paths are tested.
- Full `uv run pytest -q` exits 0 and `git diff --check` is clean.
- Production graph/scheduler/Reason/gate/UI/event/cost-ledger diffs are empty.
- A local prepared benchmark can supply ScriptedLLM or a real planner through `module:factory` without committing credentials or challenge material; estimated/unknown usage remains honestly labeled.
- The report supports or rejects a claim about offline next-cycle planning output only; it is not evidence of faster real dispatch, lower flag latency, improved solve rate, lifecycle correctness, or production readiness.
