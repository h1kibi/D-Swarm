"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface AuditLog {
  id: string;
  timestamp: number;
  action: string;
  actor_id: string;
  actor_username: string;
  resource_type: string;
  resource_id: string;
  status: "success" | "failure";
  details: Record<string, unknown>;
  ip_address: string;
}

export interface AuditFilters {
  start_date?: number;
  end_date?: number;
  action?: string;
  actor_id?: string;
  status?: "success" | "failure";
  limit?: number;
  offset?: number;
}

export interface AuditState {
  logs: AuditLog[];
  total: number;
  loading: boolean;
  error: string | null;
  refresh: (filters?: AuditFilters) => Promise<void>;
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
        const params = new URLSearchParams();
        const allFilters = { ...initialFilters, ...filters };

        if (allFilters.start_date)
          params.append("start_date", String(allFilters.start_date));
        if (allFilters.end_date) params.append("end_date", String(allFilters.end_date));
        if (allFilters.action) params.append("action", allFilters.action);
        if (allFilters.actor_id) params.append("actor_id", allFilters.actor_id);
        if (allFilters.status) params.append("status", allFilters.status);
        if (allFilters.limit) params.append("limit", String(allFilters.limit));
        if (allFilters.offset) params.append("offset", String(allFilters.offset));

        const query = params.toString() ? `?${params.toString()}` : "";
        const res = await apiFetch(`/api/audit${query}`);

        if (!res.ok) {
          setError(`HTTP ${res.status}`);
          setLogs([]);
          setTotal(0);
          return;
        }

        const body = (await res.json()) as unknown;
        if (body && typeof body === "object") {
          const record = body as Record<string, unknown>;
          const logsList = record.logs ? (record.logs as AuditLog[]) : [];
          const totalCount = record.total ? Number(record.total) : 0;
          setLogs(logsList);
          setTotal(totalCount);
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

export async function exportAuditLogs(filters?: AuditFilters): Promise<{ ok: boolean; data?: Blob; error?: string }> {
  try {
    const params = new URLSearchParams();

    if (filters?.start_date) params.append("start_date", String(filters.start_date));
    if (filters?.end_date) params.append("end_date", String(filters.end_date));
    if (filters?.action) params.append("action", filters.action);
    if (filters?.actor_id) params.append("actor_id", filters.actor_id);
    if (filters?.status) params.append("status", filters.status);

    const query = params.toString() ? `?${params.toString()}` : "";
    const res = await apiFetch(`/api/audit/export${query}`);

    if (!res.ok) {
      return { ok: false, error: `HTTP ${res.status}` };
    }

    const blob = await res.blob();
    return { ok: true, data: blob };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
