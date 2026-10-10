"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { DeckState, EventType, DSwarmEvent, emptyDeck, reduce } from "./events";
import { readKey, writeKey, removeKey } from "./storage";
import type { BudgetSnapshot } from "@/components/budgetStatus";
import type { RuntimePoolsSnapshot } from "@/components/runtimeStatus";

/**
 * API base. Empty string = same-origin: `run.sh web` serves the production
 * Next UI and proxies /api to the FastAPI backend. NEXT_PUBLIC_DSWARM_API is
 * still available for manual experiments that intentionally bypass that proxy.
 */
export const API = process.env.NEXT_PUBLIC_DSWARM_API || "";

// ---------------------------------------------------------------------------
// Auth (P3): single-password gate. The operator types a password once; the
// backend returns a signed session token we keep in localStorage and attach to
// every /api request. The password itself is never stored. SSE/WS connections
// (which can't carry a header) use a one-time ticket minted via apiFetch.
// ---------------------------------------------------------------------------
const TOKEN_KEY = "dswarm_auth_token";

export function getToken(): string {
  return readKey(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  if (token) writeKey(TOKEN_KEY, token);
  else removeKey(TOKEN_KEY);
}

// When a request comes back 401 the token is stale/missing; clear it and notify
// the app shell so it can show the login gate. The shell subscribes via
// onAuthRequired(); we keep it a tiny pub-sub to avoid threading a context
// through every standalone fetch helper.
type AuthListener = () => void;
const authListeners = new Set<AuthListener>();
export function onAuthRequired(fn: AuthListener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}
function fireAuthRequired(): void {
  setToken("");
  authListeners.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore listener errors */
    }
  });
}

/**
 * Authenticated fetch. Prepends the API base, attaches the bearer token, and
 * routes 401s to the login gate. `path` is the API-relative path (e.g.
 * "/api/runs"); callers pass the same path they used to build by hand.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API}${path}`, { ...init, headers });
  if (res.status === 401) fireAuthRequired();
  return res;
}

/** POST the operator password; on success store the returned session token. */
export async function login(password: string): Promise<{ ok: boolean; authRequired: boolean }> {
  const res = await fetch(`${API}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) return { ok: false, authRequired: true };
  const data = await res.json().catch(() => ({} as any));
  if (data?.token) setToken(String(data.token));
  return { ok: true, authRequired: Boolean(data?.auth_required) };
}

/** True if the current token is accepted (or auth is disabled). */
export async function checkAuth(): Promise<{ authenticated: boolean; authRequired: boolean; inContainer: boolean }> {
  const res = await apiFetch("/api/auth/me");
  if (res.status === 401) return { authenticated: false, authRequired: true, inContainer: false };
  const data = await res.json().catch(() => ({} as any));
  // in_container (P2-v3): the control plane runs in a container 鈫抰he deck must
  // force container mode and disable the "local" worker-isolation toggle.
  return { authenticated: true, authRequired: Boolean(data?.auth_required), inContainer: Boolean(data?.in_container) };
}

/**
 * Mint a one-time ticket for opening an SSE/WS connection (no header possible).
 * Returns "" when auth is disabled or the mint fails 鈥攃allers append it as a
 * query param only when non-empty.
 */
export async function authTicket(): Promise<string> {
  try {
    const res = await apiFetch("/api/auth/ticket", { method: "POST" });
    if (!res.ok) return "";
    const data = await res.json().catch(() => ({} as any));
    return data?.ticket ? String(data.ticket) : "";
  } catch {
    return "";
  }
}

export type RunStatus = "draft" | "queued" | "running" | "paused" | "reason_blocked" | "degraded" | "fallback_active" | "solved" | "finished" | "failed" | "cancelled";

export const isDraftRunId = (id: string) => id.startsWith("draft-");

export async function fetchBudgetSnapshot(runId: string, signal?: AbortSignal): Promise<BudgetSnapshot> {
  const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/budget`, { signal });
  const data = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    throw new Error(String(data?.detail || `budget request failed (${res.status})`));
  }
  return data as BudgetSnapshot;
}

export async function rebuildBudget(runId: string): Promise<BudgetSnapshot> {
  const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/budget/rebuild`, { method: "POST" });
  const data = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    throw new Error(String(data?.detail || `budget rebuild failed (${res.status})`));
  }
  return data as BudgetSnapshot;
}

export function useBudgetSnapshot(runId: string, pollMs = 4000) {
  const [snapshot, setSnapshot] = useState<BudgetSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!runId || isDraftRunId(runId)) return null;
    setLoading(true);
    try {
      const next = await fetchBudgetSnapshot(runId, signal);
      if (!signal?.aborted) {
        setSnapshot(next);
        setError(null);
      }
      return next;
    } catch (err) {
      if (!signal?.aborted) setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (!runId || isDraftRunId(runId)) {
      setSnapshot(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let alive = true;
    const poll = async () => {
      if (!alive) return;
      await refresh(controller.signal);
    };
    void poll();
    const timer = window.setInterval(() => void poll(), Math.max(1000, pollMs));
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [runId, pollMs, refresh]);

  const rebuild = useCallback(async () => {
    if (!runId || isDraftRunId(runId) || rebuilding) return null;
    setRebuilding(true);
    setError(null);
    try {
      const next = await rebuildBudget(runId);
      setSnapshot(next);
      return next;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      throw err;
    } finally {
      setRebuilding(false);
    }
  }, [runId, rebuilding]);

  return { snapshot, loading, error, rebuilding, refresh, rebuild };
}

export async function fetchRuntimePools(runId: string, signal?: AbortSignal): Promise<RuntimePoolsSnapshot> {
  const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/runtime-pools`, { signal });
  const data = await res.json().catch(() => ({} as any));
  if (!res.ok) {
    throw new Error(String(data?.detail || `runtime-pools request failed (${res.status})`));
  }
  return data as RuntimePoolsSnapshot;
}

export interface RunRoleUsage {
  calls: number;
  tokens: number;
  input_tokens: number;
  output_tokens: number;
  usd: number;
  unknown_calls: number;
  estimated_calls: number;
  last_call_at?: number | null;
}

