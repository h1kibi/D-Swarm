"use client";

import { Icon } from "@/components/Icon";
import type { DeckState } from "@/lib/events";
import { projectObserverSummary } from "@/lib/observerSummary";
import type { RunObservabilitySnapshot } from "@/lib/useRun";
import { useLang } from "@/lib/i18n";

/**
 * Sidebar Observer summary (docs/17 §12.3) — a compact read-only digest shown for
 * the SELECTED Run. It summarises state the deck and the observability snapshot
 * already carry; it is not an LLM answer and never replaces the full
 * Timeline/Worker/Routes/Evidence pages.
 */
export function ObserverSummaryCard({
  deck,
  observability,
  onOpenProgress,
}: {
  deck: DeckState;
  observability?: RunObservabilitySnapshot | null;
  onOpenProgress?: () => void;
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const summary = projectObserverSummary(deck, observability);

  return (
    <section className="observer-summary-card" aria-label={zh ? "观察员摘要" : "Observer summary"}>
      <header className="observer-summary-head">
        <span className="eyebrow">{zh ? "观察员摘要" : "OBSERVER SUMMARY"}</span>
        <strong className={summary.health.warn ? "warn" : "ok"}>{summary.health.status}</strong>
      </header>
      <div className="observer-summary-signals">
        <span className={summary.reason.warn ? "warn" : ""}>
          <small>{summary.reason.label}</small>
          <b>{summary.reason.value}</b>
        </span>
        {summary.activity.map((signal) => (
          <span key={signal.label} className={signal.warn ? "warn" : ""}>
            <small>{signal.label}</small>
            <b>{signal.value}</b>
          </span>
        ))}
      </div>
      {summary.lastOutput && (
        <div className="observer-summary-output">
          <small>{zh ? "最近真实输出" : "Latest real output"}</small>
          <code>{summary.lastOutput}</code>
        </div>
      )}
      <div className="observer-summary-signals">
        {summary.evidence.map((signal) => (
          <span key={signal.label} className={signal.warn ? "warn" : ""}>
            <small>{signal.label}</small>
            <b>{signal.value}</b>
          </span>
        ))}
      </div>
      <div className="observer-summary-next">
        <small>{summary.nextDecider.label}</small>
        <b>{summary.nextDecider.value}</b>
      </div>
      {summary.operatorAttention.length > 0 && (
        <ul className="observer-summary-attention">
          {summary.operatorAttention.slice(0, 4).map((row) => (
            <li key={row}><Icon name="alert" size={12} /><span>{row}</span></li>
          ))}
        </ul>
      )}
      {onOpenProgress && (
        <button type="button" className="observer-summary-open" onClick={onOpenProgress}>
          <Icon name="help" size={13} />
          {zh ? "询问进展（只读）" : "Ask Progress (read-only)"}
        </button>
      )}
    </section>
  );
}
