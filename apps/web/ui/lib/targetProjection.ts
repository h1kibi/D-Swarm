import type { RunSummary } from "./useRun";
import type { TargetRecord } from "./useTargets";

export type TargetStatus = "active" | "waiting" | "reason_blocked" | "degraded" | "fallback_active" | "solved" | "failed" | "archived" | "idle";
export interface TargetProjection {
  id: string;
  name: string;
  kind?: string;
  category: string;
  status: TargetStatus;
  runs: RunSummary[];
  activeCount: number;
  verifiedFactCount: number;
  flagCount: number;
  updated: number;
  needsAttention: boolean;
  primaryDirection: string;
  primaryDirectionId?: string | null;
  description?: string;
  targetAddress?: string | null;
  flagFormatHint?: string;
  expectedFlags?: number;
  multiFlag?: boolean;
  hints?: Array<Record<string, unknown>>;
  directions?: Array<Record<string, unknown>>;
  verifiedFacts?: Array<Record<string, unknown>>;
  auditRecords?: Array<Record<string, unknown>>;
  activity?: Array<Record<string, unknown>>;
}

function targetKey(run: RunSummary): string {
  // target_id is the durable identity. The category/name key is only a
  // compatibility projection for pre-target runs and must never be persisted.
  return run.target_id?.trim() || `legacy:${run.category || "misc"}:${(run.name || run.run_id).trim().toLowerCase()}`;
}
function directionFor(run: RunSummary | undefined): string {
  if (!run) return "General";
  if (run.direction?.trim()) return run.direction.trim();
  const split = (run.name || "").trim().split(/\s+[|/:·-]\s+/);
  return split.length > 1 ? split[0] : run.category || "General";
}
function statusFor(runs: RunSummary[]): TargetStatus {
  if (runs.some((run) => run.solved)) return "solved";
  if (runs.some((run) => run.awaiting_help || run.status === "failed")) return "failed";
  if (runs.some((run) => run.status === "reason_blocked" || run.reason_blocked)) return "reason_blocked";
  if (runs.some((run) => run.status === "fallback_active" || run.fallback_active)) return "fallback_active";
  if (runs.some((run) => run.status === "degraded" || run.degraded)) return "degraded";
  if (runs.some((run) => run.status === "running")) return "active";
  if (runs.some((run) => run.status === "queued" || run.status === "paused")) return "waiting";
  if (runs.length > 0 && runs.every((run) => run.archived)) return "archived";
  return "idle";
}
export function projectTargets(runs: RunSummary[], persisted: TargetRecord[] = []): TargetProjection[] {
  const groups = new Map<string, RunSummary[]>();
  const byId = new Map<string, TargetRecord>();
  for (const target of persisted) byId.set(target.id, target);
  for (const run of runs) {
    const key = targetKey(run);
    groups.set(key, [...(groups.get(key) || []), run]);
  }
  // Durable Target cards may exist before their first Run. Keep them visible;
  // when a run is present, the explicit target_id is the only join key.
  for (const target of persisted) {
    if (!groups.has(target.id)) groups.set(target.id, []);
  }
  return [...groups.entries()].map(([id, grouped]) => {
    const record = byId.get(id);
    const sorted = [...grouped].sort((a, b) => (b.updated_at ?? b.updated ?? 0) - (a.updated_at ?? a.updated ?? 0));
    const targetRuns = record?.runs && Array.isArray(record.runs)
      ? record.runs as RunSummary[]
      : sorted;
    const effectiveRuns = targetRuns.length > sorted.length ? targetRuns : sorted;
    const primary = record?.directions?.find((direction) => direction.id === record.primary_direction_id)
      || record?.directions?.[0];
    const fallbackRun = sorted[0];
    return {
      id,
      name: record?.name || fallbackRun?.target_name || fallbackRun?.name || fallbackRun?.run_id || "Untitled target",
      kind: record?.kind || "ctf",
      category: record?.category || fallbackRun?.target_category || fallbackRun?.category || "misc",
      status: record?.status === "archived" ? "archived" : statusFor(effectiveRuns),
      runs: effectiveRuns,
      activeCount: record?.active_run_count ?? effectiveRuns.filter((run) => run.status === "running" || run.status === "queued" || run.status === "reason_blocked" || run.status === "degraded" || run.status === "fallback_active").length,
      verifiedFactCount: record?.verified_fact_count ?? record?.verified_facts?.length ?? 0,
      flagCount: record?.accepted_flag_count ?? effectiveRuns.reduce((total, run) => total + (run.flag_count ?? run.flags?.length ?? (run.flag ? 1 : 0)), 0),
      updated: record?.last_activity_at ?? record?.updated_at ?? sorted[0]?.updated_at ?? sorted[0]?.updated ?? 0,
      needsAttention: effectiveRuns.some((run) => !!run.awaiting_help || run.status === "failed" || run.status === "reason_blocked" || run.status === "degraded" || run.status === "fallback_active"),
      primaryDirection: String(primary?.label || directionFor(sorted[0])),
      primaryDirectionId: record?.primary_direction_id ?? (typeof primary?.id === "string" ? primary.id : null),
      description: record?.description,
      targetAddress: record?.target_address,
      flagFormatHint: record?.flag_format_hint,
      expectedFlags: record?.expected_flags ?? 1,
      hints: record?.hints,
      directions: record?.directions,
      verifiedFacts: record?.verified_facts,
      auditRecords: record?.audit_records,
      activity: record?.activity,
      archivedAt: record?.archived_at,
    };
  }).sort((a, b) => Number(b.needsAttention) - Number(a.needsAttention) || b.updated - a.updated);
}

