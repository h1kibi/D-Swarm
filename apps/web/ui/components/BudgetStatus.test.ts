import { describe, expect, it } from "vitest";
import { budgetRows, budgetUsageLabel, ledgerConflictInfo, type BudgetSnapshot } from "./budgetStatus";

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


describe("usage-conflict attribution", () => {
  it("extracts a short call id from the raw ledger error", () => {
    const conflict = ledgerConflictInfo(
      "conflicting usage_id: usage::run-6038::gateway::6678bec9f4574e4dac7fe3b06f7102e2",
    );
    expect(conflict).toEqual({ callId: "6678bec9" });
    // other errors are not conflicts
    expect(ledgerConflictInfo("canonical_append_failed")).toBeNull();
    expect(ledgerConflictInfo(null)).toBeNull();
  });
});