export interface RunRoleObservability {
  role: "reason" | "worker" | "review" | "observer" | "titler" | string;
  configured: RunBindingRef;
  frozen: RunBindingRef;
  effective: RunBindingRef;
  configured_source?: string;
  effective_source: string;
  fallback_candidate?: RunBindingRef | string;
  fallback?: RunBindingRef;
  recovery?: Array<{
    kind: string;
    attempt: number;
    window_s: number;
    probe?: { ok?: boolean; error_layer?: string; detail?: string };
    blocked?: boolean;
    blocked_reason?: string;
    reason?: string;
    ts?: number;
  }>;
  operator_attention?: boolean;
  status: "active" | "ready" | "degraded" | "fallback_active" | "blocked" | "unconfigured" | string;
  usage: RunRoleUsage;
  issues: RunHealthIssue[];
}

export interface RunHealthIssue {
  kind: string;
  role?: string;
  category: string;
  severity: string;
  retryable: boolean;
  provider?: string;
  message?: string;
  suggested_action?: string;
  ts?: number;
}

/** Host-owned Reason health (docs/17 §5.4/§5.5). Counts and thresholds only —
 *  never provider exception text. */
export interface RunReasonHealth {
  state: "healthy" | "degraded" | "retrying" | "circuit_open" | "reason_blocked" | "waiting_for_operator" | "unknown" | string;
  failures: number;
  max_failures: number;
  request_failures: number;
  request_limit: number;
  retry_window_s: number;
  operator_attention: boolean;
  immediate: boolean;
  fallback_active: boolean;
  blocked_reason: string;
  ts: number;
  retry_not_before?: number;
}

export interface RunObservabilitySnapshot {
  run_id: string;
  generated_at: number;
  lifecycle: {
    status: string;
    started_at?: number | null;
    finished_at?: number | null;
    elapsed_ms: number;
    updated_at?: number | null;
  };
  knowledge: {
    inherited: Array<{
      fact_id: string;
      fact: string;
      provenance_status: string;
      source_run_id?: string;
      fact_seq?: number;
      artifact_id?: string;
    }>;
    source: string;
    read_only: boolean;
  };
  binding: {
    version?: number | null;
    frozen: boolean;
    source: string;
    roles: RunRoleObservability[];
    review_policy?: RunBindingRef;
  };
  usage: RunRoleUsage;
  health: {
    status: string;
    issues: RunHealthIssue[];
    degraded: Array<{ role?: string; engine?: string; reason?: string; ts?: number }>;
    reason?: RunReasonHealth;
  };
}

