/**
 * Reason-loop view model + fold logic (docs/07 Phase 2, data layer only).
 *
 * The kernel's reason scheduler narrates itself through `blackboard.delta`
 * events with `actor: "reason"` and a `kind`/`delta_type` discriminator
 * (recon → reason cycle → intent proposed/skipped/dispatched → executed →
 * cycle completed → loop finished). This module folds that event substream
 * into an immutable view the deck can render directly.
 *
 * `foldReasonEvent` is a pure function: events it does not care about return
 * the SAME loop object (reference-equal) so React memoization is undisturbed,
 * and every missing field falls back to a default — legacy sessions simply
 * never produce these kinds and fold to an empty loop.
 */

import { EventType, type DSwarmEvent } from "./events";

export type ReasonCycleStatus = "running" | "completed" | "skipped" | "failed";

export type ReasonIntentStatus =
  | "proposed"
  | "queued"
  | "claimed"
  | "running"
  | "awaiting_validation"
  | "completed"
  | "skipped"
  | "failed";

export interface ReasonIntentView {
  id: string;
  cycleId: string;
  goal: string;
  mode: string;
  priority?: number;
  status: ReasonIntentStatus;
  fromFactIds: string[];
  dedupeKey?: string;
  profile?: string;
  surfaceTarget?: string;
  taskKind?: string;
  hostScan?: string;
  workerId?: string;
  originalIntentId?: string;
  recoveryAttempt?: number;
  noFeedback?: boolean;
  realOutputWithoutFlag?: boolean;
  /** event ts of the dispatch decision that put this intent in flight. */
  dispatchedAt?: number;
  dispatchReason?: string;
  skipReason?: string;
  flag?: string;
}


export interface ReasonCycleView {
  id: string;
  generation: number;
  status: ReasonCycleStatus;
  trigger?: string;
  startedAt?: number;
  completedAt?: number;
  durationMs?: number;
  planner?: string;
  audits: string[];
  intents: ReasonIntentView[];
  goalMet?: boolean;
}

export interface ReconView {
  status: "running" | "completed";
  startedAt?: number;
  durationMs?: number;
  newFindings?: number;
  flag?: string;
}

export interface ReasonLoopView {
  cycles: ReasonCycleView[];
  recon?: ReconView;
  stopReason?: string;
  solved?: boolean;
  paused: boolean;
  healthState?: "healthy" | "degraded" | "retrying" | "circuit_open" | "reason_blocked" | "waiting_for_operator";
  healthFailures?: number;
  healthMaxFailures?: number;
  healthRequestFailures?: number;
  healthRequestLimit?: number;
  healthBlockedReason?: string;
}

export function emptyReasonLoop(): ReasonLoopView {
  return { cycles: [], paused: false };
}

const REASON_DELTA_KINDS = new Set([
  "recon_started", "recon_completed", "operator_paused",
  "reason_cycle_started", "intent_proposed", "intent_skipped", "dispatch_decision",
  "fallback_dispatch", "intent_completed", "intent_failed", "intent_interrupted",
  "intent_recovery_required", "intent_recovery_blocked", "intent_recovery_exhausted",
  "reason_health_changed", "reason_blocked", "reason_recovered", "provider_recovery_activated",
  "provider_recovery_blocked", "binding_effective_changed",
  "reason_cycle_completed", "reason_loop_finished",
]);

const str = (v: unknown): string | undefined =>
  typeof v === "string" && v ? v : undefined;

const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

/** from_facts entries arrive as fact seq numbers (or already-stringified ids);
 *  normalise to the `fact:<seq>` id shape the rest of the UI uses. */
function factIds(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v
    .map((x) =>
      typeof x === "number" && x > 0
        ? `fact:${x}`
        : typeof x === "string" && x
          ? x
          : "",
    )
    .filter((s) => s.length > 0);
}

function findCycle(loop: ReasonLoopView, id: string): ReasonCycleView | undefined {
  return loop.cycles.find((c) => c.id === id);
}

