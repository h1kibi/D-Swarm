import { describe, expect, it } from "vitest";
import {
  piAccountVisibility,
  shouldShowAdvancedAccounts,
} from "../lib/accountVisibility";
import type { CredentialAccount, SystemLoginStatus } from "../lib/useRun";

const account = (
  patch: Partial<CredentialAccount>
): CredentialAccount => ({
  account_id: "pi-main",
  engine: "pi",
  mode: "api_key",
  present: true,
  writable_state: false,
  details: {},
  ...patch,
});

describe("account visibility", () => {
  it("detects auto-bound pi-main and custom endpoints", () => {
    const sysLogin: Record<string, SystemLoginStatus> = { pi: "present" };
    expect(
      piAccountVisibility([account({})], sysLogin)
    ).toMatchObject({
      autoBound: true,
      envPresent: true,
      customEndpoint: false,
    });

    expect(
      piAccountVisibility(
        [account({ mode: "custom_endpoint" })],
        sysLogin
      )
    ).toMatchObject({
      autoBound: false,
      customEndpoint: true,
    });
  });

  it("keeps advanced accounts hidden unless explicitly needed", () => {
    expect(shouldShowAdvancedAccounts(false, false, false)).toBe(false);
    expect(shouldShowAdvancedAccounts(true, false, false)).toBe(true);
    expect(shouldShowAdvancedAccounts(false, true, false)).toBe(true);
    expect(shouldShowAdvancedAccounts(false, false, true)).toBe(true);
  });
});
