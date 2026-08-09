# D-Swarm pi base extensions

These TypeScript extensions are baked into the worker base image at
`/opt/dswarm/pi-config/extensions/` and linked into every isolated worker HOME's
`.pi/agent/extensions/` at runtime (see `swarm.py::_ensure_pi_config_links`).
pi loads every `*.ts` here via jiti (no compilation step needed).

## Current extensions (all always-on)

| File | Hooks | Purpose |
|------|-------|---------|
| `ctf-gateway-provider.ts` | `registerProvider` | Registers the `ctf-gateway` model provider (deepseek-v4-flash/pro) against the host gateway; no-op without `DSWARM_TASK_TOKEN`. |
| `ctf-blackboard-watchdog.ts` | `tool_call` | Caps `blackboard.py` command timeouts (default 30s, override `DSWARM_BLACKBOARD_TIMEOUT_SECONDS`) so a hung board cannot wedge the worker. |
| `ctf-provenance-guard.ts` | `tool_call` | Blocks raw shell writes to `shared_graph.db` / `winner.json`, redirecting to `blackboard.py`. |
| `ctf-context-injector.ts` | `before_agent_start` | Appends run metadata + the evidence protocol (FOUND_FLAG / VERIFIED_FACT / DEADEND) to the system prompt, idempotently. |
| `ctf-evidence-note.ts` | `registerTool` | `ctf_evidence_note` tool appends JSONL evidence notes under the current cwd (`.dswarm/evidence.jsonl`) so findings survive compaction. |

## Type checking

`./tsconfig.json` + `types/` let the host-side checker run `tsc --noEmit`
offline against the pi 0.84.1 extension API subset. `types/pi-coding-agent.d.ts`
mirrors the real declarations from
`@earendil-works/pi-coding-agent/dist/core/extensions/types.d.ts`; regenerate it
when the locked pi version changes.

## Adding extensions

Drop a new `ctf-*.ts` here (default-export `(pi: ExtensionAPI) => void`, wrap
handlers in try/catch, only import `@earendil-works/pi-coding-agent` (type-only),
`typebox`, `node:*` or relative `./` modules). Direction-specific extensions go
under `docker/worker-pi/directions/<dir>/extensions/` in a later round.
