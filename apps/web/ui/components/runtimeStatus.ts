export type RuntimePoolState =
  | "idle" | "starting" | "probing" | "ready" | "degraded"
  | "recovering" | "stopping" | "stopped" | "unknown" | string;

export interface RuntimePoolFailure {
  category: string;
  code: string;
}

export interface RuntimePoolHistoryRow {
  state: RuntimePoolState;
  reason_code?: string;
  recovery_episode?: number;
  updated_at?: number;
  kind?: string;
  generation?: number;
  failure?: RuntimePoolFailure | null;
}

export interface RuntimePoolStatus {
  pool_id: string;
  state: RuntimePoolState;
  generation: number;
  pool_instance_id: string;
  active_workers: number;
  waiting_workers: number;
  capacity: number;
  failure: RuntimePoolFailure | null;
  recovery_episode: number;
  history?: RuntimePoolHistoryRow[];
}

export interface RuntimePoolsSnapshot {
  run_id: string;
  policy_mode: string;
  pools: RuntimePoolStatus[];
}

function finiteInt(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.round(value)) : fallback;
}

/** Traffic-light tone for a pool lifecycle state (failure paints it bad). */
export function poolTone(pool: RuntimePoolStatus): "ok" | "warn" | "bad" | "muted" {
  if (pool.failure) return "bad";
  switch (pool.state) {
    case "ready":
      return "ok";
    case "degraded":
    case "recovering":
    case "probing":
      return "warn";
    case "starting":
    case "new":
      return "muted";
    default:
      return pool.state === "stopped" ? "muted" : "warn";
  }
}

export function runtimeSummary(snapshot: RuntimePoolsSnapshot): string {
  if (!snapshot.pools.length) return "—";
  const ready = snapshot.pools.filter((p) => p.state === "ready").length;
  const active = snapshot.pools.reduce((sum, p) => sum + finiteInt(p.active_workers), 0);
  const capacity = snapshot.pools.reduce((sum, p) => sum + finiteInt(p.capacity), 0);
  return `${ready}/${snapshot.pools.length} ${active}/${capacity}`;
}

/** The most recent human-meaningful problem for a pool, if any. */
export function poolProblem(pool: RuntimePoolStatus): string | null {
  if (pool.failure) return `${pool.failure.category}:${pool.failure.code}`;
  const last = pool.history?.[pool.history.length - 1];
  if (last?.failure) return `${last.failure.category}:${last.failure.code}`;
  return null;
}
