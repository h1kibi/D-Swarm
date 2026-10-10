"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface TargetRecord {
  id: string;
  name: string;
  kind?: string;
  category?: string;
  description?: string;
  primary_direction?: string | null;
  primary_direction_description?: string | null;
  target_address?: string | null;
  expected_flags?: number;
  multi_flag?: boolean;
  flag_format_hint?: string;
  primary_direction_id?: string | null;
  directions?: Array<Record<string, unknown>>;
  hints?: Array<Record<string, unknown>>;
  run_ids?: string[];
  verified_facts?: Array<Record<string, unknown>>;
  audit_records?: Array<Record<string, unknown>>;
  activity?: Array<Record<string, unknown>>;
  archived_at?: number | null;
  created_at?: number;
  updated_at?: number;
  status?: string;
  run_count?: number;
  active_run_count?: number;
  verified_fact_count?: number;
  accepted_flag_count?: number;
  last_activity_at?: number | null;
  runs?: unknown[];
}

export type TargetReviewItemType = "fact" | "summary" | "route_constraint" | "dead_end" | "credential_lead";
export interface TargetReviewItem extends Record<string, unknown> {
  item_type: TargetReviewItemType;
  source_run_id: string;
  fact_seq?: number;
  dead_end_seq?: number;
  text?: string;
  status?: string;
  route_hash?: string;
  artifact_ids?: string[];
}
export interface TargetReviewSnapshot {
  summary: TargetReviewItem & { target_id: string; run_count: number; terminal_run_count: number; terminal_statuses?: Array<Record<string, unknown>> };
  known_facts: TargetReviewItem[];
  attempted_routes: TargetReviewItem[];
  failures_runtime: TargetReviewItem[];
  evidence_refs: TargetReviewItem[];
  intent_decisions: Array<Record<string, unknown>>;
  usage_ledger: Array<Record<string, unknown>>;
  provenance_chain: Array<Record<string, unknown>>;
}

export async function fetchTargetReview(targetId: string): Promise<TargetReviewSnapshot | null> {
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/review`);
    if (!response.ok) return null;
    const data = await response.json().catch(() => null);
    return data && typeof data === "object" ? data as TargetReviewSnapshot : null;
  } catch {
    return null;
  }
}

export async function fetchTargets(signal?: AbortSignal): Promise<TargetRecord[]> {
  const response = await apiFetch("/api/targets", { signal });
  const data = await response.json().catch(() => ({} as Record<string, unknown>));
  if (!response.ok) throw new Error(String(data?.detail || `targets request failed (${response.status})`));
  return Array.isArray(data?.targets) ? data.targets as TargetRecord[] : [];
}

export function useTargets(pollMs = 5000, bump = 0): TargetRecord[] {
  const [targets, setTargets] = useState<TargetRecord[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    let alive = true;
    fetchTargets(controller.signal).then((next) => {
      if (alive) setTargets(next);
    }).catch(() => {
      // The projection from /api/runs remains a fully functional compatibility
      // path when an older backend has no Target API yet.
    });
    const timer = window.setInterval(() => {
      fetchTargets(controller.signal).then((next) => {
        if (alive) setTargets(next);
      }).catch(() => undefined);
    }, Math.max(1500, pollMs));
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [pollMs, bump]);
  return targets;
}


export async function createTarget(payload: Record<string, unknown>): Promise<TargetRecord | null> {
  try {
    const response = await apiFetch("/api/targets", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    return response.ok && data.target ? data.target as TargetRecord : null;
  } catch {
    return null;
  }
}

export async function patchTarget(targetId: string, changes: Record<string, unknown>): Promise<TargetRecord | null> {
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    });
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    return response.ok && data.target ? data.target as TargetRecord : null;
  } catch {
    return null;
  }
}

export async function archiveTarget(targetId: string, archived = true): Promise<TargetRecord | null> {
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/archive`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ archived }),
    });
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    return response.ok && data.target ? data.target as TargetRecord : null;
  } catch {
    return null;
  }
}

export async function createTargetDirection(targetId: string, payload: { label: string; description?: string; category?: string }): Promise<Record<string, unknown> | null> {
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/directions`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    return response.ok && data.direction ? data.direction as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export async function addTargetHint(targetId: string, text: string, options: { source?: string; pinned?: boolean } = {}): Promise<Record<string, unknown> | null> {
  const normalized = text.trim();
  if (!normalized) return null;
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/hints`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: normalized, source: options.source || "operator", pinned: options.pinned ?? false }),
    });
    const data = await response.json().catch(() => ({} as Record<string, unknown>));
    return response.ok && data.hint ? data.hint as Record<string, unknown> : null;
  } catch {
    return null;
  }
}
export async function auditTargetReview(targetId: string, items: Array<Record<string, unknown>>): Promise<boolean> {
  try {
    const response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/audit-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function createTargetRun(targetId: string, payload: Record<string, unknown> = {}): Promise<{ run_id: string }> {
  let response: Response;
  try {
    response = await apiFetch(`/api/targets/${encodeURIComponent(targetId)}/runs`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
  } catch {
    // Network failures do not have a safe server detail to show to the operator.
    throw new Error("target run request failed");
  }
  const data = await response.json().catch(() => ({} as Record<string, unknown>));
  if (!response.ok) {
    // Target routes expose stable operator-facing validation details. Never
    // surface an arbitrary 5xx body, which may contain host/SDK diagnostics.
    const detail = response.status < 500 && typeof data?.detail === "string"
      ? data.detail.trim().slice(0, 500)
      : "";
    throw new Error(detail || `target run request failed (${response.status})`);
  }
  const run = data?.run as { run_id?: unknown } | undefined;
  if (!run || typeof run.run_id !== "string" || !run.run_id.trim()) {
    throw new Error("target run response missing run_id");
  }
  return run as { run_id: string };
}
