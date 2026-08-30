export type LedgerState = "ready" | "rebuilding" | "failed" | "unavailable" | string;

export interface BudgetUsage {
  tokens: number;
  calls: number;
  unknown_calls?: number;
  estimated_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  usd?: number;
}

export interface BudgetScope {
  tokens: number;
  calls: number;
  cap_tokens?: number | null;
  blocked: boolean;
}

export type LedgerErrorKind = "usage_conflict" | "invalid_event" | null;

export interface BudgetSnapshot {
  run_id: string;
  ledger?: {
    global?: BudgetUsage;
    ledger_state?: LedgerState;
    ledger_error?: string | null;
  };
  ledger_state: LedgerState;
  ledger_error?: string | null;
  /** Machine-readable class of ledger_error (backend snapshot projection). */
  ledger_error_kind?: LedgerErrorKind;
  budget?: {
    profile?: Record<string, BudgetScope>;
    account?: Record<string, BudgetScope>;
  };
}

export interface BudgetRow {
  scope: "profile" | "account";
  key: string;
  tokens: number;
  capTokens: number | null;
  blocked: boolean;
}

function finiteNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function budgetRows(snapshot: BudgetSnapshot): BudgetRow[] {
  const rows: BudgetRow[] = [];
  for (const scope of ["profile", "account"] as const) {
    const values = snapshot.budget?.[scope] ?? {};
    for (const [key, value] of Object.entries(values)) {
      rows.push({
        scope,
        key,
        tokens: finiteNumber(value?.tokens),
        capTokens: value?.cap_tokens == null ? null : finiteNumber(value.cap_tokens),
        blocked: Boolean(value?.blocked),
      });
    }
  }
  return rows;
}

export function formatBudgetTokens(value: number): string {
  const tokens = Math.max(0, finiteNumber(value));
  if (tokens < 1000) return String(Math.round(tokens));
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(tokens / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`;
}

export function budgetUsageLabel(snapshot: BudgetSnapshot): string {
  const global = snapshot.ledger?.global;
  const tokens = formatBudgetTokens(finiteNumber(global?.tokens));
  const unknown = Math.max(0, Math.round(finiteNumber(global?.unknown_calls)));
  const suffix = `${unknown} unknown ${unknown === 1 ? "call" : "calls"}`;
  return `${tokens} tokens · ${suffix}`;
}

export interface LedgerConflictInfo {
  /** Short call id for display (e.g. 6678bec9). */
  callId: string;
}

/** A usage_conflict means one provider call was recorded with two different
 *  outcomes in run history (pre-fix gateway double-record). The pair is baked
 *  into the event log, so rebuild can never reconcile it — the UI explains
 *  this instead of offering a dead button. */
export function ledgerConflictInfo(error: string | null | undefined): LedgerConflictInfo | null {
  const match = /conflicting usage_id: usage::[^:]*::[a-z]*::([0-9a-f-]+)/i.exec(str2(error || ""));
  if (!match) return null;
  return { callId: match[1].slice(0, 8) };
}

function str2(v: unknown): string {
  return typeof v === "string" ? v : "";
}
