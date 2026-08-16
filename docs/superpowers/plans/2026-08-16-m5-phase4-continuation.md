# M5 Phase 4 continuation implementation plan

## Scope
Continue the approved M5 v4.1 Phase 4 producer wiring without entering Phase 5. Preserve provenance gate, anti-laundering, shared-graph fact semantics, existing COST_UPDATE consumers, and all unrelated uncommitted work.

## Tasks
1. Baseline and inspect current Phase 4 interfaces.
   - Run `./init.sh` / focused tests.
   - Inspect UsageJournal, UsageWriter, LLMClient, ModelGateway, RunManager, BTW, titler, summarizer.
2. Harden worker-token rollback.
   - Add a deterministic regression test for claim/setup failure.
   - Make `_make_cli_worker` revoke its per-worker token on all post-issue failures.
3. Add gateway UsageJournal producer.
   - Add a synchronous, injectable journal/bridge boundary suitable for ThreadingHTTPServer.
   - Persist `call_started` before upstream request; persist terminal record after JSON/stream completion or error.
   - Preserve legacy gateway usage JSONL as a producer-local buffer.
   - Add tests for ordering, fail-closed preflight, JSON, streaming, errors, claims, and identity.
4. Connect real internal producer paths.
   - Locate actual title, summary, Reason, and BTW LLM construction paths.
   - Inject UsageWriter/UsageContext only for real billable calls; keep diagnostics/probes out.
   - Add deterministic tests proving injection and no duplicate producer accounting.
5. Verification.
   - Run focused Phase 4 tests, py_compile, full pytest, and `git diff --check`.
   - Do not claim Phase 4 complete if gateway or business-path wiring remains incomplete.
