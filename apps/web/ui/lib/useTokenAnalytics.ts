"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface TokenStats {
  provider: string;
  total_tokens: number;
  total_cost: number;
  runs_count: number;
  avg_per_run: number;
  trend: number; // percentage change from last period
}

export interface TokenAnalyticsState {
  stats: TokenStats[];
  totalTokens: number;
  totalCost: number;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useTokenAnalytics(): TokenAnalyticsState {
  const [stats, setStats] = useState<TokenStats[]>([]);
  const [totalTokens, setTotalTokens] = useState(0);
  const [totalCost, setTotalCost] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/analytics/tokens");

      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setStats([]);
        setTotalTokens(0);
        setTotalCost(0);
        return;
      }

      const body = (await res.json()) as unknown;
      if (body && typeof body === "object") {
        const record = body as Record<string, unknown>;
        const statsList = record.stats ? (record.stats as TokenStats[]) : [];
        const total = record.total_tokens ? Number(record.total_tokens) : 0;
        const cost = record.total_cost ? Number(record.total_cost) : 0;

        setStats(statsList);
        setTotalTokens(total);
        setTotalCost(cost);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load analytics");
      setStats([]);
      setTotalTokens(0);
      setTotalCost(0);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { stats, totalTokens, totalCost, loading, error, refresh };
}
