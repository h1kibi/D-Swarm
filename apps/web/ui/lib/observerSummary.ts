import type { DeckState } from "./events";
import type { RunObservabilitySnapshot } from "./useRun";

/**
 * The sidebar Observer summary (docs/17 §12.3).
 *
 * It is a compact, read-only digest of state the deck and the observability
 * snapshot ALREADY carry — no Observer LLM call, no new backend surface. Fields
 * mirror the proposal exactly: current Reason state, active Intent/Worker,
 * latest real output, provider/runtime health, verified/candidate evidence with
 * the evidence gap, who decides next, and what needs the operator. Values are
 * never invented: a field with no real source is reported as unknown.
 */
export interface ObserverSignal {
  label: string;
  value: string;
  /** machine state carried alongside the operator-facing wording */
  state?: string;
  warn?: boolean;
}

export interface ObserverSummary {
  health: { status: string; warn: boolean };
  reason: ObserverSignal;
  activity: ObserverSignal[];
  lastOutput?: string;
  evidence: ObserverSignal[];
  nextDecider: ObserverSignal;
  operatorAttention: string[];
}

function short(text: string, limit = 160): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat;
}

/** The most recent real tool/terminal output we actually captured for any lane. */
function latestRealOutput(deck: DeckState): string | undefined {
  const candidates: Array<{ ts: number; text: string }> = [];
  for (const [solverId, lane] of Object.entries(deck.lanes ?? {})) {
    const stdout = (lane.runtime?.stdout || "").trim();
    if (!stdout) continue;
    const lines = stdout.split("\n").map((line) => line.trim()).filter(Boolean);
    if (!lines.length) continue;
    candidates.push({ ts: deck.finishedAt ?? deck.startedAt ?? 0, text: `${solverId}: ${lines[lines.length - 1]}` });
  }
  return candidates.length ? short(candidates[candidates.length - 1].text) : undefined;
}

export function projectObserverSummary(
  deck: DeckState,
  observability?: RunObservabilitySnapshot | null,
): ObserverSummary {
  const cycles = deck.reasonLoop?.cycles ?? [];
  const lastCycle = cycles[cycles.length - 1];
  const intents = deck.blackboard?.intents ?? [];
  const activeIntents = intents.filter((intent) => intent.status === "claimed"
    || intent.dispatchState === "awaiting_validation");
  const awaiting = intents.filter((intent) => intent.dispatchState === "awaiting_validation");
  const lanes = Object.entries(deck.lanes ?? {}).filter(([id]) => id !== "reason" && id !== "coordinator");
  const onlineLanes = lanes.filter(([, lane]) => lane.online !== false);

  const verified = (deck.blackboard?.facts ?? []).filter((fact) => fact.verified).length;
  const candidates = (deck.blackboard?.facts ?? []).filter((fact) => !fact.verified).length;
  const inherited = observability?.knowledge.inherited.length ?? 0;

  const reasonBlocked = deck.reasonBlocked || observability?.health.status === "blocked";
  const fallbackActive = deck.fallbackActive === true;
  const degraded = deck.degraded === true || (observability?.health.degraded.length ?? 0) > 0;

  const operatorAttention: string[] = [];
  const addAttention = (label: string) => {
    if (label && !operatorAttention.includes(label)) operatorAttention.push(label);
  };
  for (const issue of observability?.health.issues ?? []) {
    addAttention(short(String(issue.message || issue.kind || "health issue"), 140));
  }
  for (const row of observability?.health.degraded ?? []) {
    addAttention(short(`${row.role || row.engine || "role"} ${row.reason || "degraded"}`, 140));
  }
  // Track what each source contributed by SOURCE, not by matching substrings in the
  // rendered text — "Reason recovers next cycle" contains "Reason", so a substring
  // check would silently swallow the blocked-Reason line.
  if (reasonBlocked) addAttention(short(`Reason blocked — ${deck.reasonBlockedReason || "planner unavailable"}`, 140));
  if (fallbackActive) addAttention(short(`Provider fallback active — ${deck.fallbackReason || "settings default"}`, 140));
  if (awaiting.length) addAttention(`${awaiting.length} intent(s) awaiting_validation — Reason recovers next cycle`);

  const nextDecider: ObserverSignal = awaiting.length
    ? { label: "Next decision", value: "Reason — recover parked intents first", state: "awaiting_validation" }
    : reasonBlocked
      ? { label: "Next decision", value: "Operator — Reason is blocked", state: "reason_blocked", warn: true }
      : deck.finished
        ? { label: "Next decision", value: "None — run is terminal", state: deck.solved ? "solved" : "finished" }
        : { label: "Next decision", value: "Reason — next planning cycle", state: "reason" };

  return {
    health: {
      status: reasonBlocked ? "blocked" : fallbackActive ? "fallback_active" : degraded ? "degraded" : (observability?.health.status || (deck.finished ? "finished" : "active")),
      warn: reasonBlocked || degraded || fallbackActive,
    },
    reason: {
      label: "Reason",
      value: lastCycle
        ? `${lastCycle.id} · ${lastCycle.status}${lastCycle.durationMs != null ? ` · ${Math.round(lastCycle.durationMs / 1000)}s` : ""}`
        : "no cycle recorded",
      state: lastCycle?.status,
      warn: reasonBlocked,
    },
    activity: [
      { label: "Active intents", value: `${activeIntents.length}`, state: "claimed" },
      { label: "Workers", value: `${onlineLanes.length}/${lanes.length} online` },
      ...(awaiting.length
        ? [{ label: "Awaiting validation", value: `${awaiting.length}`, state: "awaiting_validation", warn: true }]
        : []),
    ],
    lastOutput: latestRealOutput(deck),
    evidence: [
      { label: "Verified facts", value: `${verified}` },
      { label: "Candidates", value: `${candidates}`, warn: candidates > 0 },
      { label: "Inherited (read-only)", value: `${inherited}` },
    ],
    nextDecider,
    operatorAttention,
  };
}
