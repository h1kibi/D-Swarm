# Project D-Swarm

An autonomous multi-agent swarm for CTF solving and authorized offensive security.
You hand it a challenge; it launches a swarm of coding-agent workers that attack it
in parallel through a **shared, append-only evidence graph**, and every accepted
result traces back to **real execution output** — nothing else counts.

> ⚠️ Offensive-security automation: use only for authorized CTFs, your own ranges,
> and written-permission engagements.

## How it works

- **Workers are complete CLI agents** (`pi` is the current engine), each running its
  own agentic loop inside a sandboxed Kali container with bash/python/ghidra/pwntools.
- **One shared blackboard**: discovered facts, dead ends, PoC artifacts and intents
  live on an event-sourced SQLite graph (append-only, replayable). Workers coordinate
  through it; an independent Reason phase reads the graph and proposes typed intents.
- **A race past a hardcoded provenance gate**: first worker whose flag appears in
  real stdout/stderr/artifacts scores; placeholder or laundered flags are rejected,
  zero false positives by design.
- **Verified-PoC gate for pentest mode**: blocker findings are confirmed only after
  the registered reproduction command is replayed inside the verifier container and
  its indicator reappears in fresh output.

## Capability snapshot (kernel milestones M0–M9a)

| Area | What ships |
| --- | --- |
| Worker runtime | Docker-first runtime pools (M9a): frozen pool generations, RCP-v2 control link, reverse-dial supervisor; workers never see Docker socket / host HOME / full credential stores |
| Credentials | Task-token model gateway: worker containers exchange tokens, never upstream API keys |
| Budgets | Unique usage ledger (M5): run-scoped accounting, profile budget gates, spawn guard |
| Coordination | Race mode by default; opt-in two-stage coordinator via `stage_policy` (explore → review) |
| Pentest | Origin/Goal/Hints framing, Verified-PoC reproduction gate, post-hoc scope audit (M9) |
| Correctness | Strict event immutability guards (M3), direction authority chain with audit trail (M4) |

Implementation ledger: [docs/10](docs/10-v4-kernel-improvement-implementation.md).
Authoritative architecture spec: [docs/00-architecture-spec.md](docs/00-architecture-spec.md).

## Quick start

```bash
uv sync --extra dev          # install deps (Python >= 3.13, uv)
uv run pytest -q             # test suite (live tests skip without keys)
./run.sh web                 # FastAPI backend (:8000) + Next.js deck (:3001)
```

Configuration via `DSWARM_*` env vars (see `.env.example`). The Reason planner needs
`DSWARM_DEEPSEEK_API_KEY`. Prefer a terminal: `./run.sh tui --swarm --key <k>`.

## Repository map

| Path | Contents |
| --- | --- |
| `dswarm/` | Kernel: swarm orchestration, solver execution layer, shared evidence graph, provenance gate, event spine |
| `apps/web/` | FastAPI backend (:8000) + Next.js deck (:3001); apps/tui for terminal use |
| `cmd/runtime-agent/` | In-container Go supervisor (reverse connection) |
| `docker/` | Unified Kali worker image |
| `skills/dswarm-blackboard/` | Worker ↔ blackboard interface skill |
| `docs/` | Authoritative spec, operations runbooks, milestone ledger; historical drafts under `docs/archive/` |

## Lineage

D-Swarm began as a fork of [FishCodeTech/muteki](https://github.com/FishCodeTech/muteki)
(GNU AGPL-3.0, preserved) and is now an independent product:

- the repositories share **no common commit history** (the fork rebuilt history);
- as of 2026-08-27, a mapped HEAD-to-HEAD tree comparison shows only ~68 byte-identical
  files across both trees while 263 tracked files exist exclusively here — the evidence
  graph alone grew from 584 to 5153 lines, and the solver engines diverged in both
  directions;
- attribution and the divergence statement live in [NOTICE](NOTICE).

Historical planning records mentioning the original codebase are archived under
`docs/archive/`. See also `references/btfly/` (read-only reference sources, AGPL-3.0).

## Governance & status

- Roadmap A P0–P6 shipped (v0.3.0-rc.1); kernel milestones M0–M9a implemented and
  verified. Progress tracks in [docs/10](docs/10-v4-kernel-improvement-implementation.md).
- Security posture: see [SECURITY.md](SECURITY.md) — run in dedicated disposable
  environments only; isolation of malicious challenges is an explicit non-goal.
- Development conventions and repo invariants: [AGENTS.md](AGENTS.md).
