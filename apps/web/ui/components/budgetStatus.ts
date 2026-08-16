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

export interface BudgetSnapshot {
  run_id: string;
  ledger?: {
    global?: BudgetUsage;
    ledger_state?: LedgerState;
    ledger_error?: string | null;
  };
  ledger_state: LedgerState;
  ledger_error?: string | null;
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
