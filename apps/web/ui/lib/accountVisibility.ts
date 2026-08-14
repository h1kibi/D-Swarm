import type {
  CredentialAccount,
  SystemLoginStatus,
} from "./useRun";

export interface PiAccountVisibility {
  autoBound: boolean;
  envPresent: boolean;
  customEndpoint: boolean;
}

export function piAccountVisibility(
  accounts: CredentialAccount[],
  sysLogin: Record<string, SystemLoginStatus>
): PiAccountVisibility {
  return {
    autoBound: accounts.some(
      (a) =>
        a.account_id === "pi-main" &&
        a.present &&
        a.mode !== "custom_endpoint"
    ),
    envPresent: sysLogin.pi === "present",
    customEndpoint: accounts.some((a) => a.mode === "custom_endpoint"),
  };
}

export function shouldShowAdvancedAccounts(
  advancedOpen: boolean,
  hasCustomEndpoint: boolean,
  editingAccount: boolean
): boolean {
  return advancedOpen || hasCustomEndpoint || editingAccount;
}
