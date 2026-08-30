/**
 * Decision Timeline assembly + stage derivation (docs/07 §5.3/§5.4, Phase 4).
 *
 * The center of the Command-center deck is NOT the conversation anymore — it is
 * a time-ordered decision feed assembled here from the already-folded DeckState:
 *
 *   recon → reason cycles (audits + intents) → dispatches → facts → flags
 *   plus HITL requests, operator directives (with their lifecycle), worker
 *   output SUMMARIES (collapsed — raw tool output stays in the worker panels),
 *   chat messages as one event class among many, and generic historical-activity
 *   markers for retired paths (retired execution).
 *
 * Everything in this module is a pure function over DeckState so the ordering,
 * cycle-card data and directive lifecycle migrations are unit-testable without
 * rendering. Legacy sessions simply have no reasonLoop data and degrade to a
 * chat + historical-activity stream (§7.3) — no reducer changes required.
 */

import {
  type BlackboardFact,
  type ChatMessage,
  type DeckState,
  type DirectiveStatus,
  type HitlRequest,
  type OperatorDirective,
  type SolverLane,
} from "./events";
import type { ReasonCycleView, ReasonIntentView, ReconView } from "./reason";
import { historicalBlackboardActivity, STAGES, type Stage } from "./normalize";

/** Event ts values arrive as seconds OR milliseconds; normalise to ms. */
export function tsMs(ts: number | undefined): number {
  if (!ts) return 0;
  return ts < 1e12 ? ts * 1000 : ts;
}

/** P1-4: operator directive lifecycle — Queued → Consumed → Applied → closed.
 *  Mapped from the kernel's DirectiveStatus so no raw enum reaches the UI. */
export type DirectiveLifecycle = "queued" | "consumed" | "applied" | "closed";

export function directiveLifecycle(status: DirectiveStatus): DirectiveLifecycle {
  switch (status) {
    case "received":
    case "queued":
      return "queued";
    case "bound":
      return "consumed";
    case "acted":
      return "applied";
    default: // superseded | expired | rejected
      return "closed";
  }
}

export type TimelineItem =
  | { kind: "recon"; id: string; ts: number; stage: Stage; recon: ReconView }
  | { kind: "cycle"; id: string; ts: number; stage: Stage; cycle: ReasonCycleView }
  | { kind: "dispatch"; id: string; ts: number; stage: Stage; intent: ReasonIntentView }
  | { kind: "fact"; id: string; ts: number; stage: Stage; fact: BlackboardFact }
  | { kind: "flag"; id: string; ts: number; stage: Stage; flag: string; actor: string }
  | { kind: "hitl"; id: string; ts: number; stage: Stage; req: HitlRequest }
  | { kind: "directive"; id: string; ts: number; stage: Stage; directive: OperatorDirective; lifecycle: DirectiveLifecycle }
  | { kind: "chat"; id: string; ts: number; stage: Stage; message: ChatMessage }
  | { kind: "legacy"; id: string; ts: number; stage: Stage; i18nKey: string; label: string }
  | { kind: "worker"; id: string; ts: number; stage: Stage; lane: SolverLane };

export type TimelineItemKind = TimelineItem["kind"];

/**
 * Chat messages that belong in the Decision Timeline (§5.4: chat is ONE event
 * class, not the spine). Human prompts and system lifecycle lines always show;
 * worker reasoning/tool firehose stays OUT (it is raw output — collapsed
 * summaries link to the worker panels instead). Agent messages only appear when
 * they are operator-facing main-thread answers (writeup / standby follow-ups).
 */
export function isTimelineChat(m: ChatMessage): boolean {
  if (m.role === "human" || m.role === "system") return true;
  return !!m.mainThread;
}

/** Same-timestamp ordering: stage/decision events always outrank chatter. */
const KIND_ORDER: Record<TimelineItemKind, number> = {
  recon: 0,
  cycle: 1,
  dispatch: 2,
  fact: 3,
  flag: 4,
  hitl: 5,
  directive: 6,
  worker: 7,
  legacy: 8,
  chat: 9,
};