export async function fetchRunObservability(runId: string, signal?: AbortSignal): Promise<RunObservabilitySnapshot> {
  const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/observability`, { signal });
  const data = await res.json().catch(() => ({} as any));
  if (!res.ok) throw new Error(String(data?.detail || `observability request failed (${res.status})`));
  return data as RunObservabilitySnapshot;
}

export function useRunObservability(runId: string, pollMs = 4000) {
  const [snapshot, setSnapshot] = useState<RunObservabilitySnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!runId || isDraftRunId(runId)) return null;
    setLoading(true);
    try {
      const next = await fetchRunObservability(runId, signal);
      if (!signal?.aborted) { setSnapshot(next); setError(null); }
      return next;
    } catch (err) {
      if (!signal?.aborted) setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (!runId || isDraftRunId(runId)) {
      setSnapshot(null); setError(null); setLoading(false); return;
    }
    const controller = new AbortController();
    let alive = true;
    const poll = async () => { if (alive) await refresh(controller.signal); };
    void poll();
    const timer = window.setInterval(() => void poll(), Math.max(1000, pollMs));
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [runId, pollMs, refresh]);

  return { snapshot, loading, error, refresh };
}
export function useRuntimePools(runId: string, pollMs = 4000) {
  const [snapshot, setSnapshot] = useState<RuntimePoolsSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!runId || isDraftRunId(runId)) return null;
    setLoading(true);
    try {
      const next = await fetchRuntimePools(runId, signal);
      if (!signal?.aborted) {
        setSnapshot(next);
        setError(null);
      }
      return next;
    } catch (err) {
      if (!signal?.aborted) setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (!runId || isDraftRunId(runId)) {
      setSnapshot(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let alive = true;
    const poll = async () => {
      if (!alive) return;
      await refresh(controller.signal);
    };
    void poll();
    const timer = window.setInterval(() => void poll(), Math.max(1000, pollMs));
    return () => {
      alive = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [runId, pollMs, refresh]);

  return { snapshot, loading, error, refresh };
}

/**
 * Public, non-secret binding metadata frozen when a Run is launched.
 *
 * The backend deliberately returns references (provider/model/profile) rather
 * than credentials. Keep this type permissive because old Runs may have no
 * snapshot and newer adapters can add display-only fields without breaking the
 * deck.
 */
export type RunBindingRef = Record<string, unknown>;

export interface RunBindingSnapshot {
  version?: number;
  worker_profile?: RunBindingRef;
  worker_profiles?: RunBindingRef[];
  roles?: {
    reason?: RunBindingRef;
    worker?: RunBindingRef;
    review?: RunBindingRef;
    observer?: RunBindingRef;
    titler?: RunBindingRef;
    [key: string]: RunBindingRef | undefined;
  };
  llm_mode?: "settings_default" | "per_run_override" | string;
  role_sources?: Record<string, string>;
  effective_sources?: Record<string, string>;
  fallback_candidates?: Record<string, RunBindingRef | string>;
  fallback?: Record<string, RunBindingRef>;
  review_policy?: RunBindingRef;
  [key: string]: unknown;
}

/** One run as the thread rail lists it (matches RunManager.Run.summary()). */
export interface RunSummary {
  run_id: string;
  name: string;
  category: string;
  target_id?: string | null;
  target_name?: string | null;
  target_category?: string | null;
  direction_id?: string | null;
  direction?: string | null;
  inherited_verified_facts?: Array<{
    fact_id: string;
    fact: string;
    provenance_status: string;
    source_run_id?: string;
    fact_seq?: number;
    artifact_id?: string;
  }>;
  inherited_verified_fact_count?: number;
  /** Immutable launch configuration; never contains API keys or tokens. */
  binding_snapshot?: RunBindingSnapshot | null;
  /** Non-secret integrity diagnostics for legacy/imported Target relationships. */
  dispatch_diagnostics?: string[];
  started: boolean;
  finished: boolean;
  solved: boolean;
  paused: boolean;
  status: RunStatus;
  reason_blocked?: boolean;
  reason_blocked_reason?: string;
  degraded?: boolean;
  degraded_reason?: string;
  fallback_active?: boolean;
  fallback_reason?: string;
  queued?: boolean;
  queue_position?: number | null;
  cancelled?: boolean;
  flag?: string | null;
  // multi-flag progress (backend summary already sends these; the Run Fleet
  // rows render "flags x/y" from them). Unauthenticated list responses REDACT
  // the values (contain flag_count instead).
  flag_count?: number;
  flags?: string[];
  expected_flags?: number;
  multi_flag?: boolean;
  awaiting_help?: boolean;
  failure_reason?: string | null;
  help_text?: string;
  pinned: boolean;
  pinned_at?: number | null;
  archived: boolean;
  folder_id?: string | null;
  order: number;
  updated: number;
  updated_at?: number;
}

export function hasTargetDispatchMismatch(run?: Pick<RunSummary, "dispatch_diagnostics"> | null): boolean {
  return Boolean(run?.dispatch_diagnostics?.some((code) =>
    code === "target_challenge_mismatch" || code === "target_binding_mismatch"
  ));
}

/** An operator-created rail folder (sessions/_folders.json). */
export interface Folder {
  id: string;
  name: string;
  order: number;
}

/**
 * Subscribe to a run's SSE event stream and fold it into DeckState. Reconnects
 * with Last-Event-ID (the browser EventSource sets this automatically on
 * reconnect, and our backend honors it). The conversation-first deck swaps
 * `runId` when the operator opens a new solve 鈥攖he stream re-subscribes and the
 * deck resets. Returns the live deck + controls.
 */
export function useRun(runId: string) {
  const [deck, setDeck] = useState<DeckState>(() => emptyDeck(runId));
  const [connected, setConnected] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    esRef.current?.close();
    esRef.current = null;
    setDeck(emptyDeck(runId));
    setConnected(false);
    // runId is briefly "" on first mount (the page mints the real draft id in a
    // post-hydration effect to avoid an SSR/client random-id mismatch). No id 鈫?
    // no stream to open; the next runId change re-runs this.
    //
    // Draft ids are local UI placeholders. Opening an EventSource for them creates
    // empty backend runs and long-lived idle SSE sockets; enough refreshes/tabs can
    // exhaust the browser's per-origin connection pool and starve real run streams.
    if (!runId || isDraftRunId(runId)) return;

    // every EventType is a named SSE event; one generic handler folds them all
    const handler = (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data) as DSwarmEvent;
        setDeck((prev) => reduce(prev, ev));
      } catch {
        /* ignore malformed frame */
      }
    };

    // EventSource can't send an Authorization header, so when auth is on we mint
    // a one-time ticket first and pass it as ?ticket=. authTicket() returns ""
    // when auth is disabled (or on failure) 鈥攖hen we open the stream plainly,
    // exactly as before. `cancelled` guards the await: if runId changes (or the
    // component unmounts) before the ticket resolves, we must not open a now-
    // orphaned EventSource.
    let cancelled = false;
    (async () => {
      const ticket = await authTicket();
      if (cancelled) return;
      const qs = ticket ? `?ticket=${encodeURIComponent(ticket)}` : "";
      const es = new EventSource(`${API}/api/runs/${runId}/events${qs}`);
      esRef.current = es;
      es.onopen = () => setConnected(true);
      es.onerror = () => setConnected(false);
      // listen to all known event names plus the default. Derived directly from
      // the EventType enum (single source of truth) 鈥攁 hand-copied list silently
      // dropped any newly-added SSE event whose name was forgotten.
      Object.values(EventType).forEach((name) => es.addEventListener(name, handler as EventListener));
      es.onmessage = handler;
    })();

    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [runId]);

  const start = useCallback(
    async (body: Record<string, any>, overrideRunId?: string) => {
      // overrideRunId lets the caller dispatch to a freshly-minted id without
      // waiting for the runId state update to flush (avoids a one-render race
      // where a draft is promoted to a real run id at send time).
      const target = overrideRunId || runId;
      const res = await apiFetch(`/api/runs/${target}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail = "";
        try {
          const body = await res.json();
          detail = body?.detail ? String(body.detail) : "";
        } catch {
          try {
            detail = await res.text();
          } catch {
            detail = "";
          }
        }
        throw new Error(detail || `start failed (${res.status})`);
      }
      return res.json().catch(() => ({}));
    },
    [runId]
  );

  const sendHitl = useCallback(
    async (
      target: string,
      action: string,
      text: string,
      opts?: { preemption?: string; recipients?: string[]; clientRequestId?: string },
    ): Promise<boolean> => {
      // A command is always scoped to this Run.  Keep the legacy `target`
      // spelling for older servers, while new servers receive an explicit
      // recipient list and idempotency key.
      const normalizedRecipients = (opts?.recipients?.length
        ? opts.recipients
        : target.split(",").map((item) => item.trim()).filter(Boolean))
        .map((item) => item === "global" ? "all" : item);
      const body: Record<string, unknown> = {
        run_id: runId,
        target: target || "global",
        recipients: normalizedRecipients.length ? normalizedRecipients : ["all"],
        action,
        text,
        client_request_id: opts?.clientRequestId
          || (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
            ? crypto.randomUUID() : `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`),
      };
      if (opts?.preemption) body.preempt_policy = opts.preemption;
      const m = text.match(/https?:\/\/[^\s"'<>]+/);
      if ((action === "redirect" || action === "directive") && m) body.url = m[0].replace(/[.,;)]+$/, "");
      const sm = text.match(/^\s*(standing|常驻|standing guidance)\s*[:：]\s*(.*)$/i);
      if (sm) { body.standing = true; body.text = sm[2]; }
      else if (action === "hint" && /\b(ssh|vps|reverse[- ]?shell|root@|port[- ]?forward|credential|password)\b/i.test(text)) {
        body.standing = true;
      }
      if (action === "mark_false" && text.trim()) body.flag = text.trim();
      try {
        const response = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/hitl`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        return response.ok;
      } catch {
        return false;
      }
    },
    [runId]
  );

  // "缂佈呯敾閸嬫岸顣?: relaunch the FULL swarm on a finished run (reuses its workspace so
  // verified facts carry over). Optional `text` folds an operator hint into the
  // re-solve's challenge description.
  const resolve = useCallback(
    async (text?: string) => {
      const body: Record<string, unknown> = {};
      if (text && text.trim()) body.challenge = { description: text.trim() };
      const res = await apiFetch(`/api/runs/${runId}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await res.json().catch(() => ({} as any));
      if (!res.ok || result?.ok === false) {
        const detail = result?.detail ? String(result.detail) : "resolve failed";
        throw new Error(`${detail}${res.status ? ` (${res.status})` : ""}`);
      }
      return result as { ok: boolean; queued?: boolean; position?: number | null };
    },
    [runId]
  );

  return { deck, connected, start, sendHitl, resolve };
}

/**
 * Poll the run list for the thread rail. Runs are cheap summaries (no event
 * replay). `bump` forces an immediate refetch (e.g. right after a dispatch).
 */
export function useRunList(pollMs = 4000, bump = 0) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  useEffect(() => {
    let alive = true;
    let inFlight: AbortController | null = null;
    const load = async () => {
      if (inFlight) return;
      const ctrl = new AbortController();
      inFlight = ctrl;
      const timeout = window.setTimeout(() => ctrl.abort(), Math.max(3000, Math.min(10000, pollMs * 2)));
      try {
        // ?archived=1 returns ALL runs (archived + active) so the rail can render
        // its Archived section 鈥攚ithout it the backend hides archived rows and
        // the section is always empty (the archive-view bug).
        const r = await apiFetch(`/api/runs?archived=1`, { signal: ctrl.signal });
        const j = await r.json();
        if (alive) setRuns(j.runs ?? []);
      } catch {
        /* offline 鈥攌eep last list */
      } finally {
        window.clearTimeout(timeout);
        if (inFlight === ctrl) inFlight = null;
      }
    };
    load();
    const id = setInterval(load, pollMs);
    return () => {
      alive = false;
      inFlight?.abort();
      inFlight = null;
      clearInterval(id);
    };
  }, [pollMs, bump]);
  return runs;
}

