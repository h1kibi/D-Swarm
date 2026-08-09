# Changelog

All notable public release changes are tracked here.

## 0.3.0-rc.1 - 2026-08-10

### Added

- Route A P3: host-side model gateway (task-token reverse proxy) — worker
  containers never see the real upstream key; per-run token issue/revoke,
  per-run usage ledger, `ctf-gateway` pi provider extension (image pi aligned
  to 0.83.0).
- Route A P4: run scheduler — FIFO queue + global concurrency cap (default 5,
  1–8), queued pause/resume/cancel, `/api/scheduler`, `run.queued` /
  `run.dispatched` / `run.cancelled` events; the deck renders queued/cancelled
  states with position.
- Route A P5: `eval_nyu` benchmark harness (oracle / runner / report / CLI) —
  NYU + local datasets, docker target lifecycle, engine roster with pi,
  resume-safe results, historical baseline ingestion, pi-vs-baseline report.
  Local pilot: pi 2/2 solved (cdut md5 + baby_rce), $0.008.
- Route A P6: Wails desktop shell (`desktop/`) — native window over the
  FastAPI backend + Next deck, supervised child processes, clean shutdown.

### Fixed

- Fixed draft-run attachment upload returning 422 when using the file-picker button: the live `FileList` was cleared before the async upload finished on new solves.
- Gateway worker relay: the per-worker env `DSWARM_PI_PROVIDER` now wins over
  the host-built `--provider` flag; pi 0.81+ resolves the provider from the
  model, so gateway workers default `DSWARM_WORKER_MODEL=deepseek-v4-flash`.
- `ensure_container` recreates the control dir before writing the token —
  Docker Desktop's bind-mount cache could serve a stale token and the
  supervisor's Hello was rejected (`unauthorized`).
- Rejected unclosed angle-bracket placeholder flags (`FOUND_FLAG=<the flag>`).
- Windows host portability: board file + blackboard skill subprocess now
  write/read explicit UTF-8 (GBK locale).

### Changed

- Release metadata and package versions now point at `0.3.0-rc.1`; the default
  container worker image is `ghcr.io/h1kibi/dswarm-worker-pi:0.3.0-rc.1`.
- The GitHub release workflow now publishes the all-in-one Kali pi worker as
  `dswarm-worker-pi` and does not move `latest` tags for pre-releases.

## 0.2.5 - 2026-06-30

### Changed

- Release metadata, package versions, and worker build examples now point at `0.2.5`.

### Fixed

- Resolved container workspace permission mismatches by detecting the worker image's actual `kali` UID/GID before chowning shared run state.

## 0.2.4 - 2026-06-30

### Changed

- Release metadata, package versions, and worker build examples now point at `0.2.4`.

### Fixed

- Fixed Codex custom endpoint dispatch so a settings-page credential account `base_url` is applied to the actual worker profile instead of falling back to OpenAI.
- Made Codex custom endpoint health checks run the real Codex CLI Responses turn, surfacing LiteLLM/DeepSeek schema failures before a run starts.
- Preserved file-backed API key probing for Codex custom endpoints by injecting the resolved key into the CLI health-check environment.

## 0.2.3 - 2026-06-29

### Added

- Added the `/btw` side-query drawer to the web command deck for quick, local multi-turn Q&A over a run.
- Added a worker-backed `/api/runs/{run_id}/btw` stream that starts a short-lived read-only CLI worker for each turn.
- Added deterministic tests for `/btw` prompt construction, transcript handling, read-only graph access, and worker-slot isolation.
- Documented in Worker Settings that `/btw` follows the configured Review worker by default.

### Changed

- `/btw` now reads run files, JSONL, shared graph state, winner snapshots, and artifacts through the worker instead of answering from a compressed summary.
- `/btw` defaults to the configured Review worker when the frontend does not specify a profile, while still allowing explicit API overrides.
- Release metadata, package versions, and worker build examples now point at `0.2.3`.
- Expanded `.env.example` into a fuller operator map covering web auth, compose deployment, worker backends, `/btw` timeouts, credential fallbacks, CLI binary overrides, retention, and internal runtime envs.
- Aligned the default worker image across backend code, Worker Settings, Docker Compose, and docs on `ghcr.io/fishcodetech/dswarm-worker:latest`.

### Fixed

- Reduced `/btw` answer distortion by letting the side worker inspect source run evidence directly.
- Kept `/btw` out of swarm scheduling, review concurrency, max-worker slots, graph writes, and run cost accounting.
- Removed the redundant read-only explainer banner from the `/btw` drawer.
- Fixed Docker Compose env passthrough for `DSWARM_DEEPSEEK_BASE_URL`, `DSWARM_LLM_TRUST_ENV`, and custom worker network names.

## 0.2.1 - 2026-06-29

### Added

- Added Docker deployment documentation to both English and Chinese READMEs.
- Documented the official GHCR images for the web API, UI, full worker, and slim worker.
- Added guidance for choosing the full worker image versus the slim worker image.

### Changed

- `./run.sh web` is now documented as a production Next.js build/server path rather than a Next dev server.
- The default container worker image now points to `ghcr.io/fishcodetech/dswarm-worker:latest`.
- Docker Compose deployment docs now clarify that compose builds the control plane from the checkout but expects the worker image to exist on the host Docker daemon.
- Release/build script examples now use the `ghcr.io/fishcodetech/*` image namespace.

### Fixed

- Fixed GHCR release workflow image tags by lowercasing the registry owner namespace.
- Excluded generated worker build artifacts from public release syncs.
- Passed `DSWARM_DEEPSEEK_API_KEY` through Docker Compose into the `web-api` container.

## 0.2.0 - 2026-06-29

### Added

- Published the initial public release with GHCR images for the web API, UI, full worker, and slim worker.

### Changed

- Switched the local web command deck runner to production-mode Next.js serving.
- Improved container worker probing and standby behavior so worker checks run in container mode when configured.
