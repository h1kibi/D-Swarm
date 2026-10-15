"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface AuditLog {
  id: string;
  action: string;
  actor: string;
  resource_type: string;
  resource_id: string;
  details: Record<string, unknown>;
  timestamp: number;
  ip_address: string;
  status: string;
}

export interface AuditFilters {
  action?: string;
  actor_id?: string;
  resource_type?: string;
  resource_id?: string;
  status_filter?: string;
  limit?: number;
  skip?: number;
}

export interface AuditState {
  logs: AuditLog[];
  total: number;
  loading: boolean;
  error: string | null;
  refresh: (filters?: AuditFilters) => Promise<void>;
}

function buildQuery(filters: AuditFilters): string {
  const params = new URLSearchParams();
  if (filters.action) params.append("action", filters.action);
  if (filters.actor_id) params.append("actor_id", filters.actor_id);
  if (filters.resource_type) params.append("resource_type", filters.resource_type);
  if (filters.resource_id) params.append("resource_id", filters.resource_id);
  if (filters.status_filter) params.append("status_filter", filters.status_filter);
  if (filters.limit) params.append("limit", String(filters.limit));
  if (filters.skip) params.append("skip", String(filters.skip));
  const query = params.toString();
  return query ? `?${query}` : "";
}

export function useAudit(initialFilters?: AuditFilters): AuditState {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(
    async (filters?: AuditFilters): Promise<void> => {
      setLoading(true);
      try {
        const merged = { ...initialFilters, ...filters };
        const res = await apiFetch(`/api/audit/logs${buildQuery(merged)}`);

        if (!res.ok) {
          setError(`HTTP ${res.status}`);
          setLogs([]);
          setTotal(0);
          return;
        }

        const body = (await res.json()) as unknown;
        if (body && typeof body === "object") {
          const record = body as Record<string, unknown>;
          setLogs(record.logs ? (record.logs as AuditLog[]) : []);
          setTotal(record.total ? Number(record.total) : 0);
        }
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load audit logs");
        setLogs([]);
        setTotal(0);
      } finally {
        setLoading(false);
      }
    },
    [initialFilters]
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { logs, total, loading, error, refresh };
}

export async function exportAuditLogs(
  filters?: AuditFilters
): Promise<{ ok: boolean; data?: Blob; error?: string }> {
  try {
    const res = await apiFetch(`/api/audit/export${buildQuery(filters || {})}`);
    if (!res.ok) {
      return { ok: false, error: `HTTP ${res.status}` };
    }
    return { ok: true, data: await res.blob() };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
