"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { DeckState, EventType, DSwarmEvent, emptyDeck, reduce } from "./events";
import { readKey, writeKey, removeKey } from "./storage";

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
  // in_container (P2-v3): the control plane runs in a container →the deck must
  // force container mode and disable the "local" worker-isolation toggle.
  return { authenticated: true, authRequired: Boolean(data?.auth_required), inContainer: Boolean(data?.in_container) };
}

/**
 * Mint a one-time ticket for opening an SSE/WS connection (no header possible).
 * Returns "" when auth is disabled or the mint fails —callers append it as a
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

export type RunStatus = "draft" | "queued" | "running" | "paused" | "solved" | "finished" | "failed" | "cancelled";

export const isDraftRunId = (id: string) => id.startsWith("draft-");

/** One run as the thread rail lists it (matches RunManager.Run.summary()). */
export interface RunSummary {
  run_id: string;
  name: string;
  category: string;
  started: boolean;
  finished: boolean;
  solved: boolean;
  paused: boolean;
  status: RunStatus;
  queued?: boolean;
  queue_position?: number | null;
  cancelled?: boolean;
  flag?: string | null;
  // multi-flag progress (backend summary already sends these; the Run Fleet
  // rows render "flags x/y" from them).
  flags?: string[];
  expected_flags?: number;
  multi_flag?: boolean;
  // HITL attention signal: a worker is waiting on an operator answer. Drives the
  // Fleet "Needs Attention" filter + badge (docs/07 §5.2).
  awaiting_help?: boolean;
  help_text?: string;
  pinned: boolean;
  pinned_at?: number | null;
  archived: boolean;
  folder_id?: string | null;
  order: number;
  updated: number;
  updated_at?: number;
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
 * `runId` when the operator opens a new solve —the stream re-subscribes and the
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
    // post-hydration effect to avoid an SSR/client random-id mismatch). No id →
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
    // when auth is disabled (or on failure) —then we open the stream plainly,
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
      // the EventType enum (single source of truth) —a hand-copied list silently
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
    async (target: string, action: string, text: string, opts?: { preemption?: string }) => {
      // A redirect can carry a NEW target URL ("the challenge moved here") —pull
      // the first URL out of the text and send it as `url` so the worker retargets
      // its next turn. A message prefixed with "standing:" (or 常驻:) is persistent
      // background guidance (VPS/SSH creds) injected into every future worker.
      const body: Record<string, unknown> = { target, action, text };
      // B: an explicit directive carries a preemption policy (how aggressively it
      // overrides in-flight work). Default soft_rebind (rebind next batch, no kill).
      if (opts?.preemption) body.preempt_policy = opts.preemption;
      if (action === "directive" && !opts?.preemption) body.preempt_policy = "soft_rebind";
      const m = text.match(/https?:\/\/[^\s"'<>]+/);
      if ((action === "redirect" || action === "directive") && m) body.url = m[0].replace(/[.,;)]+$/, "");
      // Explicit "standing:" / "常驻:" prefix →persistent guidance.
      const sm = text.match(/^\s*(standing|常驻|standing guidance)\s*[:：]\s*(.*)$/i);
      if (sm) { body.standing = true; body.text = sm[2]; }
      if (action === "mark_false" && text.trim()) {
        body.flag = text.trim();
      }
      // Auto-detect: a hint that hands over a RESOURCE (VPS / SSH / creds / a
      // reverse-shell host) is almost always meant to apply to ALL workers for the
      // rest of the run, not just the one turn —mark it standing so late-spawned
      // workers inherit it too (operators kept forgetting the "standing:" prefix and
      // the VPS hint never reached new workers). Heuristic, conservative: only fires
      // on clear resource-handover signals.
      else if (action === "hint" &&
               /\b(ssh|vps|鍙嶅脊|reverse[- ]?shell|root@|绔彛杞彂|port[- ]?forward|credential|鍑瘉|璐﹀彿|瀵嗙爜|password|璺虫澘|涓浆)\b/i.test(text)) {
        body.standing = true;
      }
      await apiFetch(`/api/runs/${runId}/hitl`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    },
    [runId]
  );

  // "缁х画鍋氶": relaunch the FULL swarm on a finished run (reuses its workspace so
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
        // its Archived section —without it the backend hides archived rows and
        // the section is always empty (the archive-view bug).
        const r = await apiFetch(`/api/runs?archived=1`, { signal: ctrl.signal });
        const j = await r.json();
        if (alive) setRuns(j.runs ?? []);
      } catch {
        /* offline —keep last list */
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
 * multipart/form-data —do NOT set Content-Type, the browser adds the boundary.
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

/** Operator rail mutations —pin/unpin, archive/unarchive, rename, move to a
 *  folder (folder_id=null →top-level), drag-order. */
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

/** Hard-delete a run (irreversible —the caller confirms first). */
export async function deleteRun(runId: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/runs/${runId}`, { method: "DELETE" });
    return r.ok;
  } catch {
    return false;
  }
}

// ── engine status ────────────────────────────────────────────────────────────

/** Deep per-engine self-check result (FE-healthcheck-page). */
export interface EngineHealth {
  engine: string;
  bin: string;
  version: string;
  healthy: boolean;
  detail: string;
  backend?: string;
  /** Where the bin path came from: "env" (pinned via DSWARM_<E>_BIN), "known-good",
   *  "path" (auto-discovered on PATH —may be the wrong version), or "fallback". */
  bin_source?: "env" | "known-good" | "path" | "fallback";
  /** The env var that pins this engine's bin (e.g. DSWARM_PI_BIN). */
  bin_env?: string;
}

/** Run the DEEP self-check (slow —exercises auth). `backend` picks local (host
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


// ── rail folders (FE-session-folder) ────────────────────────────────────────

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

// ── worker-roster management (BE-worker-management) ─────────────────────────

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
  wire_api?: "auto" | "openai" | "openai-chat" | "openai-responses" | string;
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
  start_workers: number;
  max_workers: number;
  worker_backend: "local" | "container";
  wall_clock_budget: number;
  max_total_workers: number;
  cost_budget_usd: number;
  stage_policy: {
    prepare: Record<string, unknown>;
    coordinator?: {
      wall_clock_budget: number;
      review?: {
        enabled?: boolean;
        engine?: string;
        timeout?: number;
        after_race?: boolean;
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
    };
    reason?: Record<string, unknown>;
    budgets: { max_total_workers: number; cost_budget_usd: number };
  };
  llm_providers: LLMProvider[];
  llm_profiles: {
    planner: ReasonLlmProfile;
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
    race: boolean;
    max_running: number;
    max_review_running?: number;
    priority: number;
    model?: string;
    effort?: string;
    image?: string;
    enabled: boolean;
  }[];
  overrides: Record<string, { engines: string[]; start_workers: number }>;
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
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "连接测试失败。"), models: [] };
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
    return { ok: false, detail: `连接测试失败：${String(error)}`, models: [] };
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

// ── P2-v3: worker image health (daemon / pulled / version) ──────────────────
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

// ── per-profile health (single source of truth shared with the dispatch precheck) ──
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
  // SINGLE SOURCE OF TRUTH for "bound?" —read these instead of the literal
  // credential_account field (which caused the "鏈粦瀹?vs 宸茬粦瀹? contradiction).
  // explicit = profile named the account; inherited = empty →fell back to the
  // default/host login (show "鑷姩: <id>", NOT "鏈粦瀹?); missing = no credential.
  binding_kind?: "explicit" | "inherited" | "missing";
  effective_credential_id?: string;
};

/** Batch readiness for every profile at the CHEAP binding depth (zero network /
 *  zero docker) —drives the settings badge + account rows. Backend is resolved
 *  server-side (same per-profile runtime→backend mapping dispatch uses). */
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

/** DEEP probe for one profile ("测试连通): binding + (container) plumbing + a real
 *  auth hello with the profile's pinned model. A green here matches the dispatch
 *  precheck, so the run won't die on profile_unhealthy. */
export async function testProfileHealth(profileId: string): Promise<ProfileHealth | null> {
  try {
    // A container deep-probe (docker run + real one-turn hello) can take ~60-120s
    // on a cold container start; cap it so "测试中… can't hang forever (no
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
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "save failed") };
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
    if (!r.ok) return { ok: false, detail: String(j?.detail ?? "连接测试失败。"), models: [] };
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
    return { ok: false, detail: `连接测试失败：${String(error)}`, models: [] };
  }
}

export async function testLlmEndpoint(
  which: "planner" | "titler",
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
    const msg = (e as Error)?.name === "TimeoutError" ? "测试超时（120s）" : String(e);
    return { ok: false, code: "client_error", detail: msg, model, layers: [] };
  }
}

/** Test a registered credential account. local →host probe with the account's
 *  env; container →real `docker run --rm` plumbing test. Never host-fallback. */
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
        // cap the wait —a container probe + cold hello can be slow, but the
        // button must never spin forever (the "测试中… hang).
        signal: AbortSignal.timeout(180_000),
      }
    );
    const j = await r.json().catch(() => ({}));
    return { ok: !!j.ok, detail: String(j.detail ?? ""), layer: j.layer };
  } catch (e) {
    const msg = (e as Error)?.name === "TimeoutError"
      ? "测试超时（180s）——容器探测或冷启动较慢，请重试或检查引擎状态"
      : String(e);
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
 *  (omit engine →the backend planner picks). */
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

/**
 * Run-id-parametrized HITL post —the batch Fleet controls (pause / resume /
 * stop across a selection) fan out through this, one call per run, reusing the
 * existing single-run API (docs/07 §5.2: no API change for batch ops).
 */
export async function sendRunHitl(
  runId: string,
  action: string,
  text = "",
  target = "global",
): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/runs/${runId}/hitl`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, action, text }),
    });
    return r.ok;
  } catch {
    return false;
  }
}