/** Resolve the cycle an event belongs to: explicit reason_cycle_id wins, then
 *  the most recent cycle; create a running cycle when none exists yet. */
function ensureCycle(
  loop: ReasonLoopView,
  p: Record<string, any>,
  ts: number,
): { loop: ReasonLoopView; cycle: ReasonCycleView } {
  const id = str(p.reason_cycle_id);
  let cycle = id ? findCycle(loop, id) : loop.cycles[loop.cycles.length - 1];
  if (cycle) return { loop, cycle };
  cycle = {
    id: id ?? "reason-?",
    generation: num(p.generation) ?? 0,
    status: "running",
    startedAt: ts,
    audits: [],
    intents: [],
  };
  return { loop: { ...loop, cycles: [...loop.cycles, cycle] }, cycle };
}

function replaceCycle(
  loop: ReasonLoopView,
  cycle: ReasonCycleView,
  next: ReasonCycleView,
): ReasonLoopView {
  return {
    ...loop,
    cycles: loop.cycles.map((c) => (c === cycle ? next : c)),
  };
}

function findIntent(
  loop: ReasonLoopView,
  id: string,
): { cycle: ReasonCycleView; intent: ReasonIntentView } | undefined {
  for (const cycle of loop.cycles) {
    const intent = cycle.intents.find((i) => i.id === id);
    if (intent) return { cycle, intent };
  }
  return undefined;
}

/** Upsert an intent inside its cycle, applying `patch`. Creates the intent
 *  (and its cycle, if needed) when the event references one we have not seen. */
function upsertIntent(
  loop: ReasonLoopView,
  p: Record<string, any>,
  ts: number,
  patch: (prev: ReasonIntentView | undefined) => Partial<ReasonIntentView>,
): ReasonLoopView {
  const id = str(p.intent_id) ?? "intent-?";
  const found = findIntent(loop, id);
  let cycle: ReasonCycleView;
  if (found) {
    cycle = found.cycle;
  } else {
    const ensured = ensureCycle(loop, p, ts);
    loop = ensured.loop;
    cycle = ensured.cycle;
  }
  const prev = found?.intent;
  const base: ReasonIntentView = prev ?? {
    id,
    cycleId: cycle.id,
    goal: str(p.goal) ?? "",
    mode: str(p.mode) ?? "",
    status: "proposed",
    fromFactIds: [],
  };
  const nextIntent: ReasonIntentView = { ...base, ...patch(prev) };
  const nextCycle: ReasonCycleView = {
    ...cycle,
    intents: prev
      ? cycle.intents.map((i) => (i === prev ? nextIntent : i))
      : [...cycle.intents, nextIntent],
  };
  return replaceCycle(loop, cycle, nextCycle);
}

/**
 * Fold one event into the reason-loop view. Only `blackboard.delta` events
 * from the reason actor with a known reason kind change the state; everything
 * else returns `loop` unchanged (same reference).
 */
