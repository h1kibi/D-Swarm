/**
 * Legacy + new event normalizer (docs/07 Phase 1).
 *
 * The UI never reads raw SSE payloads directly anymore: every event — live or
 * replayed from an old session JSONL — passes through here first. The raw
 * event is NEVER mutated (it stays available for the Raw Event expander);
 * normalization only *annotates* it with:
 *
 *  - a two-dim stage hint (docs/07 §P0-3). New kernels emit `payload.stage`
 *    explicitly; for legacy sessions the stage is DERIVED from event order
 *    (`stageDerived: true`) and must be rendered as approximate. Derived
 *    state is never written back to the event log.
 *  - a legacyActivity marker for retired paths (race / old coordinator /
 *    pre-pi worker engines). These render as generic "legacy execution
 *    activity" — the old run modes never reappear in nav, settings, or the
 *    formal status enums.
 */

import { EventType, type DSwarmEvent } from "./events";

export type Stage =
  | "queued"
  | "prepare"
  | "recon"
  | "reason"
  | "dispatch"
  | "execute"
  | "review"
  | "finalize"
  | "legacy";

export const STAGES: readonly Stage[] = [
  "queued",
  "prepare",
  "recon",
  "reason",
  "dispatch",
  "execute",
  "review",
  "finalize",
];

export interface LegacyActivity {
  kind: "race" | "coordinator" | "engine";
  /** i18n key — never render raw kernel vocabulary directly. */
  i18nKey: string;
  detail?: string;
}

export interface NormalizedEvent {
  raw: DSwarmEvent;
  stage?: Stage;
  stageDerived: boolean;
  legacyActivity?: LegacyActivity;
}

const STAGE_SET = new Set<string>(STAGES);

/** Retired-path blackboard kinds → generic legacy activity. */
const LEGACY_BB_KINDS: Record<string, LegacyActivity> = {
  race_started: { kind: "race", i18nKey: "legacy.raceStarted" },
  race_concluded: { kind: "race", i18nKey: "legacy.raceConcluded" },
  race_scout_spawned: { kind: "race", i18nKey: "legacy.raceStarted" },
};

/** Legacy-activity lookup by blackboard delta kind — lets the Decision
 *  Timeline render retired-path markers from the folded blackboard event log
 *  without re-normalizing the raw stream (docs/07 §7.3). */
export function legacyBlackboardActivity(kind: string): LegacyActivity | undefined {
  return LEGACY_BB_KINDS[kind];
}

/** Pre-pi worker engines → generic worker identity (docs/07 §7.3). */
const LEGACY_ENGINES = new Set(["claude", "codex", "cursor"]);

function bbStageHint(kind: string): Stage | undefined {
  if (kind === "intent_proposed") return "reason";
  if (kind === "intent_claimed") return "dispatch";
  if (kind === "intent_concluded") return "execute";
  if (kind.startsWith("review")) return "review";
  return undefined;
}

/**
 * Normalize one raw event. `prevStage` is the stage of the previous event in
 * the stream — legacy derivation is order-sensitive, so callers replaying a
 * session thread the returned stage forward.
 */
export function normalizeEvent(
  raw: DSwarmEvent,
  prevStage?: Stage,
): NormalizedEvent {
  const p = (raw.payload ?? {}) as Record<string, unknown>;

  // 1. explicit stage from a new kernel wins; validate against the enum so a
  //    raw kernel value never reaches the UI unchecked.
  const explicit = typeof p.stage === "string" && STAGE_SET.has(p.stage)
    ? (p.stage as Stage)
    : undefined;

  let legacyActivity: LegacyActivity | undefined;
  let derived: Stage | undefined;

  // 2. retired-path detection (payload-level; event names are unchanged).
  if (raw.event_type === EventType.BLACKBOARD_DELTA) {
    const kind = typeof p.kind === "string" ? p.kind : "";
    legacyActivity = LEGACY_BB_KINDS[kind];
    derived = bbStageHint(kind);
    if (legacyActivity) derived = "legacy";
  } else if (raw.event_type === EventType.GUIDANCE_INJECTED) {
    legacyActivity = { kind: "coordinator", i18nKey: "legacy.coordinatorPlan" };
    derived = "legacy";
  } else if (
    raw.event_type === EventType.WORKER_STATUS ||
    raw.event_type === EventType.WORKER_LIFECYCLE
  ) {
    const engine = typeof p.engine === "string" ? p.engine.toLowerCase() : "";
    if (LEGACY_ENGINES.has(engine)) {
      legacyActivity = {
        kind: "engine",
        i18nKey: "legacy.engine",
        detail: engine,
      };
    }
    derived = "execute";
  }

  // 3. coarse order-based derivation for legacy sessions.
  if (!explicit && !derived) {
    switch (raw.event_type) {
      case EventType.RUN_QUEUED:
        derived = "queued";
        break;
      case EventType.RUN_STARTED:
      case EventType.RUN_DISPATCHED:
        derived = "prepare";
        break;
      case EventType.TOOL_CALL_START:
      case EventType.TOOL_CALL_RESULT:
      case EventType.REASONING_DELTA:
      case EventType.TERMINAL_OUTPUT:
        derived = "execute";
        break;
      case EventType.RUN_FINISHED:
      case EventType.RUN_CANCELLED:
        derived = "finalize";
        break;
      default:
        derived = undefined; // keep prevStage
    }
  }

  const stage = explicit ?? derived ?? prevStage;
  return {
    raw,
    stage,
    stageDerived: !explicit && stage !== undefined,
    legacyActivity,
  };
}

/** Normalize a whole stream (e.g. a replayed session), threading the stage. */
export function normalizeStream(rawEvents: DSwarmEvent[]): NormalizedEvent[] {
  const out: NormalizedEvent[] = [];
  let stage: Stage | undefined;
  for (const raw of rawEvents) {
    const n = normalizeEvent(raw, stage);
    stage = n.stage;
    out.push(n);
  }
  return out;
}
