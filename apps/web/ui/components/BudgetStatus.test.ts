import { describe, expect, it } from "vitest";
import { budgetRows, budgetUsageLabel, type BudgetSnapshot } from "./budgetStatus";

const snapshot: BudgetSnapshot = {
  run_id: "run-1",
  ledger: { global: { tokens: 1234, unknown_calls: 1, calls: 3 } },
  ledger_state: "ready",
  ledger_error: null,
  budget: {
    profile: { planner: { tokens: 900, calls: 2, cap_tokens: 1000, blocked: false } },
    account: { "acct-1": { tokens: 1200, calls: 3, cap_tokens: 1000, blocked: true } },
  },
};

describe("BudgetStatus projections", () => {
  it("renders profile and account rows with independent scope labels", () => {
    expect(budgetRows(snapshot)).toEqual([
      { scope: "profile", key: "planner", tokens: 900, capTokens: 1000, blocked: false },
      { scope: "account", key: "acct-1", tokens: 1200, capTokens: 1000, blocked: true },
    ]);
  });

  it("formats unknown calls without treating them as zero spend", () => {
    expect(budgetUsageLabel(snapshot)).toBe("1.2k tokens · 1 unknown call");
  });
});
