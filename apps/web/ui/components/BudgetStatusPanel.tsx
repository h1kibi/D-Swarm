"use client";

import { useT } from "@/lib/i18n";
import {
  budgetRows,
  budgetUsageLabel,
  formatBudgetTokens,
  ledgerConflictInfo,
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
  // A usage conflict is baked into run history: rebuild cannot fix it, so the
  // panel explains the conflict instead of offering a button that always 503s.
  const conflict = state === "failed" && snapshot.ledger_error_kind === "usage_conflict"
    ? ledgerConflictInfo(snapshot.ledger_error)
    : null;
  const rows = budgetRows(snapshot);
  const scopeGroups: { labelKey: string; items: typeof rows }[] = (
    ["profile", "account"] as const
  ).map((scope) => ({
    labelKey: scope === "profile" ? "budget.groupProfile" : "budget.groupAccount",
    items: rows.filter((r) => r.scope === scope)
      .sort((a, b) => b.tokens - a.tokens),
  })).filter((g) => g.items.length > 0);
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

      {state === "failed" && conflict && (
        <div className="budget-alert">
          <span>
            {t("budget.conflictTitle", { call: conflict.callId })}
            <span className="budget-conflict-note">{t("budget.conflictNote")}</span>
          </span>
        </div>
      )}
      {state === "failed" && !conflict && (
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

      {scopeGroups.map((group) => (
        <div className="budget-scope-group" key={group.labelKey}>
          <div className="budget-scope-title">{t(group.labelKey)}</div>
          <div className="budget-scope-list">
          {group.items.map((row) => (
            <div className={`budget-scope-row ${row.blocked ? "blocked" : ""}`} key={`${row.scope}:${row.key}`}>
              <div className="budget-scope-label">
                <span className="budget-scope-key" title={row.key}>{row.key}</span>
              </div>
              <div className="budget-scope-value">
                <span>{formatBudgetTokens(row.tokens)}{row.capTokens != null ? ` / ${formatBudgetTokens(row.capTokens)}` : ""}</span>
                {row.blocked && <span className="budget-blocked">{t("budget.blocked")}</span>}
              </div>
            </div>
          ))}
          </div>
        </div>
      ))}
    </section>
  );
}