export function foldReasonEvent(
  loop: ReasonLoopView,
  ev: DSwarmEvent,
): ReasonLoopView {
  if (ev.event_type !== EventType.BLACKBOARD_DELTA) return loop;
  const p = (ev.payload ?? {}) as Record<string, any>;
  if (p.actor !== "reason") return loop;
  const kind = str(p.kind) ?? str(p.delta_type);
  if (!kind || !REASON_DELTA_KINDS.has(kind)) return loop;

  switch (kind) {
    case "recon_started":
      return {
        ...loop,
        recon: { status: "running", startedAt: ev.ts },
      };
    case "recon_completed":
      return {
        ...loop,
        recon: {
          ...loop.recon,
          status: "completed",
          durationMs: num(p.duration_ms),
          newFindings: num(p.new_findings),
          flag: str(p.flag),
        },
      };
    case "operator_paused":
      return { ...loop, paused: true };
    case "reason_cycle_started": {
      const id = str(p.reason_cycle_id) ?? "reason-?";
      const existing = findCycle(loop, id);
      if (existing) {
        return replaceCycle(loop, existing, {
          ...existing,
          status: "running",
          generation: num(p.generation) ?? existing.generation,
          startedAt: existing.startedAt ?? ev.ts,
        });
      }
      return {
        ...loop,
        cycles: [
          ...loop.cycles,
          {
            id,
            generation: num(p.generation) ?? 0,
            status: "running",
            trigger: str(p.trigger),
            startedAt: ev.ts,
            audits: [],
            intents: [],
          },
        ],
      };
    }
    case "intent_proposed":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: prev?.status ?? "proposed",
        goal: str(p.goal) ?? prev?.goal ?? "",
        mode: str(p.mode) ?? prev?.mode ?? "",
        priority: num(p.priority) ?? prev?.priority,
        profile: str(p.profile) ?? prev?.profile,
        surfaceTarget: str(p.surface_target) ?? prev?.surfaceTarget,
        taskKind: str(p.task_kind) ?? prev?.taskKind,
        hostScan: str(p.host_scan) ?? prev?.hostScan,
        dedupeKey: str(p.dedupe_key) ?? prev?.dedupeKey,
        fromFactIds: factIds(p.from_facts).length ? factIds(p.from_facts) : (prev?.fromFactIds ?? []),
      }));
    case "dispatch_decision":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "running",
        dispatchedAt: prev?.dispatchedAt ?? ev.ts,
        dispatchReason: str(p.dispatch_reason) ?? prev?.dispatchReason,
        profile: str(p.profile) ?? prev?.profile,
        priority: num(p.priority) ?? prev?.priority,
        workerId: str(p.worker_id) ?? str(p.worker) ?? prev?.workerId,
      }));
    case "fallback_dispatch":
      return upsertIntent(loop, { ...p, intent_id: str(p.intent_id) ?? "fallback-bootstrap" }, ev.ts, (prev) => ({
        status: "running", goal: prev?.goal || "fallback bootstrap",
        dispatchedAt: prev?.dispatchedAt ?? ev.ts,
        dispatchReason: str(p.reason) ?? prev?.dispatchReason,
      }));
    case "intent_interrupted":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "awaiting_validation",
        workerId: str(p.worker) ?? prev?.workerId,
        noFeedback: p.no_feedback !== false,
        realOutputWithoutFlag: p.real_output_without_flag === true,
        recoveryAttempt: num(p.recovery_attempt) ?? prev?.recoveryAttempt,
        skipReason: str(p.result_detail) ?? prev?.skipReason,
      }));
    case "intent_recovery_required": {
      const recoveryId = str(p.recovery_intent_id);
      const originalId = str(p.intent_id);
      const id = recoveryId || originalId;
      if (!id) return loop;
      return upsertIntent(loop, { ...p, intent_id: id }, ev.ts, (prev) => ({
        status: recoveryId ? "queued" : "awaiting_validation",
        originalIntentId: recoveryId ? originalId : prev?.originalIntentId,
        recoveryAttempt: num(p.recovery_attempt) ?? prev?.recoveryAttempt,
        goal: str(p.goal) ?? prev?.goal ?? "",
        workerId: str(p.worker) ?? prev?.workerId,
        dispatchReason: recoveryId ? "awaiting validation recovery" : prev?.dispatchReason,
      }));
    }
    case "intent_recovery_blocked":
    case "intent_recovery_exhausted":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "failed",
        recoveryAttempt: num(p.recovery_attempt) ?? prev?.recoveryAttempt,
        skipReason: str(p.blocked_reason) ?? str(p.reason) ?? str(p.error_type) ?? prev?.skipReason,
      }));
    case "intent_skipped":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "skipped",
        dedupeKey: str(p.dedupe_key) ?? prev?.dedupeKey,
        skipReason: str(p.skip_reason) ?? prev?.skipReason,
      }));
    case "intent_completed":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "completed",
        workerId: str(p.worker) ?? str(p.worker_id) ?? prev?.workerId,
        flag: str(p.flag) ?? prev?.flag,
      }));
    case "intent_failed":
      return upsertIntent(loop, p, ev.ts, (prev) => ({
        status: "failed",
        workerId: str(p.worker) ?? str(p.worker_id) ?? prev?.workerId,
        skipReason: str(p.error) ?? str(p.reason) ?? prev?.skipReason,
      }));
    case "reason_health_changed": {
      const health = str(p.health_state) as ReasonLoopView["healthState"];
      if (!health) return loop;
      return {
        ...loop,
        healthState: health,
        healthFailures: num(p.failures) ?? loop.healthFailures,
        healthMaxFailures: num(p.max_failures) ?? loop.healthMaxFailures,
        healthRequestFailures: num(p.request_failures) ?? loop.healthRequestFailures,
        healthRequestLimit: num(p.request_limit) ?? loop.healthRequestLimit,
        healthBlockedReason: str(p.blocked_reason) ?? (health === "healthy" ? undefined : loop.healthBlockedReason),
      };
    }
    case "reason_blocked":
      return { ...loop, paused: true, healthState: "reason_blocked", healthBlockedReason: str(p.reason) ?? loop.healthBlockedReason, stopReason: str(p.reason) ?? loop.stopReason };
    case "reason_recovered":
      return { ...loop, paused: false, healthState: "healthy", healthFailures: 0, healthBlockedReason: undefined };
    case "reason_cycle_completed": {
      const ensured = ensureCycle(loop, p, ev.ts);
      loop = ensured.loop;
      const cycle = ensured.cycle;
      const notes: string[] = Array.isArray(p.audit_notes)
        ? p.audit_notes.filter((n: unknown) => typeof n === "string" && n)
        : str(p.audit_notes)
          ? [p.audit_notes as string]
          : [];
      return replaceCycle(loop, cycle, {
        ...cycle,
        status: "completed",
        completedAt: ev.ts,
        durationMs: num(p.duration_ms) ?? cycle.durationMs,
        planner: str(p.planner) ?? cycle.planner,
        goalMet: typeof p.goal_met === "boolean" ? p.goal_met : cycle.goalMet,
        audits: [...cycle.audits, ...notes],
      });
    }
    case "reason_loop_finished":
      return {
        ...loop,
        stopReason: str(p.stop_reason) ?? loop.stopReason,
        solved: typeof p.solved === "boolean" ? p.solved : loop.solved,
      };
    default:
      return loop;
  }
}