/** One file the backend saved into the run's uploads folder (server.py upload
 *  endpoint). `path` is the ABSOLUTE on-disk path the worker will stage. */
export interface SavedFile {
  name: string;
  path: string;
  size: number;
}

/**
 * Upload challenge files into a run's folder (sessions/{runId}/uploads/). Posts
 * multipart/form-data 鈥攄o NOT set Content-Type, the browser adds the boundary.
 * The form field name ("files") MUST match the endpoint's `files` param. Returns
 * the saved files (with absolute paths) to thread into challenge.attachments at
 * dispatch. Returns [] on any failure (the deck just shows no chips).
 */
export async function uploadFiles(
  runId: string,
  files: FileList | File[]
): Promise<SavedFile[]> {
  const fd = new FormData();
  Array.from(files).forEach((f) => fd.append("files", f));
  try {
    const r = await apiFetch(`/api/runs/${runId}/uploads`, {
      method: "POST",
      body: fd,
    });
    if (!r.ok) return [];
    const j = await r.json();
    return (j.files ?? []) as SavedFile[];
  } catch {
    return [];
  }
}

/** Mint a fresh run id for "+ New solve". Falls back to a local id if offline. */
export async function newRun(): Promise<string> {
  try {
    const r = await apiFetch(`/api/runs`, { method: "POST" });
    const j = await r.json();
    if (j.run_id) return j.run_id as string;
  } catch {
    /* offline */
  }
  return `run-${Date.now().toString(36)}`;
}

export interface StartupTestEvent {
  seq: number;
  ts: number;
  test_id: string;
  type: "test.started" | "worker.phase" | "worker.event" | "provider.error" | "provider.batch_alert" | "flow.check" | "test.done";
  mode?: "startup" | "full_flow" | string;
  benchmark?: string;
  worker_count?: number;
  worker_id?: string;
  phase?: string;
  detail?: string;
  ok?: boolean | null;
  status?: string;
  layer?: string;
  blocker?: string;
  backend?: string;
  model?: string;
  account_id?: string;
  binding_kind?: string;
  effective_credential_id?: string;
  category?: string;
  severity?: string;
  retryable?: boolean;
  should_pause_dispatch?: boolean;
  user_message?: string;
  suggested_action?: string;
  raw_message?: string;
  provider?: string;
  affected_workers?: number;
  active_workers?: number;
  count?: number;
  check_id?: string;
  event_type?: string;
  payload?: Record<string, unknown>;
  summary?: {
    ok: boolean;
    passed: number;
    failed: number;
    mode?: string;
    benchmark?: string;
    checks?: Array<{ id: string; ok: boolean; detail: string }>;
    results: Array<{
      worker_id: string;
      ok: boolean;
      phase: string;
      detail: string;
      status?: string;
      layer?: string;
      blocker?: string;
      backend?: string;
      model?: string;
      account_id?: string;
      binding_kind?: string;
      effective_credential_id?: string;
    }>;
  };
}

export async function openStartupTestEvents(testId: string): Promise<EventSource> {
  const ticket = await authTicket();
  const qs = ticket ? `?ticket=${encodeURIComponent(ticket)}` : "";
  return new EventSource(`${API}/api/startup-test/${encodeURIComponent(testId)}/events${qs}`);
}

export interface StartupTestOptions {
  mode?: "startup" | "full_flow";
  benchmark?: string;
}

export async function startStartupTest(options: StartupTestOptions = {}): Promise<string> {
  const hasBody = Boolean(options.mode || options.benchmark);
  const r = await apiFetch(`/api/startup-test`, {
    method: "POST",
    ...(hasBody ? {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: options.mode || "startup",
        benchmark: options.benchmark || "local-smoke",
      }),
    } : {}),
  });
  if (!r.ok) throw new Error(`startup test failed: ${r.status}`);
  const j = await r.json().catch(() => ({} as { test_id?: string }));
  if (!j.test_id) throw new Error("startup test did not return an id");
  return j.test_id;
}

/** Operator rail mutations 鈥攑in/unpin, archive/unarchive, rename, move to a
 *  folder (folder_id=null 鈫抰op-level), drag-order. */
export async function patchRun(
  runId: string,
  patch: { pinned?: boolean; archived?: boolean; name?: string; folder_id?: string | null; order?: number }
): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/runs/${runId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    return r.ok;
  } catch {
    return false;
  }
}