export interface TargetDirectionGroup {
  id: string;
  label: string;
  runs: RunSummary[];
}

/**
 * Build the visible Target -> Direction -> Run tree. Persisted directions are
 * rendered even before they have a Run; legacy/unassigned Runs are retained in
 * a deterministic fallback branch instead of disappearing from the workspace.
 */
export function groupTargetRunsByDirection(target: TargetProjection | undefined, runs: RunSummary[], zh = false): TargetDirectionGroup[] {
  const groups = new Map<string, TargetDirectionGroup>();
  for (const direction of target?.directions ?? []) {
    const id = String(direction.id || "");
    const label = String(direction.label || direction.name || "Direction").trim();
    if (id && label) groups.set(id, { id, label, runs: [] });
  }
  const fallbackLabel = (run: RunSummary): string => {
    if (run.direction?.trim()) return run.direction.trim();
    const split = (run.name || "").trim().split(/\s+[|/:·-]\s+/);
    return split.length > 1 ? split[0] : run.category || (zh ? "通用探索" : "General exploration");
  };
  for (const run of runs) {
    const explicit = String(run.direction_id || "");
    const matching = explicit ? groups.get(explicit) : undefined;
    if (matching) {
      matching.runs.push(run);
      continue;
    }
    const label = fallbackLabel(run);
    const byLabel = [...groups.values()].find((group) => group.label === label);
    if (byLabel) {
      byLabel.runs.push(run);
      continue;
    }
    const key = `unassigned:${label}`;
    const fallback = groups.get(key) ?? { id: key, label, runs: [] };
    fallback.runs.push(run);
    groups.set(key, fallback);
  }
  return [...groups.values()].sort((a, b) => {
    const aPrimary = a.id === target?.primaryDirectionId ? 0 : 1;
    const bPrimary = b.id === target?.primaryDirectionId ? 0 : 1;
    return aPrimary - bPrimary || a.label.localeCompare(b.label);
  });
}

export function targetForRun(targets: TargetProjection[], runId: string): TargetProjection | undefined {
  return targets.find((target) => target.runs.some((run) => run.run_id === runId));
}

/**
 * Pick the initial durable Target for the command deck without overriding an
 * explicit Run deep-link. This is intentionally a pure projection helper so
 * boot selection remains deterministic and easy to test.
 */
export function initialTargetId(targets: TargetProjection[], runId: string): string | null {
  if (runId && !runId.startsWith("draft-")) return targetForRun(targets, runId)?.id ?? null;
  // Boot landing must open a Target the operator actually works with: prefer an
  // attention/active target, then any target that owns runs, then the newest card.
  // Empty smoke/placeholder cards must never become the boot screen.
  const landing = targets.find((target) => target.needsAttention || target.activeCount > 0)
    ?? targets.find((target) => target.runs.length > 0)
    ?? targets[0];
  return landing?.id ?? null;
}


