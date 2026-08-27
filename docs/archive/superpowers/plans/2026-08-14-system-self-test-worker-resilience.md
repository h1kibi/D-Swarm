> 状态：历史档案 —— 已被 [docs/00-architecture-spec.md](../../00-architecture-spec.md) 取代；本文保留作为时代记录。

# System Self-Test and Worker Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the main UI testing/diagnostics from a worker-startup smoke test into a full-flow system exercise, and add user-facing LLM provider error classification, worker recovery feedback, and batch-failure alerts.

**Architecture:** Keep the frontend as a bus/API subscriber and do not bypass solver core. Add focused backend units: one for provider/runtime error classification and aggregation, one for deterministic/real full-flow self-test orchestration layered beside the existing `StartupTestController`. Extend existing SSE events rather than inventing a second UI channel.

**Tech Stack:** Python/FastAPI/pytest/SSE for backend, Next.js/React/Vitest for UI, existing `RunManager`, `StartupTestController`, `InsightBus`, `SQLiteSharedGraph`, and worker profile configuration.

**Spec:** User-confirmed requirements in chat on 2026-08-14: full-flow test may spend real LLM calls against local benchmark, must test all enabled workers, must cover stop/hint/resume including stop-after-user, worker crash, backend restart, app restart semantics, hints may be consumed by next worker but must immediately become blackboard directives and be clearly surfaced in UI, batch provider alert thresholds are: same provider/account 3 fatal/auth/quota errors in 60s, >50% active workers same class, and first fatal quota/auth gets visible warning.

## Global Constraints

- Preserve provenance gate: do not weaken `dswarm/solver/gate.py` or anti-laundering checks.
- Preserve frontend boundary: UI calls backend APIs/SSE only; it must not invoke solver core directly.
- Preserve append-only shared graph semantics.
- Default self-test target is local prepared benchmark; real LLM calls are allowed but should be explicit in UI copy.
- Test all enabled worker profiles.
- Ordinary hint/focus does not need to interrupt a live single-shot worker; it must immediately become an operator directive and be consumed by a future worker/intent.
- Batch provider alert threshold: same provider/account 3 fatal/auth/quota errors in 60 seconds or same class affecting >50% active workers.
- New behavior must have deterministic tests; live provider tests must be opt-in/skip without keys.

---

## File Structure

- Create `apps/web/provider_errors.py`: pure classification and aggregation of provider/worker runtime errors into stable, user-facing diagnostics.
- Modify `apps/web/startup_test.py`: extend startup/full-flow events with provider-error summaries and full-flow phases; keep existing startup API compatible.
- Modify `apps/web/routes/startup_test.py`: accept mode/options for startup vs full-flow; default remains existing startup smoke test.
- Modify `apps/web/ui/components/StartupTestPanel.tsx`: render quick startup vs full-flow stages, hint-delivery semantics, provider alerts, and recovery summaries.
- Modify `apps/web/ui/lib/useRun.ts`: add request types/options if needed, without breaking existing callers.
- Add/modify tests:
  - `tests/test_provider_errors.py`
  - `tests/test_startup_test.py`
  - `tests/test_web_server.py` or route-specific startup tests
  - `apps/web/ui/test/startupTest.test.tsx` / existing startup test file

## Task 1: Provider error classification and batch aggregation

**Files:**
- Create: `apps/web/provider_errors.py`
- Test: `tests/test_provider_errors.py`

**Interfaces:**
- Produces: `classify_provider_error(message: str, *, provider: str = "", account_id: str = "", worker_id: str = "") -> ProviderErrorDiagnostic`
- Produces: `ProviderErrorAggregator(window_s: float = 60.0, fatal_threshold: int = 3, majority_ratio: float = 0.5)` with `.record(diag, now, active_workers)` returning alert dict or `None`.

- [ ] **Step 1: Write failing tests**