/** Hard-delete a Run (irreversible — the caller confirms first). */
export async function deleteRun(runId: string): Promise<boolean> {
  try {
    const response = await apiFetch(`/api/runs/${runId}`, { method: "DELETE" });
    const payload = await response.json().catch(() => ({} as { ok?: boolean }));
    return response.ok && payload.ok === true;
  } catch {
    return false;
  }
}

/** Re-apply the rail naming rule ({category}-{url|attachment|棰樼洰}) to an
 *  EXISTING run from its remembered dispatch. Deterministic rule hits (web鈫扷RL
 *  host, rev/crypto/forensics/misc鈫抋ttachment) return the new name immediately;
 *  anything else (pwn 鈫?棰樼洰鍚? is named by the background titler LLM and comes
 *  back pending 鈥攖he rail poll picks the rename up when it lands. */
export async function retitleRun(
  runId: string,
): Promise<{ ok: boolean; name: string | null; pending: boolean; detail?: string }> {
  try {
    const r = await apiFetch(`/api/runs/${runId}/retitle`, { method: "POST" });
    const j = await r.json().catch(() => ({} as any));
    if (!r.ok) return { ok: false, name: null, pending: false, detail: String(j?.detail ?? "") };
    return { ok: Boolean(j?.ok), name: j?.name ?? null, pending: Boolean(j?.pending) };
  } catch (e) {
    return { ok: false, name: null, pending: false, detail: String(e) };
  }
}

// 鈹€鈹€ engine status 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

/** Deep per-engine self-check result (FE-healthcheck-page). */
export interface EngineHealth {
  engine: string;
  bin: string;
  version: string;
  healthy: boolean;
  detail: string;
  backend?: string;
  /** Where the bin path came from: "env" (pinned via DSWARM_<E>_BIN), "known-good",
   *  "path" (auto-discovered on PATH 鈥攎ay be the wrong version), or "fallback". */
  bin_source?: "env" | "known-good" | "path" | "fallback";
  /** The env var that pins this engine's bin (e.g. DSWARM_PI_BIN). */
  bin_env?: string;
}

/** Run the DEEP self-check (slow 鈥攅xercises auth). `backend` picks local (host
 *  CLI + auth) vs container (docker run --rm: image + CLI launchable inside the
 *  worker image). On-demand, not polled. */
export async function getEngineHealth(backend: "local" | "container" = "local"): Promise<EngineHealth[]> {
  try {
    const r = await apiFetch(`/api/engines/health?backend=${backend}`);
    const j = await r.json();
    return (j.engines ?? []) as EngineHealth[];
  } catch {
    return [];
  }
}


// 鈹€鈹€ rail folders (FE-session-folder) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

export async function listFolders(): Promise<Folder[]> {
  try {
    const r = await apiFetch(`/api/folders`);
    const j = await r.json();
    return (j.folders ?? []) as Folder[];
  } catch {
    return [];
  }
}

export async function createFolder(name: string): Promise<Folder | null> {
  try {
    const r = await apiFetch(`/api/folders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const j = await r.json();
    return (j.folder ?? null) as Folder | null;
  } catch {
    return null;
  }
}

export async function renameFolder(id: string, name: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/folders/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const j = await r.json().catch(() => ({}));
    return !!j.ok;
  } catch {
    return false;
  }
}

export async function deleteFolder(id: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/folders/${id}`, { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    return !!j.ok;
  } catch {
    return false;
  }
}

/** Poll the folder list for the rail (cheap; bump forces an immediate refetch). */
export function useFolders(pollMs = 8000, bump = 0): Folder[] {
  const [folders, setFolders] = useState<Folder[]>([]);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const f = await listFolders();
      if (alive) setFolders(f);
    };
    load();
    const id = setInterval(load, pollMs);
    return () => { alive = false; clearInterval(id); };
  }, [pollMs, bump]);
  return folders;
}

// 鈹€鈹€ worker-roster management (BE-worker-management) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

/** The default worker roster the dispatch path falls back to. Mirrors the
 *  backend WorkerConfigStore (apps/web/worker_config.py). */
export type ReasonLlmProfile = {
  provider: string;
  provider_ref?: string;
  model: string;
  base_url?: string;
  effort?: string;
  timeout?: number;
  credential_source?: "auto" | "env" | "account" | string;
  credential_account?: string;
  wire_api?: "auto" | "openai" | "openai-chat" | "openai-responses" | "anthropic" | string;
};

export interface ReasonLlmProbe {
  ok: boolean;
  code?: string;
  detail: string;
  model: string;
  base_url?: string;
  base_url_host?: string;
  credential_source?: string;
  credential_account?: string;
  detected_wire_api?: string | null;
  layers?: {
    name: string;
    ok: boolean;
    status?: number | null;
    attempted?: boolean;
    detail?: string;
  }[];
}


export interface LLMProvider {
  id: string;
  label: string;
  kind?: string;
  base_url: string;
  wire_api?: "auto" | "openai" | "openai-chat" | "openai-responses" | string;
  auth_mode?: "bearer" | "x-api-key" | "custom" | string;
  auth_header?: string;
  auth_prefix?: string;
  models?: string[];
  default_model?: string;
  notes?: string;
}

export interface LLMProviderTemplate extends LLMProvider {}

export interface LLMProviderSecretMeta {
  provider_id: string;
  present: boolean;
  updated_at?: number | null;
  details?: Record<string, unknown>;
}

export interface ProviderSecretUpdate {
  provider_id: string;
  action: "replace" | "remove";
  value?: string;
}