/** Traffic-light tone for the loop banner. */
export function reasonLoopTone(loop: ReasonLoopView): "ok" | "warn" | "bad" | "muted" {
  if (loop.solved) return "ok";
  if (loop.healthState === "circuit_open" || loop.healthState === "reason_blocked" || loop.healthState === "waiting_for_operator") return "bad";
  if (loop.healthState === "degraded" || loop.healthState === "retrying" || loop.paused) return "warn";
  if (loop.stopReason) return loop.stopReason === "operator_stop" ? "muted" : "warn";
  return "ok";
}

/** One line per cycle for the intents-tab strip: status + duration + trigger. */
export function reasonCycleRows(
  loop: ReasonLoopView,
): { id: string; generation: number; status: ReasonCycleStatus; durationMs?: number; trigger?: string; auditCount: number; intentCount: number }[] {
  return loop.cycles.map((cycle) => ({
    id: cycle.id,
    generation: cycle.generation,
    status: cycle.status,
    durationMs: cycle.durationMs ?? (
      cycle.startedAt != null
        ? Math.max(0, Math.round(((cycle.completedAt ?? Date.now() / 1000) - cycle.startedAt) * 1000))
        : undefined
    ),
    trigger: cycle.trigger,
    auditCount: cycle.audits.length,
    intentCount: cycle.intents.length,
  }));
}

export function formatDurationMs(ms?: number): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1).replace(/\.0$/, "")}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s - m * 60)}s`;
}
