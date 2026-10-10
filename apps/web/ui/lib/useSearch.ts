"use client";

import { useCallback, useState } from "react";
import { apiFetch } from "./useRun";

export interface SearchResult {
  id: string;
  type: "event" | "target" | "run";
  title: string;
  description: string;
  metadata: Record<string, unknown>;
}

export interface SearchFilters {
  query?: string;
  type?: "all" | "event" | "target" | "run";
  sort?: "relevance" | "date" | "name";
  limit?: number;
}

export function useSearch() {
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(async (filters: SearchFilters): Promise<void> => {
    if (!filters.query || filters.query.length < 2) {
      setResults([]);
      return;
    }

    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (filters.query) params.append("q", filters.query);
      if (filters.type && filters.type !== "all") params.append("type", filters.type);
      if (filters.sort) params.append("sort", filters.sort);
      if (filters.limit) params.append("limit", String(filters.limit));

      const res = await apiFetch(`/api/search?${params.toString()}`);

      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setResults([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const resultsList =
        body && typeof body === "object" && "results" in body
          ? ((body as Record<string, unknown>).results as SearchResult[])
          : [];

      setResults(resultsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { results, loading, error, search };
}
