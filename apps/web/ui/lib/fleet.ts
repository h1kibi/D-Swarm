/**
 * Run Fleet view model (docs/07 §5.2, Phase 4) — pure functions over the
 * backend's run summaries, unit-tested without rendering.
 *
 * The left rail is upgraded from a plain conversation list to a high-density
 * fleet: attention/activity filters, a compact row mode, and batch selection
 * for pause/resume/stop. All filtering/sorting/selection logic lives here so
 * the component stays a thin renderer and the "Needs Attention" rule has a
 * single definition.
 *
 * NOTE: the summary endpoint carries no per-run cost/worker-count/stage (those
 * live in each run's event stream; replaying 100 streams for a list would be
 * absurd), so fleet rows show what the summary has — lifecycle status, flag
 * progress, queue position, HITL-pending, updated time. Cost/workers/stage are
 * shown for the SELECTED run in the top bar, derived live from its DeckState.
 */

import type { RunSummary } from "./useRun";

export type FleetFilter =
  | "all"
  | "active"
  | "attention"
  | "queued"
  | "paused"
  | "solved"
  | "failed"
  | "archived";

export type FleetSort = "attention" | "newest" | "oldest" | "name";

export const FLEET_FILTERS: readonly FleetFilter[] = [
  "all",
  "active",
  "attention",
  "queued",
  "paused",
  "solved",
  "failed",
  "archived",
];

export type BatchAction = "pause" | "resume" | "stop" | "archive" | "unarchive";

/**
 * Needs Attention (§5.2): an unfinished run with a pending HITL hand-raise
 * (awaiting_help) or a runtime failure. The summary payload carries no
 * per-run health flag, so HITL-pending is the live signal here.
 */
export function runNeedsAttention(r: RunSummary): boolean {
  return (!!r.awaiting_help && !r.finished) || r.status === "failed";
}

/** A run the fleet considers "active" (in flight, not terminal, not paused —
 *  paused runs have their own filter bucket per §5.2). */
export function isFleetActive(r: RunSummary): boolean {
  return r.started && !r.finished && !r.archived && r.status !== "cancelled" && r.status !== "paused";
}

export function filterFleet(runs: RunSummary[], filter: FleetFilter): RunSummary[] {
  switch (filter) {
    case "all":
      return runs.filter((r) => !r.archived);
    case "active":
      return runs.filter((r) => isFleetActive(r) && !r.queued);
    case "attention":
      return runs.filter(runNeedsAttention);
    case "queued":
      return runs.filter((r) => !r.archived && (r.status === "queued" || !!r.queued));
    case "paused":
      return runs.filter((r) => !r.archived && r.status === "paused");
    case "solved":
      return runs.filter((r) => !r.archived && r.solved);
    case "failed":
      return runs.filter((r) => !r.archived && (r.status === "failed" || r.status === "cancelled"));
    case "archived":
      return runs.filter((r) => r.archived);
  }
}

/** Per-filter counts for the filter chip bar. */
export function fleetCounts(runs: RunSummary[]): Record<FleetFilter, number> {
  const out = {} as Record<FleetFilter, number>;
  for (const f of FLEET_FILTERS) out[f] = filterFleet(runs, f).length;
  return out;
}

/**
 * Fleet ordering (§5.2). "attention" floats needs-attention runs first, then
 * running, then newest-updated. The rail's own pinned/folder sections still
 * group first — this orders WITHIN a section. Never mutates the input.
 */
export function sortFleet(runs: RunSummary[], sort: FleetSort): RunSummary[] {
  const byUpdated = (a: RunSummary, b: RunSummary) => (b.updated_at ?? 0) - (a.updated_at ?? 0);
  const rows = [...runs];
  switch (sort) {
    case "attention":
      return rows.sort((a, b) =>
        Number(runNeedsAttention(b)) - Number(runNeedsAttention(a)) ||
        Number(isFleetActive(b)) - Number(isFleetActive(a)) ||
        byUpdated(a, b));
    case "newest":
      return rows.sort(byUpdated);
    case "oldest":
      return rows.sort((a, b) => (a.updated_at ?? 0) - (b.updated_at ?? 0));
    case "name":
      return rows.sort((a, b) => (a.name || a.run_id).localeCompare(b.name || b.run_id));
  }
}

/** Flag progress label data for a row: "1/3" — undefined when not started.
 *  Flag VALUES are redacted from unauthenticated list responses (containment,
 *  run-6427) — the count travels in flag_count when that's active. */
export function flagProgress(r: RunSummary): { got: number; need: number } | null {
  if (!r.started) return null;
  const got = r.flag_count ?? r.flags?.length ?? (r.flag ? 1 : 0);
  return { got, need: Math.max(1, r.expected_flags ?? 1) };
}

/** Toggle one id in a batch selection (immutable — the component keeps a Set). */
export function toggleSelection(selected: ReadonlySet<string>, runId: string): Set<string> {
  const next = new Set(selected);
  if (next.has(runId)) next.delete(runId);
  else next.add(runId);
  return next;
}

/**
 * The runs a batch action actually applies to (§5.2): pause/resume only make
 * sense on live runs, stop on anything not terminal, archive/unarchive on the
 * selection's not-yet / already-archived halves. Selection order is kept
 * stable (input order) so the fan-out is deterministic.
 */
export function batchTargets(
  runs: RunSummary[],
  selected: ReadonlySet<string>,
  action: BatchAction,
): string[] {
  const chosen = runs.filter((r) => selected.has(r.run_id));
  switch (action) {
    case "pause":
      return chosen.filter((r) => r.status === "running" || r.status === "queued").map((r) => r.run_id);
    case "resume":
      return chosen.filter((r) => r.status === "paused").map((r) => r.run_id);
    case "stop":
      return chosen.filter((r) => !r.finished && r.status !== "cancelled" && r.started).map((r) => r.run_id);
    case "archive":
      return chosen.filter((r) => !r.archived).map((r) => r.run_id);
    case "unarchive":
      return chosen.filter((r) => r.archived).map((r) => r.run_id);
  }
}
