"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface Target {
  id: string;
  event_id: string;
  name: string;
  description: string;
  category: string;
  target_address: string | null;
  expected_flags: number;
  is_solved: boolean;
  solved_by: string | null;
  flags: string[];
  facts_count: number;
  created_by: string;
  created_at: number;
  updated_at: number;
}

export interface TargetsState {
  targets: Target[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useTargets(eventId?: string): TargetsState {
  const [targets, setTargets] = useState<Target[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const query = eventId ? `?event_id=${encodeURIComponent(eventId)}` : "";
      const res = await apiFetch(`/api/targets${query}`);
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setTargets([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const targetsList =
        body && typeof body === "object" && "targets" in body
          ? ((body as Record<string, unknown>).targets as Target[])
          : [];
      setTargets(targetsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load targets");
      setTargets([]);
    } finally {
      setLoading(false);
    }
  }, [eventId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { targets, loading, error, refresh };
}

export async function createTarget(data: {
  name: string;
  description: string;
  category: string;
  event_id: string;
}): Promise<{ ok: boolean; target?: Target; error?: string }> {
  try {
    const res = await apiFetch("/api/targets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to create target";
      return { ok: false, error: detail };
    }

    const result = (await res.json()) as unknown;
    const target =
      result && typeof result === "object" && "target" in result
        ? ((result as Record<string, unknown>).target as Target)
        : undefined;

    return { ok: true, target };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function updateTarget(
  id: string,
  data: Partial<{
    name: string;
    description: string;
    category: string;
    solved: boolean;
  }>
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/targets/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to update target";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function deleteTarget(id: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/targets/${id}`, { method: "DELETE" });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to delete target";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
