"use client";

import { useT } from "@/lib/i18n";
import {
  poolProblem,
  poolTone,
  runtimeSummary,
  type RuntimePoolsSnapshot,
  type RuntimePoolStatus,
} from "./runtimeStatus";

interface RuntimeStatusProps {
  snapshot?: RuntimePoolsSnapshot | null;
  loading?: boolean;
  error?: string | null;
}

const STATE_LABEL_KEY: Record<string, string> = {
  new: "runtime.stateNew",
  ready: "runtime.stateReady",
  starting: "runtime.stateStarting",
  probing: "runtime.stateProbing",
  degraded: "runtime.stateDegraded",
  recovering: "runtime.stateRecovering",
  stopping: "runtime.stateStopping",
  stopped: "runtime.stateStopped",
};

type TFn = (key: string, vars?: Record<string, string | number>) => string;

function PoolRow({ pool, t }: { pool: RuntimePoolStatus; t: TFn }) {
  const tone = poolTone(pool);
  const problem = poolProblem(pool);
  const labelKey = STATE_LABEL_KEY[pool.state] ?? "runtime.stateUnknown";
  const generations = pool.history?.length ?? 0;
  return (
    <div className={`runtime-pool-row ${tone === "bad" ? "bad" : ""}`}>
      <div className="budget-scope-label">
        <span className={`budget-state ${tone}`}>
          <span className="budget-state-dot" aria-hidden="true" />
          {t(labelKey)}
        </span>
        <span className="budget-scope-key" title={pool.pool_id}>{pool.pool_id}</span>
      </div>
      <div className="budget-scope-value">
        <span>{t("runtime.workers", { active: pool.active_workers, capacity: pool.capacity })}</span>
        {pool.generation > 0 && <span className="runtime-gen">g{pool.generation}</span>}
      </div>
      {problem && <div className="runtime-problem">{problem}</div>}
      {generations > 1 && (
        <div className="runtime-history-hint">{t("runtime.transitions", { n: generations })}</div>
      )}
    </div>
  );
}

export function RuntimeStatus({ snapshot, loading = false, error }: RuntimeStatusProps) {
  const t = useT();
  if (!snapshot && loading) {
    return <section className="budget-status budget-status-loading" aria-busy="true"><span className="budget-skeleton" /></section>;
  }
  if (!snapshot) {
    return <section className="budget-status budget-status-empty">
      <span>{error || t("runtime.unavailable")}</span>
    </section>;
  }
  const pools = snapshot.pools;
  return (
    <section className="budget-status" aria-label={t("runtime.title")}>
      <div className="budget-status-head">
        <div>
          <div className="budget-eyebrow">{t("runtime.title")}</div>
          <div className="budget-usage">{runtimeSummary(snapshot)}</div>
        </div>
        {snapshot.policy_mode && (
          <span className="budget-state muted">{snapshot.policy_mode}</span>
        )}
      </div>
      {pools.length === 0 ? (
        <div className="budget-meta"><span>{t("runtime.noPools")}</span></div>
      ) : (
        <div className="budget-scope-list">
          {pools.map((pool) => <PoolRow key={pool.pool_id} pool={pool} t={t} />)}
        </div>
      )}
      {error && <div className="budget-error">{error}</div>}
    </section>
  );
}
