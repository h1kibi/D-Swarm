"use client";

import { BudgetStatus } from "@/components/BudgetStatusPanel";
import { Icon } from "@/components/Icon";
import type { BudgetSnapshot } from "@/components/budgetStatus";
import type { RunObservabilitySnapshot } from "@/lib/useRun";
import { useLang } from "@/lib/i18n";

function formatTokens(value: number): string {
  if (value < 1_000) return Math.round(value).toLocaleString();
  return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)}k`;
}

function formatUsd(value: number): string {
  return value < 0.01 && value > 0 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function LedgerWorkspace({
  budget,
  observability,
  loading,
  rebuilding,
  error,
  onRebuild,
}: {
  budget?: BudgetSnapshot | null;
  observability?: RunObservabilitySnapshot | null;
  loading?: boolean;
  rebuilding?: boolean;
  error?: string | null;
  onRebuild?: () => void | Promise<void>;
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const usage = observability?.usage;
  const roles = observability?.binding.roles ?? [];

  return (
    <section className="ledger-workspace" aria-label={zh ? "Run 账本" : "Run ledger"}>
      <header className="run-view-heading">
        <div>
          <span className="eyebrow">USAGE / LEDGER</span>
          <h2>{zh ? "调用与成本账本" : "Usage and cost ledger"}</h2>
          <p>{zh ? "按角色、Provider 和账户核对调用、Token、成本及未知计量；账本状态来自可重放事件。" : "Audit calls, tokens, cost, and unknown accounting by role, provider, and account."}</p>
        </div>
        <div className="ledger-health">
          <span className={`ledger-health-dot ${observability?.health.status || "unknown"}`} />
          <div><small>{zh ? "运行健康" : "Run health"}</small><strong>{observability?.health.status || (loading ? (zh ? "加载中" : "Loading") : (zh ? "不可用" : "Unavailable"))}</strong></div>
        </div>
      </header>

      <div className="ledger-summary-grid">
        <div className="ledger-summary-item"><Icon name="plug" size={15} /><span>{zh ? "调用" : "Calls"}</span><strong>{usage?.calls ?? 0}</strong></div>
        <div className="ledger-summary-item"><Icon name="cpu" size={15} /><span>Tokens</span><strong>{formatTokens(usage?.tokens ?? 0)}</strong></div>
        <div className="ledger-summary-item"><Icon name="upload" size={15} /><span>{zh ? "输入" : "Input"}</span><strong>{formatTokens(usage?.input_tokens ?? 0)}</strong></div>
        <div className="ledger-summary-item"><Icon name="terminal" size={15} /><span>{zh ? "输出" : "Output"}</span><strong>{formatTokens(usage?.output_tokens ?? 0)}</strong></div>
        <div className="ledger-summary-item"><Icon name="alert" size={15} /><span>{zh ? "未知调用" : "Unknown"}</span><strong>{usage?.unknown_calls ?? 0}</strong></div>
        <div className="ledger-summary-item"><Icon name="layers" size={15} /><span>{zh ? "成本" : "Cost"}</span><strong>{formatUsd(usage?.usd ?? 0)}</strong></div>
      </div>

      <div className="ledger-layout">
        <div className="ledger-role-panel">
          <div className="intent-section-label">ROLE ATTRIBUTION</div>
          {roles.length === 0 ? (
            <div className="ledger-empty">{loading ? (zh ? "正在加载角色计量…" : "Loading role usage…") : (zh ? "此 Run 没有可用的角色计量。" : "No role attribution is available for this run.")}</div>
          ) : (
            <div className="ledger-table" role="table" aria-label={zh ? "角色计量" : "Role usage"}>
              <div className="ledger-table-row header" role="row">
                <span role="columnheader">{zh ? "角色" : "Role"}</span><span role="columnheader">Provider / Model</span><span role="columnheader">Calls</span><span role="columnheader">Tokens</span><span role="columnheader">Cost</span><span role="columnheader">Status</span>
              </div>
              {roles.map((role) => {
                const binding = Object.keys(role.effective || {}).length ? role.effective : role.frozen;
                const provider = String(binding.provider || binding.provider_id || binding.provider_ref || "-");
                const model = String(binding.model || binding.model_id || "-");
                return (
                  <div className={`ledger-table-row status-${role.status}`} role="row" key={role.role}>
                    <strong role="cell">{role.role}</strong>
                    <span role="cell" title={`${provider} / ${model}`}><b>{provider}</b><small>{model}</small></span>
                    <span role="cell">{role.usage.calls}</span>
                    <span role="cell">{formatTokens(role.usage.tokens)}</span>
                    <span role="cell">{formatUsd(role.usage.usd)}</span>
                    <span role="cell"><i className={`ledger-status ${role.status}`} />{role.status}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <aside className="ledger-budget-panel">
          <div className="intent-section-label">BUDGET GATES</div>
          <BudgetStatus snapshot={budget} loading={loading} rebuilding={rebuilding} error={error} onRebuild={onRebuild} />
        </aside>
      </div>

      {!!observability?.health.issues.length && (
        <section className="ledger-issues">
          <div className="intent-section-label">ACCOUNTING & PROVIDER ISSUES</div>
          {observability.health.issues.map((issue, index) => (
            <article key={`${issue.kind}-${issue.role || "global"}-${index}`}>
              <Icon name="alert" size={14} />
              <div><strong>{issue.role || "global"} · {issue.category}</strong><p>{issue.message || issue.kind}</p>{issue.suggested_action && <small>{issue.suggested_action}</small>}</div>
              <span className={`intent-state ${issue.severity}`}>{issue.severity}</span>
            </article>
          ))}
        </section>
      )}
    </section>
  );
}
