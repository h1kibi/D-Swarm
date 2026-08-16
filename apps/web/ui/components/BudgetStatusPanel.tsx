"use client";

import { useT } from "@/lib/i18n";
import {
  budgetRows,
  budgetUsageLabel,
  formatBudgetTokens,
  type BudgetSnapshot,
  type LedgerState,
} from "./budgetStatus";

interface BudgetStatusProps {
  snapshot?: BudgetSnapshot | null;
  loading?: boolean;
  rebuilding?: boolean;
  error?: string | null;
  onRebuild?: () => void | Promise<void>;
}

function ledgerTone(state: LedgerState): "ok" | "warn" | "bad" | "muted" {
  if (state === "ready") return "ok";
  if (state === "rebuilding") return "warn";
  if (state === "failed") return "bad";
  return "muted";
}

export function BudgetStatus({ snapshot, loading = false, rebuilding = false, error, onRebuild }: BudgetStatusProps) {
  const t = useT();
  if (!snapshot && loading) {
    return <section className="budget-status budget-status-loading" aria-busy="true"><span className="budget-skeleton" /></section>;
  }
  if (!snapshot) {
    return <section className="budget-status budget-status-empty">
      <span>{error || t("budget.unavailable")}</span>
    </section>;
  }

  const state = rebuilding ? "rebuilding" : snapshot.ledger_state;
  const tone = ledgerTone(state);
  const rows = budgetRows(snapshot);
  const global = snapshot.ledger?.global;
  const unknown = Math.max(0, Math.round(global?.unknown_calls ?? 0));
  const estimated = Math.max(0, Math.round(global?.estimated_calls ?? 0));

  return (
    <section className="budget-status" aria-label={t("budget.title")}>
      <div className="budget-status-head">
        <div>
          <div className="budget-eyebrow">{t("budget.title")}</div>
          <div className="budget-usage">{budgetUsageLabel(snapshot)}</div>
        </div>
        <span className={`budget-state ${tone}`}>
          <span className="budget-state-dot" aria-hidden="true" />
          {t(`budget.ledger${state === "ready" ? "Ready" : state === "rebuilding" ? "Rebuilding" : state === "failed" ? "Failed" : "Unavailable"}`)}
        </span>
      </div>

      <div className="budget-meta">
        <span>{t("budget.calls", { n: global?.calls ?? 0 })}</span>
        {unknown > 0 && <span className="budget-meta-warn">{t("budget.unknownCalls", { n: unknown })}</span>}
        {estimated > 0 && <span>{t("budget.estimatedCalls", { n: estimated })}</span>}
      </div>

      {state === "failed" && (
        <div className="budget-alert">
          <span>{snapshot.ledger_error || t("budget.ledgerFailed")}</span>
          {onRebuild && (
            <button type="button" className="budget-rebuild" onClick={() => void onRebuild()} disabled={rebuilding}>
              {rebuilding ? t("budget.rebuilding") : t("budget.rebuild")}
            </button>
          )}
        </div>
      )}
      {state === "rebuilding" && <div className="budget-progress">{t("budget.rebuilding")}</div>}
      {error && state !== "failed" && <div className="budget-error">{error}</div>}

      {rows.length > 0 && (
        <div className="budget-scope-list">
          {rows.map((row) => (
            <div className={`budget-scope-row ${row.blocked ? "blocked" : ""}`} key={`${row.scope}:${row.key}`}>
              <div className="budget-scope-label">
                <span className="budget-scope-kind">{t(`budget.${row.scope}`)}</span>
                <span className="budget-scope-key" title={row.key}>{row.key}</span>
              </div>
              <div className="budget-scope-value">
                <span>{formatBudgetTokens(row.tokens)}{row.capTokens != null ? ` / ${formatBudgetTokens(row.capTokens)}` : ""}</span>
                {row.blocked && <span className="budget-blocked">{t("budget.blocked")}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