export interface WorkerSettings {
  engines: string[];
  max_workers: number;
  worker_backend: "local" | "container";
  wall_clock_budget: number;
  max_total_workers: number;
  cost_budget_usd: number;
  review_policy: {
    enabled?: boolean;
    engine?: string;
    provider_ref?: string;
    credential_source?: string;
    credential_account?: string;
    base_url?: string;
    wire_api?: string;
    model?: string;
    timeout?: number;
    after_fruitless_workers?: number;
    after_duplicate_intents?: number;
    on_course_correct?: boolean;
    on_reason_dry?: boolean;
    on_candidate_spike?: boolean;
    on_operator_hint?: boolean;
    allow_review_fallback?: boolean;
    every_completed_workers?: number;
    candidate_spike_threshold?: number;
    max_concurrent?: number;
    cooldown_events?: number;
    max_review_workers?: number;
  };
  llm_providers: LLMProvider[];
  llm_profiles: {
    planner: ReasonLlmProfile;
    /** Product-facing alias; backend mirrors this into planner for compatibility. */
    reason?: ReasonLlmProfile;
    worker?: ReasonLlmProfile;
    review?: ReasonLlmProfile;
    observer?: ReasonLlmProfile;
    observer_fallback?: ReasonLlmProfile;
    titler: ReasonLlmProfile;
  };
  runtime_profiles: {
    id: string;
    backend: "local" | "container";
    label: string;
    network?: string;
    memory?: string;
    cpus?: string;
    pids_limit?: number;
  }[];
  worker_profiles: {
    id: string;
    name?: string;
    label?: string;
    engine: string;
    transport: string;
    auth: string;
    credential_mode?: string;
    credential_account: string;
    api_key_ref?: string;
    provider_ref?: string;
    base_url?: string;
    wire_api?: "auto" | "openai-chat" | "openai-responses" | string;
    auth_mode?: "bearer" | "x-api-key" | "custom" | string;
    auth_header?: string;
    auth_prefix?: string;
    runtime: string;
    roles: string[];
    max_running: number;
    max_review_running?: number;
    priority: number;
    model?: string;
    effort?: string;
    image?: string;
    enabled: boolean;
  }[];
  overrides: Record<string, { engines: string[] }>;
}

export interface CredentialAccount {
  account_id: string;
  engine: string;
  mode: string;
  present: boolean;
  writable_state: boolean;
  updated_at?: number | null;
  details: Record<string, unknown>;
}

export interface WorkerModelOptions {
  allow_custom: boolean;
  models: Record<string, { id: string; label: string }[]>;
}

export interface WorkerEndpointProbe {
  ok: boolean;
  detail: string;
  error_layer?: string;
  base_url?: string;
  models: { id: string; name: string }[];
  detected_wire_api?: "openai-chat" | "openai-responses" | null;
  connectivity?: { ok: boolean; status: number | null };
  authentication?: { ok: boolean; status: number | null };
  model_discovery?: { ok: boolean; status?: number; items: { id: string; name: string }[] };
  model_probe?: { attempted: boolean; ok: boolean; status?: number; model?: string; protocol?: string };
}

export interface WorkerSettingsIssue {
  path: string;
  severity: "error" | "warning";
  code: string;
  message: string;
}

export interface WorkerSettingsChange {
  scope: "worker" | "runtime" | "reason" | "secret" | string;
  id: string;
  fields: string[];
}

export interface WorkerSecretUpdate {
  account_id: string;
  action: "replace" | "remove";
  value?: string;
  base_url?: string;
}

export interface WorkerSettingsWorkspace {
  config: WorkerSettings;
  revision: string;
  accounts: CredentialAccount[];
  provider_templates?: LLMProviderTemplate[];
  provider_secrets?: LLMProviderSecretMeta[];
  issues?: WorkerSettingsIssue[];
  changes?: WorkerSettingsChange[];
}

export interface WorkerSettingsValidation {
  ok: boolean;
  issues: WorkerSettingsIssue[];
  changes: WorkerSettingsChange[];
}

export async function getWorkerSettings(): Promise<WorkerSettings | null> {
  try {
    const r = await apiFetch(`/api/settings/workers`);
    if (!r.ok) return null;
    const j = await r.json();
    return (j.config ?? null) as WorkerSettings | null;
  } catch {
    return null;
  }
}

export async function getWorkerSettingsWorkspace(): Promise<WorkerSettingsWorkspace | null> {
  try {
    const r = await apiFetch(`/api/settings/workers`);
    if (!r.ok) return null;
    const j = await r.json();
    if (!j?.config || typeof j?.revision !== "string") return null;
    return {
      config: j.config as WorkerSettings,
      revision: j.revision,
      accounts: (j.accounts ?? []) as CredentialAccount[],
      provider_templates: (j.provider_templates ?? []) as LLMProviderTemplate[],
      provider_secrets: (j.provider_secrets ?? []) as LLMProviderSecretMeta[],
      issues: (j.issues ?? []) as WorkerSettingsIssue[],
      changes: (j.changes ?? []) as WorkerSettingsChange[],
    };
  } catch {
    return null;
  }
}

export async function validateWorkerSettingsDraft(
  draft: WorkerSettings,
  secretUpdates: WorkerSecretUpdate[],
  providerSecretUpdates: ProviderSecretUpdate[] = []
): Promise<WorkerSettingsValidation> {
  try {
    const r = await apiFetch(`/api/settings/workers/validate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft, secret_updates: secretUpdates, provider_secret_updates: providerSecretUpdates }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      return {
        ok: false,
        issues: [{ path: "draft", severity: "error", code: "request_failed", message: String(j?.detail ?? "Validation failed.") }],
        changes: [],
      };
    }
    return j as WorkerSettingsValidation;
  } catch (error) {
    return {
      ok: false,
      issues: [{ path: "draft", severity: "error", code: "network_error", message: String(error) }],
      changes: [],
    };
  }
}

export async function applyWorkerSettingsDraft(
  baseRevision: string,
  draft: WorkerSettings,
  secretUpdates: WorkerSecretUpdate[],
  providerSecretUpdates: ProviderSecretUpdate[] = []
): Promise<{ ok: boolean; workspace?: WorkerSettingsWorkspace; conflict?: boolean; detail?: string }> {
  try {
    const r = await apiFetch(`/api/settings/workers/apply`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_revision: baseRevision, draft, secret_updates: secretUpdates, provider_secret_updates: providerSecretUpdates }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      return { ok: false, conflict: r.status === 409, detail: String(j?.detail ?? "Apply failed.") };
    }
    return {
      ok: true,
      workspace: {
        config: j.config as WorkerSettings,
        revision: String(j.revision ?? ""),
        accounts: (j.accounts ?? []) as CredentialAccount[],
        provider_templates: (j.provider_templates ?? []) as LLMProviderTemplate[],
        provider_secrets: (j.provider_secrets ?? []) as LLMProviderSecretMeta[],
        issues: (j.issues ?? []) as WorkerSettingsIssue[],
        changes: (j.changes ?? []) as WorkerSettingsChange[],
      },
    };
  } catch (error) {
    return { ok: false, detail: String(error) };
  }
}

export async function probeWorkerEndpoint(
  profile: WorkerSettings["worker_profiles"][number],
  apiKey: string,
  validateModel = false,
): Promise<WorkerEndpointProbe> {
  try {
    const r = await apiFetch(`/api/settings/workers/probe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, api_key: apiKey, validate_model: validateModel }),
      signal: AbortSignal.timeout(60_000),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "Connection test failed."), models: [] };
    return {
      ok: Boolean(j?.ok),
      detail: String(j?.detail ?? ""),
      error_layer: j?.error_layer ? String(j.error_layer) : undefined,
      base_url: j?.base_url ? String(j.base_url) : undefined,
      models: Array.isArray(j?.models) ? j.models : [],
      detected_wire_api: j?.detected_wire_api ?? null,
      connectivity: j?.connectivity,
      authentication: j?.authentication,
      model_discovery: j?.model_discovery,
      model_probe: j?.model_probe,
    };
  } catch (error) {
    return { ok: false, detail: `Connection test failed: ${String(error)}`, models: [] };
  }
}

