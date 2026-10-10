"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface Account {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "member";
  created_at: number;
  last_login: number | null;
  is_active: boolean;
}

export interface AccountsState {
  accounts: Account[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useAccounts(): AccountsState {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/accounts");
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setAccounts([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const accountsList =
        body && typeof body === "object" && "accounts" in body
          ? ((body as Record<string, unknown>).accounts as Account[])
          : [];
      setAccounts(accountsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load accounts");
      setAccounts([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { accounts, loading, error, refresh };
}

export async function createAccount(data: {
  username: string;
  display_name: string;
  password: string;
  role: "admin" | "member";
}): Promise<{ ok: boolean; account?: Account; error?: string }> {
  try {
    const res = await apiFetch("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to create account";
      return { ok: false, error: detail };
    }

    const result = (await res.json()) as unknown;
    const account =
      result && typeof result === "object" && "account" in result
        ? ((result as Record<string, unknown>).account as Account)
        : undefined;

    return { ok: true, account };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function updateAccount(
  id: string,
  data: Partial<{
    display_name: string;
    role: "admin" | "member";
  }>
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/accounts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to update account";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function toggleAccountActive(
  id: string,
  isActive: boolean
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/accounts/${id}/active`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: isActive }),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to toggle account";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function deleteAccount(id: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/accounts/${id}`, { method: "DELETE" });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to delete account";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