```python
from apps.web.provider_errors import ProviderErrorAggregator, classify_provider_error


def test_classifies_transient_network_error_as_retryable():
    diag = classify_provider_error(
        "ConnectTimeout: connection reset by peer",
        provider="deepseek",
        account_id="main",
        worker_id="pi-web",
    )
    assert diag.category == "transient_network"
    assert diag.severity == "warning"
    assert diag.retryable is True
    assert "自动重试" in diag.user_message


def test_classifies_insufficient_balance_as_fatal_quota():
    diag = classify_provider_error(
        "402 insufficient balance: please recharge your account",
        provider="deepseek",
        account_id="main",
        worker_id="pi-pwn",
    )
    assert diag.category == "insufficient_quota"
    assert diag.severity == "fatal"
    assert diag.retryable is False
    assert diag.should_pause_dispatch is True


def test_aggregator_alerts_after_three_fatal_errors_in_window():
    agg = ProviderErrorAggregator(window_s=60, fatal_threshold=3, majority_ratio=0.5)
    alerts = []
    for i in range(3):
        diag = classify_provider_error(
            "insufficient balance",
            provider="deepseek",
            account_id="main",
            worker_id=f"pi-{i}",
        )
        alert = agg.record(diag, now=100 + i, active_workers=8)
        if alert:
            alerts.append(alert)
    assert alerts
    assert alerts[-1]["type"] == "provider.batch_alert"
    assert alerts[-1]["category"] == "insufficient_quota"
    assert alerts[-1]["count"] == 3
    assert alerts[-1]["should_pause_dispatch"] is True


def test_aggregator_alerts_when_majority_workers_hit_same_error():
    agg = ProviderErrorAggregator(window_s=60, fatal_threshold=99, majority_ratio=0.5)
    alert = None
    for i in range(3):
        diag = classify_provider_error(
            "rate limit exceeded",
            provider="openai-compatible",
            account_id="team",
            worker_id=f"pi-{i}",
        )
        alert = agg.record(diag, now=200 + i, active_workers=5)
    assert alert is not None
    assert alert["affected_workers"] == 3
    assert alert["active_workers"] == 5
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_provider_errors.py -q --color=no`
Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement minimal pure module**

Implement dataclass fields: `category`, `severity`, `retryable`, `should_pause_dispatch`, `provider`, `account_id`, `worker_id`, `raw_message`, `user_message`, `suggested_action`.

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_provider_errors.py -q --color=no`
Expected: PASS.

## Task 2: Surface provider diagnostics in startup/full-flow test events

**Files:**
- Modify: `apps/web/startup_test.py`
- Test: `tests/test_startup_test.py`

**Interfaces:**
- Consumes: `classify_provider_error`, `ProviderErrorAggregator`.
- Produces event type `provider.error` for individual classified failures.
- Produces event type `provider.batch_alert` for threshold alerts.
- Existing `test.started`, `worker.phase`, `worker.event`, `test.done` stay compatible.

- [ ] **Step 1: Write failing tests**

Add tests that inject a `run_worker_test` returning `{ok: False, detail: "insufficient balance", provider: "deepseek", account_id: "main"}` for three workers and assert `provider.batch_alert` appears in session events.

- [ ] **Step 2: Run targeted test to verify fail**

Run: `uv run pytest tests/test_startup_test.py::test_startup_test_emits_provider_batch_alert -q --color=no`
Expected: FAIL because event is not emitted.

- [ ] **Step 3: Implement event emission**

In `_run`, after a worker outcome failure, classify `detail`, emit `provider.error`, feed aggregator with `active_workers=len(profiles)`, emit `provider.batch_alert` if returned. Include `retryable`, `severity`, `category`, `user_message`, `suggested_action`, `should_pause_dispatch`.

- [ ] **Step 4: Run startup tests**

Run: `uv run pytest tests/test_startup_test.py -q --color=no`
Expected: PASS.

## Task 3: Add full-flow self-test mode scaffold

**Files:**
- Modify: `apps/web/startup_test.py`
- Modify: `apps/web/routes/startup_test.py`
- Test: `tests/test_startup_test.py`

**Interfaces:**
- `StartupTestController.start(mode: str = "startup", benchmark: str = "local-smoke")`
- `StartupTestSession.mode`
- Event phases for `full_flow`: `benchmark.loaded`, `blackboard.checked`, `reason.checked`, `hint.checked`, `btw.checked`, `stop.checked`, `resume.checked`, `recovery.checked`.

- [ ] **Step 1: Write failing route/controller tests**

Test `POST /api/startup-test` with body `{"mode":"full_flow","benchmark":"local-smoke"}` returns test id and emits `test.started` with `mode="full_flow"`.

- [ ] **Step 2: Run failing tests**

Run: `uv run pytest tests/test_startup_test.py -q --color=no`
Expected: FAIL on missing mode support.

- [ ] **Step 3: Implement mode plumbing only**

Add mode fields and validation. For now full_flow runs the existing per-worker startup test plus emits the scaffold phase events as skipped/unchecked if no injected full-flow runner is provided.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_startup_test.py -q --color=no`
Expected: PASS.