export async function getWorkerModelOptions(): Promise<WorkerModelOptions> {
  try {
    const r = await apiFetch(`/api/settings/worker-models`);
    if (!r.ok) return { allow_custom: true, models: {} };
    const j = await r.json();
    return {
      allow_custom: Boolean(j.allow_custom ?? true),
      models: (j.models ?? {}) as WorkerModelOptions["models"],
    };
  } catch {
    return { allow_custom: true, models: {} };
  }
}

// 鈹€鈹€ P2-v3: worker image health (daemon / pulled / version) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
export type WorkerImageStatus = {
  image: string;
  daemon: { ok: boolean; detail: string };
  pulled: { ok: boolean; detail: string };
  version: { status: "match" | "mismatch" | "unknown"; expected: string | null; actual: string | null; detail: string };
  overall: "green" | "yellow" | "red";
};

export async function getWorkerImageStatus(): Promise<WorkerImageStatus | null> {
  try {
    const r = await apiFetch(`/api/settings/worker-image`);
    if (!r.ok) return null;
    return (await r.json()) as WorkerImageStatus;
  } catch {
    return null;
  }
}

export async function pullWorkerImage(): Promise<{ ok: boolean; detail: string; version?: string | null }> {
  try {
    const r = await apiFetch(`/api/settings/worker-image/pull`, { method: "POST" });
    const j = await r.json().catch(() => ({}));
    return { ok: Boolean(j?.ok), detail: String(j?.detail ?? (r.ok ? "" : "pull failed")), version: j?.version ?? null };
  } catch (e) {
    return { ok: false, detail: String(e) };
  }
}

export async function testWorkerProfileModel(
  profile: WorkerSettings["worker_profiles"][number],
  model: string,
  backend: "local" | "container"
): Promise<{ ok: boolean; detail: string; model: string; engine: string }> {
  try {
    const r = await apiFetch(`/api/settings/worker-model/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, model, backend }),
    });
    const j = await r.json().catch(() => ({}));
    return {
      ok: !!j.ok,
      detail: String(j.detail ?? ""),
      model: String(j.model ?? model),
      engine: String(j.engine ?? profile.engine),
    };
  } catch (e) {
    return { ok: false, detail: String(e), model, engine: profile.engine };
  }
}

// 鈹€鈹€ per-profile health (single source of truth shared with the dispatch precheck) 鈹€鈹€
export type ProfileHealth = {
  profile_id: string;
  engine: string;
  backend: string;
  status: "ok" | "blocked" | "auth_failed" | "disabled";
  layer: string | null;
  blocker: string | null;
  detail: string;
  model: string;
  account_id: string;
  // SINGLE SOURCE OF TRUTH for "bound?" 鈥攔ead these instead of the literal
  // credential_account field (which caused the "閺堫亞绮︾€?vs 瀹歌尙绮︾€? contradiction).
  // explicit = profile named the account; inherited = empty 鈫抐ell back to the
  // default/host login (show "閼奉亜濮? <id>", NOT "閺堫亞绮︾€?); missing = no credential.
  binding_kind?: "explicit" | "inherited" | "missing";
  effective_credential_id?: string;
};

/** Batch readiness for every profile at the CHEAP binding depth (zero network /
 *  zero docker) 鈥攄rives the settings badge + account rows. Backend is resolved
 *  server-side (same per-profile runtime鈫抌ackend mapping dispatch uses). */
export async function fetchProfilesHealth(): Promise<ProfileHealth[]> {
  try {
    const r = await apiFetch(`/api/settings/profiles/health`);
    if (!r.ok) return [];
    const j = await r.json();
    return (j.profiles ?? []) as ProfileHealth[];
  } catch {
    return [];
  }
}

/** DEEP probe for one profile ("娴嬭瘯杩為€?: binding + (container) plumbing + a real
 *  auth hello with the profile's pinned model. A green here matches the dispatch
 *  precheck, so the run won't die on profile_unhealthy. */
export async function testProfileHealth(profileId: string): Promise<ProfileHealth | null> {
  try {
    // A container deep-probe (docker run + real one-turn hello) can take ~60-120s
    // on a cold container start; cap it so "娴嬭瘯涓€?can't hang forever (no
    // client timeout was the reason the button spun indefinitely on a slow probe).
    const r = await apiFetch(`/api/settings/profiles/${encodeURIComponent(profileId)}/health`, {
      method: "POST",
      signal: AbortSignal.timeout(180_000),
    });
    if (!r.ok) return null;
    return (await r.json()) as ProfileHealth;
  } catch {
    return null;
  }
}

export async function listCredentialAccounts(): Promise<CredentialAccount[]> {
  try {
    const r = await apiFetch(`/api/settings/credential-accounts`);
    if (!r.ok) return [];
    const j = await r.json();
    return (j.accounts ?? []) as CredentialAccount[];
  } catch {
    return [];
  }
}

export async function putCredentialAccount(
  accountId: string,
  body: { engine: string; secret?: string; base_url?: string; target_engine?: string }
): Promise<{ ok: boolean; account?: CredentialAccount; detail?: string }> {
  try {
    const r = await apiFetch(`/api/settings/credential-accounts/${encodeURIComponent(accountId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "Save failed.") };
    return { ok: true, account: (j.account ?? null) as CredentialAccount | undefined };
  } catch {
    return { ok: false, detail: "save failed" };
  }
}

export async function deleteCredentialAccount(accountId: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/settings/credential-accounts/${encodeURIComponent(accountId)}`, {
      method: "DELETE",
    });
    if (!r.ok) return false;
    const j = await r.json();
    return Boolean(j.ok);
  } catch {
    return false;
  }
}

export type SystemLoginStatus = "present" | "absent" | "unknown";

/** Host-side login presence per engine (drives the local-mode credentials UI). */
export async function getSystemLogin(): Promise<Record<string, SystemLoginStatus>> {
  try {
    const r = await apiFetch(`/api/settings/system-login`);
    if (!r.ok) return {};
    const j = await r.json();
    return (j.logins ?? {}) as Record<string, SystemLoginStatus>;
  } catch {
    return {};
  }
}

/** Test the planner/titler endpoint the operator is editing (key from env/account store). */

export async function probeLlmProvider(
  provider: LLMProvider,
  apiKey: string,
  validateModel = false,
  model = provider.default_model || "",
): Promise<WorkerEndpointProbe> {
  try {
    const r = await apiFetch(`/api/settings/llm-providers/probe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, api_key: apiKey, model, validate_model: validateModel }),
      signal: AbortSignal.timeout(60_000),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "Connection test failed."), models: [] };
    return {
      ok: Boolean(j?.ok),
      detail: String(j?.detail ?? ""),
      error_layer: j?.error_layer ? String(j.error_layer) : undefined,
      base_url: j?.base_url ? String(j.base_url) : undefined,
      models: Array.isArray(j?.models) ? j.models : [],
      detected_wire_api: j?.detected_wire_api ?? null,
      connectivity: j?.connectivity,
      authentication: j?.authentication,
      model_discovery: j?.model_discovery,
      model_probe: j?.model_probe,
    };
  } catch (error) {
    return { ok: false, detail: `Connection test failed: ${String(error)}`, models: [] };
  }
}

