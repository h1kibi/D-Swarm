"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

/**
 * Deck run summary shape (from GET /api/runs, served by runs.py).
 * Flag values are redacted for unauthenticated callers; `flag_count` stays.
 */
export interface RunSummary {
  run_id: string;
  name: string;
  category?: string;
  target_id?: string | null;
  target_name?: string | null;
  status: string;
  started?: boolean;
  finished?: boolean;
  solved?: boolean;
  flag_count?: number;
  flags?: string[];
  flags_redacted?: boolean;
  archived?: boolean;
  updated_at?: number;
}

export interface RunsState {
  runs: RunSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useRuns(): RunsState {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/runs?archived=1");
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setRuns([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const runsList =
        body && typeof body === "object" && "runs" in body
          ? ((body as Record<string, unknown>).runs as RunSummary[])
          : [];
      setRuns(runsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load runs");
      setRuns([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { runs, loading, error, refresh };
}
