"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface RunInfo {
  id: string;
  target_id: string;
  event_id: string;
  created_by: string;
  created_at: number;
  status: "pending" | "running" | "completed" | "failed" | "stopped";
  solved: boolean;
  flags_found: string[];
  facts_count: number;
  token_usage: Record<string, number>;
}

export interface RunsState {
  runs: RunInfo[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useRuns(eventId?: string): RunsState {
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const query = eventId ? `?event_id=${encodeURIComponent(eventId)}` : "";
      const res = await apiFetch(`/api/runs${query}`);
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setRuns([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const runsList =
        body && typeof body === "object" && "runs" in body
          ? ((body as Record<string, unknown>).runs as RunInfo[])
          : [];
      setRuns(runsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load runs");
      setRuns([]);
    } finally {
      setLoading(false);
    }
  }, [eventId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { runs, loading, error, refresh };
}

export async function startRun(data: {
  target_id: string;
  event_id: string;
}): Promise<{ ok: boolean; run?: RunInfo; error?: string }> {
  try {
    const res = await apiFetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to start run";
      return { ok: false, error: detail };
    }

    const result = (await res.json()) as unknown;
    const run =
      result && typeof result === "object" && "run" in result
        ? ((result as Record<string, unknown>).run as RunInfo)
        : undefined;

    return { ok: true, run };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function stopRun(id: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/runs/${id}/stop`, { method: "POST" });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to stop run";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function deleteRun(id: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/runs/${id}`, { method: "DELETE" });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to delete run";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