export async function testLlmEndpoint(
  which: "planner" | "review" | "observer" | "observer_fallback" | "titler",
  profile: ReasonLlmProfile
): Promise<ReasonLlmProbe> {
  const model = String(profile.model || "");
  try {
    const r = await apiFetch(`/api/settings/llm/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        which,
        base_url: profile.base_url,
        model,
        provider_ref: profile.provider_ref || "",
        credential_source: profile.credential_source || "auto",
        credential_account: profile.credential_account || "pi-main",
        wire_api: profile.wire_api || "auto",
      }),
      signal: AbortSignal.timeout(120_000),
    });
    const j = await r.json().catch(() => ({}));
    return {
      ok: !!j.ok,
      code: j.code ? String(j.code) : undefined,
      detail: String(j.detail ?? ""),
      model: String(j.model ?? model),
      base_url: j.base_url ? String(j.base_url) : undefined,
      base_url_host: j.base_url_host ? String(j.base_url_host) : undefined,
      credential_source: j.credential_source ? String(j.credential_source) : undefined,
      credential_account: j.credential_account ? String(j.credential_account) : undefined,
      detected_wire_api: j.detected_wire_api ?? null,
      layers: Array.isArray(j.layers) ? j.layers : [],
    };
  } catch (e) {
    const msg = (e as Error)?.name === "TimeoutError" ? "Test timed out (120s)" : String(e);
    return { ok: false, code: "client_error", detail: msg, model, layers: [] };
  }
}

/** Test a registered credential account. local 鈫抙ost probe with the account's
 *  env; container 鈫抮eal `docker run --rm` plumbing test. Never host-fallback. */
export async function testCredentialAccount(
  accountId: string,
  engine: string,
  backend: "local" | "container"
): Promise<{ ok: boolean; detail: string; layer?: string }> {
  try {
    const r = await apiFetch(
      `/api/settings/credential-accounts/${encodeURIComponent(accountId)}/test`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engine, backend }),
        // cap the wait 鈥攁 container probe + cold hello can be slow, but the
        // button must never spin forever (the "娴嬭瘯涓€?hang).
        signal: AbortSignal.timeout(180_000),
      }
    );
    const j = await r.json().catch(() => ({}));
    return { ok: !!j.ok, detail: String(j.detail ?? ""), layer: j.layer };
  } catch (e) {
    const msg = (e as Error)?.name === "TimeoutError" ? "Test timed out (180s); container probe or cold start may be slow. Retry or check engine status." : String(e);
    return { ok: false, detail: msg };
  }
}

/** Unify backend + runtime across all enabled profiles (one-container-per-run). */
export async function putRuntimeEnvironment(
  backend: "local" | "container",
  runtime_id: string
): Promise<WorkerSettings | null> {
  try {
    const r = await apiFetch(`/api/settings/runtime-environment`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend, runtime_id }),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return (j.config ?? null) as WorkerSettings | null;
  } catch {
    return null;
  }
}

/** Operator runtime control: add a worker for an engine to a LIVE run
 *  (omit engine 鈫抰he backend planner picks). */
export async function spawnWorker(runId: string, engine?: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/runs/${runId}/workers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(engine ? { engine } : {}),
    });
    const j = await r.json().catch(() => ({}));
    return !!j.ok;
  } catch {
    return false;
  }
}

/** Operator runtime control: stop a specific worker by its solver_id. */
export async function killWorker(runId: string, solverId: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/runs/${runId}/workers`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ solver_id: solverId }),
    });
    const j = await r.json().catch(() => ({}));
    return !!j.ok;
  } catch {
    return false;
  }
}

/** Run-id-parametrized HITL post for fleet controls. */
export async function sendRunHitl(
  runId: string,
  action: string,
  text = "",
  target = "global",
  recipients?: string[],
): Promise<boolean> {
  try {
    const normalizedRecipients = (recipients?.length
      ? recipients
      : target.split(",").map((item) => item.trim()).filter(Boolean))
      .map((item) => item === "global" ? "all" : item);
    const r = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/hitl`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: runId,
        target: target || "global",
        recipients: normalizedRecipients.length ? normalizedRecipients : ["all"],
        action,
        text,
        client_request_id: typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID() : `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      }),
    });
    return r.ok;
  } catch {
    return false;
  }
}