/**
 * Assemble the unified time-ordered Decision Timeline from DeckState.
 * Output ordering: timestamp asc; same-ts ties break by decision priority
 * (KIND_ORDER) then by stable id, so live event folding never reshuffles rows
 * the operator is already reading.
 */
export function buildTimeline(deck: DeckState): TimelineItem[] {
  const items: TimelineItem[] = [];
  const loop = deck.reasonLoop;

  if (loop.recon) {
    items.push({
      kind: "recon",
      id: "recon",
      ts: tsMs(loop.recon.startedAt) || tsMs(deck.startedAt),
      stage: "recon",
      recon: loop.recon,
    });
  }

  for (const cycle of loop.cycles) {
    items.push({
      kind: "cycle",
      id: `cycle:${cycle.id}`,
      ts: tsMs(cycle.startedAt),
      stage: "reason",
      cycle,
    });
    for (const intent of cycle.intents) {
      if (intent.workerId || intent.status === "running" || intent.status === "completed" || intent.status === "failed") {
        items.push({
          kind: "dispatch",
          id: `dispatch:${intent.id}`,
          ts: tsMs(intent.dispatchedAt) || tsMs(cycle.startedAt),
          stage: "dispatch",
          intent,
        });
      }
    }
  }

  // Verified/candidate facts with provenance (actor / source event seq).
  for (const f of deck.blackboard.facts) {
    if (f.state === "rejected" || f.state === "merged" || f.state === "superseded") continue;
    items.push({
      kind: "fact",
      id: `fact:${f.factSeq ?? f.ts}`,
      ts: tsMs(f.ts),
      stage: "execute",
      fact: f,
    });
  }

  // Flag captures carry their own blackboard timeline entries (kind=flag_found).
  for (const e of deck.blackboard.events) {
    if (e.kind === "flag_found") {
      items.push({ kind: "flag", id: `flag:${e.id}`, ts: tsMs(e.ts), stage: "review", flag: e.label, actor: e.actor });
      continue;
    }
    // Retired-path deltas (retired execution) → generic legacy markers.
    const legacy = historicalBlackboardActivity(e.kind);
    if (legacy) {
      items.push({ kind: "legacy", id: `legacy:${e.id}`, ts: tsMs(e.ts), stage: "legacy", i18nKey: legacy.i18nKey, label: e.label });
    }
  }

  for (const req of deck.hitlRequests) {
    items.push({ kind: "hitl", id: `hitl:${req.id}`, ts: tsMs(req.ts), stage: "execute", req });
  }

  for (const d of deck.operatorDirectives) {
    items.push({
      kind: "directive",
      id: `directive:${d.id}`,
      ts: tsMs(d.ts),
      stage: "reason",
      directive: d,
      lifecycle: directiveLifecycle(d.status),
    });
  }

  // Collapsed worker-output summaries — one per lane; the raw tool output
  // itself never enters the Timeline body (§5.4 / P1-3).
  for (const lane of Object.values(deck.lanes)) {
    if (lane.role === "review") continue;
    const activity = lane.toolLines.length + (lane.reasoning ? 1 : 0);
    if (!activity && !lane.status) continue;
    items.push({
      kind: "worker",
      id: `worker:${lane.solverId}`,
      ts: tsMs(lane.runtime?.started_at) || tsMs(deck.startedAt),
      stage: "execute",
      lane,
    });
  }

  for (const m of deck.chat) {
    if (!isTimelineChat(m)) continue;
    items.push({ kind: "chat", id: `chat:${m.id}`, ts: tsMs(m.ts), stage: "execute", message: m });
  }

  return items.sort((a, b) =>
    a.ts - b.ts || KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}

/** DOM anchor id a Stage Rail click scrolls to (§5.3: click stage → jump). */
export function stageAnchorId(stage: Stage): string {
  return `tl-stage-${stage}`;
}

/**
 * First timeline item per stage, in stream order — the Stage Rail jump targets.
 * Items carry their own stage; the first occurrence of each becomes its anchor.
 */
export function stageAnchors(items: TimelineItem[]): Partial<Record<Stage, string>> {
  const out: Partial<Record<Stage, string>> = {};
  for (const item of items) {
    if (item.stage === "legacy") continue;
    if (!(item.stage in out)) out[item.stage] = item.id;
  }
  return out;
}

/** Run-level status, orthogonal to stage (docs/07 §P0-3):
 *  stage answers WHERE the run is, status answers HOW it is going. */
export type RunStatus =
  | "active" | "waiting" | "paused" | "degraded" | "failed" | "solved" | "completed";

/** Two-dim run state for the Stage Rail + top bar (docs/07 §P0-3). */
export interface StageInfo {
  stage: Stage;
  /** true when the stage was INFERRED (legacy session, no explicit kernel
   *  stage events) — the UI must render it as approximate. */
  derived: boolean;
  /** paused / waiting on operator / degraded — renders yellow on the rail. */
  waiting: boolean;
  failed: boolean;
  /** structured status dimension; derivable from the flags + solved/finished. */
  status: RunStatus;
}

/** Collapse the orthogonal flags into the single display status
 *  (precedence: failure > pause > operator wait > degradation > outcome). */
export function runStatusOf(deck: DeckState, waiting: boolean, failed: boolean): RunStatus {
  if (failed) return "failed";
  if (deck.reasonLoop.paused) return "paused";
  if (waiting) return "waiting";
  if (deck.runtimeDegraded.length > 0 && !deck.finished) return "degraded";
  if (deck.solved) return "solved";
  if (deck.finished) return "completed";
  return "active";
}

/**
 * Current stage of a run. The reason loop (new-kernel explicit events) wins;
 * legacy sessions with no reasonLoop data fall back to a coarse approximation
 * derived from the digest-level run phase, flagged `derived` (§5.3 fallback).
 */
export function deriveStage(deck: DeckState): StageInfo {
  const loop = deck.reasonLoop;
  const failed = deck.outcomeReason === "runtime_failure";
  const waiting = loop.paused || !!deck.awaitingOperator;
  const hasLoop = !!loop.recon || loop.cycles.length > 0;

  if (hasLoop) {
    let stage: Stage;
    if (loop.stopReason || loop.solved) {
      stage = "finalize";
    } else if (loop.recon?.status === "running") {
      stage = "recon";
    } else {
      const last = loop.cycles[loop.cycles.length - 1];
      const anyRunning = loop.cycles.some((c) =>
        c.intents.some((i) => i.status === "running" || i.status === "claimed"));
      const anyQueued = loop.cycles.some((c) =>
        c.intents.some((i) => i.status === "proposed" || i.status === "queued"));
      if (last?.status === "running") stage = "reason";
      else if (anyRunning) stage = "execute";
      else if (anyQueued) stage = "dispatch";
      else if (loop.cycles.length > 0) stage = "reason"; // between cycles
      else stage = "prepare";
    }
    return { stage, derived: false, waiting, failed, status: runStatusOf(deck, waiting, failed) };
  }

  // Legacy fallback — approximate, from the run's coarse lifecycle only.
  if (!deck.started) return { stage: "queued", derived: true, waiting, failed, status: runStatusOf(deck, waiting, failed) };
  if (deck.finished || deck.solved) return { stage: "finalize", derived: true, waiting, failed, status: runStatusOf(deck, waiting, failed) };
  return { stage: "execute", derived: true, waiting, failed, status: runStatusOf(deck, waiting, failed) };
}

/** Per-stage visual state for the Stage Rail (§5.3 conventions). */
export type StageNodeState = "completed" | "active" | "pending";

export function stageRailStates(info: StageInfo): { stage: Stage; state: StageNodeState }[] {
  const cur = STAGES.indexOf(info.stage);
  return STAGES.map((stage, i) => ({
    stage,
    state: i < cur ? "completed" : i === cur ? "active" : "pending",
  }));
}
