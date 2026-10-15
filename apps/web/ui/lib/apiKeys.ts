"use client";

import { apiFetch } from "./useRun";

/** API-key metadata (never the plaintext token — that appears once at creation). */
export interface ApiKeyInfo {
  key_id: string;
  provider: string;
  created_at: number | null;
  revoked_at: number | null;
  is_active: boolean;
}

export async function fetchApiKeys(): Promise<ApiKeyInfo[]> {
  try {
    const res = await apiFetch("/api/accounts/me/api-keys");
    if (!res.ok) return [];
    const body = (await res.json()) as unknown;
    return body && typeof body === "object" && "keys" in body
      ? ((body as Record<string, unknown>).keys as ApiKeyInfo[])
      : [];
  } catch {
    return [];
  }
}

export async function createApiKey(): Promise<{ ok: boolean; token?: string; error?: string }> {
  try {
    const res = await apiFetch("/api/accounts/me/api-keys", { method: "POST" });
    if (!res.ok) {
      return { ok: false, error: `HTTP ${res.status}` };
    }
    const body = (await res.json()) as unknown;
    const token =
      body && typeof body === "object" && "api_token" in body
        ? String((body as Record<string, unknown>).api_token ?? "")
        : "";
    return token ? { ok: true, token } : { ok: false, error: "No token returned" };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function revokeApiKey(keyId: string): Promise<boolean> {
  try {
    const res = await apiFetch(`/api/accounts/me/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
    });
    return res.ok;
  } catch {
    return false;
  }
}