## Task 4: Deterministic full-flow contract test harness

**Files:**
- Modify: `apps/web/startup_test.py`
- Test: `tests/test_startup_test.py`

**Interfaces:**
- Add injectable `run_full_flow_test: Callable[[StartupTestSession], Awaitable[dict[str, Any]]] | None`.
- Full-flow result summary includes `checks: list[dict]` where each check has `id`, `ok`, `detail`.

- [ ] **Step 1: Write failing test**

Create fake full-flow runner that emits/checks blackboard, reason, hint directive, btw, stop, resume, recovery. Assert summary contains all checks and `ok=True` only when all pass.

- [ ] **Step 2: Run failing test**

Run: `uv run pytest tests/test_startup_test.py::test_full_flow_runner_summarizes_required_checks -q --color=no`
Expected: FAIL.

- [ ] **Step 3: Implement injected runner path**

If mode is `full_flow` and runner is supplied, run worker startup for all enabled profiles, then run full-flow checks, merge summaries.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_startup_test.py -q --color=no`
Expected: PASS.

## Task 5: UI rendering for full-flow and provider alerts

**Files:**
- Modify: `apps/web/ui/components/StartupTestPanel.tsx`
- Modify: `apps/web/ui/lib/useRun.ts`
- Test: `apps/web/ui/test/startupTest.test.tsx` or existing startup test file

**Interfaces:**
- UI understands event types `provider.error`, `provider.batch_alert`, `flow.check`.
- UI displays hint semantics copy: “普通提示已写入黑板 directive；当前 single-shot worker 可不中断，下一个 worker/intent 会消费。”

- [ ] **Step 1: Write failing UI tests**

Render panel with synthetic events or component helper and assert provider batch alert text and full-flow check rows are visible.

- [ ] **Step 2: Run failing UI test**

Run: `npm test -- startupTest.test.tsx`
Expected: FAIL on missing rendering.

- [ ] **Step 3: Implement UI rendering**

Add alert list, full-flow checklist, and mode label. Keep existing worker rows.

- [ ] **Step 4: Run UI tests**

Run: `npm test -- startupTest.test.tsx`
Expected: PASS.

## Task 6: Verification pass

**Files:**
- No new files except potential local notes.

- [ ] **Step 1: Run backend targeted tests**

Run: `uv run pytest tests/test_provider_errors.py tests/test_startup_test.py -q --color=no`
Expected: PASS.

- [ ] **Step 2: Run full backend suite**

Run: `uv run pytest -q --color=no`
Expected: PASS or only known skips/warnings.

- [ ] **Step 3: Run frontend tests and build**

Run: `npm test`
Expected: PASS.

Run: `npm run build`
Expected: PASS.

- [ ] **Step 4: Diff hygiene**

Run: `git diff --check`
Expected: no whitespace errors.

## Self-Review

- Spec coverage: Worker-all-enabled coverage remains in existing startup loop and is retained; full-flow mode scaffold and injected runner cover reason/blackboard/hint/BTW/stop/resume/recovery contracts; provider classifier/aggregator covers transient vs fatal and batch thresholds.
- Placeholder scan: no implementation placeholders are required for the first executable slice; real benchmark runner details can be iterated after scaffold and tests land.
- Type consistency: `ProviderErrorDiagnostic`, `ProviderErrorAggregator.record`, `StartupTestController.start(mode, benchmark)` are consistently referenced.
