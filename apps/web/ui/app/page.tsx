"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRun, useRunList, useFolders, useBudgetSnapshot, useRuntimePools, newRun, patchRun, deleteRun, uploadFiles, spawnWorker, killWorker, createFolder, renameFolder, deleteFolder, sendRunHitl, SavedFile } from "@/lib/useRun";
import { useT } from "@/lib/i18n";
import { GraphNode, isRunActive } from "@/lib/events";
import { I18nProvider } from "@/lib/i18n";
import { ThreadRail } from "@/components/ThreadRail";
import { StartupTestPanel } from "@/components/StartupTestPanel";
import { Conversation } from "@/components/Conversation";
import type { DispatchOpts } from "@/components/Conversation";
import { LoginGate } from "@/components/LoginGate";
import { CommandPalette } from "@/components/CommandPalette";
import { BtwPanel } from "@/components/BtwPanel";
import { ToastLane, useToasts } from "@/components/Toast";
import { detailUrlForRun, type DetailView } from "@/lib/runRoute";
import { TopBar } from "@/components/TopBar";
import { DecisionTimeline } from "@/components/DecisionTimeline";
import { SwarmInspector } from "@/components/SwarmInspector";
import { OperatorBar } from "@/components/OperatorBar";
import { deriveStage } from "@/lib/timeline";
import type { Stage } from "@/lib/normalize";
import type { BatchAction } from "@/lib/fleet";
import { clampRailWidth, RAIL_WIDTH_DEFAULT, RAIL_WIDTH_STORAGE_KEY } from "@/lib/railSizing";
import { readKey, writeKey } from "@/lib/storage";
import { useDeckMotion } from "@/lib/useDeckMotion";

/**
 * D-Swarm Command Deck — Command-center shell (docs/07 §5, Phase 4).
 *
 * The conversation is NO LONGER the spine. The layout is:
 *
 *   TopBar (brand · run/target · Stage Rail · flags · cost · connection · controls)
 *   Run Fleet (left) │ Decision Timeline (center) │ Live Swarm Inspector (right)
 *   Operator Command Bar (persistent bottom)
 *
 * The deck stays a dumb subscriber (§3): dispatch POSTs /start with the prose
 * prompt, commands POST /hitl, and everything else folds back from the run's
 * SSE stream. A not-yet-dispatched DRAFT still gets the conversational
 * welcome/composer (that surface is how a run is launched); once a run exists,
 * the Decision Timeline takes over the center.
 */

// A draft is a local, not-yet-dispatched conversation. It never hits the backend
// until the operator sends a prompt — see dispatch() / onNewSolve().
const newDraftId = () => `draft-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
const isDraft = (id: string) => id.startsWith("draft-");
type ThemeMode = "light" | "dark";
const INSPECTOR_WIDTH_MIN = 280;
const INSPECTOR_WIDTH_MAX = 640;
const INSPECTOR_WIDTH_DEFAULT = 340;
const INSPECTOR_WIDTH_STORAGE_KEY = "dswarm.inspector.width";

function inspectorWidthMax(viewportWidth?: number): number {
  if (!viewportWidth || viewportWidth <= 0) return INSPECTOR_WIDTH_MAX;
  return Math.max(INSPECTOR_WIDTH_MIN, Math.min(INSPECTOR_WIDTH_MAX, Math.round(viewportWidth * 0.46)));
}

function clampInspectorWidth(width: number, viewportWidth?: number): number {
  const next = Number.isFinite(width) ? width : INSPECTOR_WIDTH_DEFAULT;
  return Math.round(Math.min(inspectorWidthMax(viewportWidth), Math.max(INSPECTOR_WIDTH_MIN, next)));
}

// per-run routing (/run/<id>): the URL is derived from the active run id (drafts
// map to "/" since they have no backend row yet). Reading it on load restores a
// deep-linked conversation; the dynamic route app/run/[id]/page.tsx serves it.
const runIdFromPath = (): string => {
  if (typeof window === "undefined") return "";
  const m = window.location.pathname.match(/^\/run\/([^/]+)\/?$/);
  return m ? decodeURIComponent(m[1]) : "";
};
const urlForRun = (id: string): string =>
  id && !isDraft(id) ? `/run/${encodeURIComponent(id)}` : "/";

export default function Page() {
  return (
    <I18nProvider>
      <LoginGate>
        <Deck />
      </LoginGate>
    </I18nProvider>
  );
}

function Deck() {
  // Start every page load on a FRESH draft conversation (ChatGPT-style: open →
  // empty new chat, history lives in the rail). We must NOT bind to a shared
  // fixed id like "deck-run" — that reopens (and replays) one ever-growing log,
  // which is exactly the "new solve still shows old chat" bug.
  //
  // The draft id is LOCAL until the operator actually dispatches: a draft never
  // touches the backend, so opening the deck (or hitting "+ New solve") never
  // mints empty run-NNNN stubs that clutter the rail. dispatch() promotes the
  // draft to a real backend run id at send time.
  const t = useT();
  // Init to "" (deterministic across the static-export prerender AND client
  // hydration), then mint the real random draft id in a mount-only effect.
  // newDraftId() uses Date.now()+Math.random(), so calling it during useState
  // initialization runs it twice — once at build prerender, once at hydration —
  // producing different ids and a React hydration "text content does not match"
  // error. Deferring to a post-mount effect keeps the first render identical.
  const [runId, setRunId] = useState("");
  useEffect(() => {
    // seed from the URL (/run/<id> deep-link / refresh); else a fresh draft.
    setRunId((cur) => cur || runIdFromPath() || newDraftId());
    // back/forward navigation between runs → re-read the path.
    const onPop = () => setRunId(runIdFromPath() || newDraftId());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  // keep the URL in sync with the active run (replace, not push — internal run
  // switches shouldn't pile up history entries; deep-link/refresh still work).
  useEffect(() => {
    if (!runId) return;
    const url = urlForRun(runId);
    if (window.location.pathname !== url) window.history.replaceState({}, "", url);
  }, [runId]);
  const { deck, connected, start, sendHitl, resolve } = useRun(runId);
  const budget = useBudgetSnapshot(runId);
  const runtime = useRuntimePools(runId);

  const [railCollapsed, setRailCollapsed] = useState(false);
  const [railWidth, setRailWidth] = useState(RAIL_WIDTH_DEFAULT);
  const [railWidthReady, setRailWidthReady] = useState(false);
  const [theme, setTheme] = useState<ThemeMode>("light");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [btwOpen, setBtwOpen] = useState(false);
  const [startupTestOpen, setStartupTestOpen] = useState(false);
  // Roster-row → Worker 详情 focus seed. The nonce bumps on every click so
  // re-clicking the same worker re-focuses the lanes panel; WorkerLanes reacts
  // only to a new nonce, leaving the operator's manual chip filtering intact.
  const [winW, setWinW] = useState(typeof window !== "undefined" ? window.innerWidth : 1280);
  const [listBump, setListBump] = useState(0);
  // Unified toast/action-feedback lane: every operator mutation confirms here
  // (or reports failure). pushToast(...) is threaded into the action handlers.
  const { toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const shellRef = useRef<HTMLDivElement | null>(null);
  // Files attached to the NEXT dispatch (file-based tracks). Saved server-side
  // the moment they're attached; we hold the returned absolute paths here so
  // dispatch() can put them on challenge.attachments. Lives at this level (not
  // in the Composer) so it survives into dispatch and resets on run switch.
  const [attachments, setAttachments] = useState<SavedFile[]>([]);
  // Phase 4 shell state: right-column inspector visibility, Stage Rail →
  // Decision Timeline jump target, and the Operator Command Bar's target.
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_WIDTH_DEFAULT);
  const [inspectorWidthReady, setInspectorWidthReady] = useState(false);
  const [stageJump, setStageJump] = useState<{ stage: Stage; nonce: number } | null>(null);
  const [opTarget, setOpTarget] = useState("global");
  const [opFocusNonce, setOpFocusNonce] = useState(0);

  const runs = useRunList(4000, listBump);
  const folders = useFolders(8000, listBump);
  const bump = () => setListBump((n) => n + 1);

  // shorthand for a failure toast — shown whenever a mutation helper returns
  // null/false (network / backend error), the biggest silent-failure gap today.
  const toastFail = () => pushToast({ msg: t("toast.actionFailed"), variant: "error" });

  useEffect(() => {
    try {
      const saved = readKey("dswarm.theme");
      if (saved === "dark" || saved === "light") {
        setTheme(saved);
        return;
      }
      if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) setTheme("dark");
    } catch {
      // keep the default light theme when storage/media is unavailable
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      writeKey("dswarm.theme", theme);
    } catch {
      // theming should still work for this session
    }
  }, [theme]);

  const toggleTheme = () => setTheme((cur) => (cur === "dark" ? "light" : "dark"));

  // Operator command → swarm. Wraps sendHitl so the otherwise-silent "生成复盘"
  // (writeup) command gives immediate feedback: the planner takes seconds to
  // produce the report and it lands as a normal chat bubble, so without this the
  // button looked dead. Every other command path is untouched (pass-through).
  const onCommand = useCallback(
    (target: string, action: string, text: string) => {
      if (action === "writeup") {
        pushToast({ msg: t("toast.writeupRequested"), variant: "info", icon: "pencil" });
      }
      return sendHitl(target, action, text);
    },
    [sendHitl, pushToast, t]
  );

  useEffect(() => {
    const onR = () => setWinW(window.innerWidth);
    window.addEventListener("resize", onR);
    return () => window.removeEventListener("resize", onR);
  }, []);

  useEffect(() => {
    try {
      const raw = readKey(RAIL_WIDTH_STORAGE_KEY);
      const parsed = raw ? Number(raw) : NaN;
      if (Number.isFinite(parsed)) setRailWidth(clampRailWidth(parsed, window.innerWidth));
    } catch {
      // localStorage may be blocked; keep the default width.
    } finally {
      setRailWidthReady(true);
    }
  }, []);

  useEffect(() => {
    if (!railWidthReady) return;
    try {
      writeKey(RAIL_WIDTH_STORAGE_KEY, String(railWidth));
    } catch {
      // localStorage may be blocked; resizing should still work for this session.
    }
  }, [railWidth, railWidthReady]);

  useEffect(() => {
    try {
      const raw = readKey(INSPECTOR_WIDTH_STORAGE_KEY);
      const parsed = raw ? Number(raw) : NaN;
      if (Number.isFinite(parsed)) setInspectorWidth(clampInspectorWidth(parsed, window.innerWidth));
    } catch {
      // localStorage may be blocked; keep the default width.
    } finally {
      setInspectorWidthReady(true);
    }
  }, []);

  useEffect(() => {
    if (!inspectorWidthReady) return;
    try {
      writeKey(INSPECTOR_WIDTH_STORAGE_KEY, String(inspectorWidth));
    } catch {
      // localStorage may be blocked; resizing should still work for this session.
    }
  }, [inspectorWidth, inspectorWidthReady]);

  const onRailResize = useCallback((width: number) => {
    const viewport = typeof window !== "undefined" ? window.innerWidth : undefined;
    setRailWidth(clampRailWidth(width, viewport));
  }, []);
  const onInspectorResize = useCallback((width: number) => {
    const viewport = typeof window !== "undefined" ? window.innerWidth : undefined;
    setInspectorWidth(clampInspectorWidth(width, viewport));
  }, []);

  // Cmd/Ctrl+K opens (toggles) the command palette — the single, more-powerful
  // power-user entry point. This OWNS Cmd+K now: the old composer-focus path in
  // Conversation.tsx was moved off Cmd+K (it kept the bare "/" key, and the
  // palette also exposes a "focus composer" command), so the two never
  // double-fire. We preventDefault to swallow the browser default. The settings
  // modal sits above the palette; while it's up we don't toggle (Esc there owns
  // dismissal). The panel single-key shortcuts (e/w/g/t/b) already bail on any
  // modifier, so Cmd+K never collides with them.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // BTW observer: Ctrl/Cmd+Shift+/ opens the read-only side-query drawer.
  // Avoids Cmd+B (bookmark bar), `?` (help), and bare-key panel shortcuts
  // (those bail on any modifier). While settings/palette are up we defer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === "/" || e.key === "?")) {
        if (paletteOpen) return;
        e.preventDefault();
        setBtwOpen((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [paletteOpen]);

  // Single-key shortcuts to jump the secondary panels from the keyboard, so a
  // power operator watching a run never has to reach for the mouse. Mnemonic map:
  //   e=evidence  w=workers  g=graph  t=timeline  b=blackboard
  // Esc (above) already closes, so a key only ever OPENS its panel.
  //
  // The make-or-break guard is "never fire while typing": we bail on any modifier
  // (so Cmd+K composer-focus, browser shortcuts, and Ctrl/Alt combos pass through),
  // on IME composition, and when focus is in a text field — checked on BOTH the
  // event.target AND document.activeElement so a steer like "go" typed into the
  // composer can never clobber a panel. Only armed once a run has started (panels
  // are meaningless on the welcome screen) and no modal is up.
  const PANEL_KEYS: Record<string, DetailView> = {
    e: "evidence", w: "workers", g: "graph", t: "timeline", b: "blackboard",
    f: "findings", c: "credentials", p: "pocs", r: "routes", d: "directives",
  };
  useEffect(() => {
    if (!deck.started || paletteOpen || btwOpen || startupTestOpen) return;
    const isTyping = (el: EventTarget | Element | null): boolean => {
      const node = el as HTMLElement | null;
      if (!node || typeof node.tagName !== "string") return false;
      const tag = node.tagName.toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select" || node.isContentEditable;
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.isComposing) return;
      if (isTyping(e.target) || isTyping(document.activeElement)) return;
      const view = PANEL_KEYS[e.key.toLowerCase()];
      if (!view) return;
      e.preventDefault();
      openArtifact(view);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // openArtifact is stable enough for this scope; re-bind only on the gates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deck.started, paletteOpen]);

  // When the auto-title lands live (RUN_TITLED → deck.challengeName), refetch the
  // rail immediately so the active row's "new conversation" placeholder is
  // replaced at once instead of waiting up to one poll interval.
  useEffect(() => {
    if (deck.challengeName) setListBump((n) => n + 1);
  }, [deck.challengeName]);

  const running = isRunActive(deck);
  // "still rehydrating" signal — purely client-derived, no backend field. A real
  // (non-draft) run is SELECTED and the rail's cheap summary says it started (so
  // there ARE events on disk to replay), but the deck hasn't folded its first SSE
  // event yet (deck.started still false). The SSE replay populates a beat after
  // selection, leaving a visible blank gap → show skeletons until RUN_STARTED
  // lands and flips deck.started true. Flips off the instant data arrives.
  const activeSummary = runs.find((r) => r.run_id === runId);
  const loading = !isDraft(runId)
    && !!activeSummary?.started
    && !activeSummary?.finished
    && !deck.started;
  useDeckMotion(shellRef, { flagCount: deck.flags.length });
  // Attach files to the next dispatch. They're uploaded to the run's folder
  // immediately and we keep the saved absolute paths. A draft conversation has
  // no backend run yet, so promote it to a real run-NNNN FIRST (same idiom as
  // dispatch) — then the upload lands under that id and dispatch reuses it.
  const addFiles = async (files: FileList | File[]) => {
    if (!files || files.length === 0) return;
    let id = runId;
    if (!id || isDraft(id)) {
      id = await newRun();
      setRunId(id);
    }
    const saved = await uploadFiles(id, files);
    if (saved.length) setAttachments((prev) => [...prev, ...saved]);
  };
  const removeFile = (path: string) =>
    setAttachments((prev) => prev.filter((f) => f.path !== path));

  // Dispatch the REAL swarm conversationally: one prose prompt → /start. The
  // backend infers category/target from the prompt and dispatches the shelled
  // pi CLI workers. The flag still only counts if it traces to real
  // execution output (provenance gate).
  const dispatch = async (prompt: string, opts?: DispatchOpts) => {
    // Promote a local draft to a real backend run id at send time, so the run
    // persists + orders as run-NNNN. Already-real ids (selected from the rail,
    // or promoted by addFiles when a file was attached) dispatch as-is. start()
    // reads the current `runId`; pass the freshly minted id explicitly to avoid
    // racing the setRunId state update.
    let id = runId;
    if (!id || isDraft(id)) {
      id = await newRun();
      setRunId(id);
    }
    // webSearch off → offline (backend denies the worker's WebSearch/WebFetch and,
    // by implication, the KB — a clean black-box run).
    const offline = opts ? !opts.webSearch : false;
    // pentest mode (goal-driven, no flag) adds goal/scope. CTF (default) sends
    // nothing extra so its dispatch body stays byte-identical.
    const challenge: Record<string, unknown> = {
      description: prompt,
      attachments: attachments.map((f) => f.path),
    };
    if (opts?.mode === "pentest") {
      challenge.mode = "pentest";
      if (opts.goal) challenge.goal = opts.goal;
      if (opts.scope) challenge.scope = opts.scope;
    } else {
      if (opts?.flagFormat === "token") {
        challenge.flag_format = "token";
      } else if (opts?.flagFormat === "custom" && opts.flagWrapper?.trim()) {
        challenge.flag_format_wrapper = opts.flagWrapper.trim();
      }
      if (opts?.collect) {
        // collect mode is multi-flag only. Flag shape is independent: default keeps
        // the backend's brace regex; token/custom mode is opt-in from the advanced panel.
        challenge.multi_flag = true;
        if (opts.collectCount && opts.collectCount > 0) {
          challenge.expected_flags = opts.collectCount;
        }
      }
    }
    // worker isolation toggle → backend's worker_backend ("container" runs each
    // worker in a Docker container that can't read the host bench tree; default
    // "local" = host subprocess).
    const worker_backend = opts?.containerMode ? "container" : "local";
    const runOverrides: Record<string, unknown> = {};
    if (opts?.wallClockBudget != null) runOverrides.wall_clock_budget = opts.wallClockBudget;
    if (opts?.maxTotalWorkers != null) runOverrides.max_total_workers = opts.maxTotalWorkers;
    if (opts?.costBudgetUsd != null) runOverrides.cost_budget_usd = opts.costBudgetUsd;
    // attachments: absolute paths the backend saved; the worker stages them into
    // its cwd. Filtered server-side to existing paths, so a stale one is harmless.
    try {
      await start({ kind: "swarm", prompt, offline, challenge, worker_backend, ...runOverrides }, id);
      setAttachments([]); // chips consumed by this dispatch
      setListBump((n) => n + 1);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "";
      pushToast({
        msg: detail ? `${t("toast.dispatchFailed")}：${detail}` : t("toast.dispatchFailed"),
        variant: "error",
      });
    }
  };

  const onNewSolve = () => {
    // Purely local: reset to a fresh empty draft. No backend run is created until
    // the operator dispatches, so "+ New solve" can't litter the rail with stubs.
    setAttachments([]);
    setRunId(newDraftId());
  };

  const onSelectRun = (id: string) => {
    if (id === runId) return;
    setAttachments([]);
    setOpTarget("global");
    setRunId(id);
  };

  // Pin / archive / rename / delete from a row's ⋯ menu. Optimistic: mutate the
  // backend, then bump the rail poll. Archive shows an undo toast; delete asks
  // for confirmation first (irreversible).
  const onRailAction = async (a:
    | { kind: "pin"; runId: string; pinned: boolean }
    | { kind: "archive"; runId: string; archived: boolean }
    | { kind: "rename"; runId: string; name: string }
    | { kind: "delete"; runId: string }
    | { kind: "move"; runId: string; folderId: string | null }
    | { kind: "newFolder" }
    | { kind: "renameFolder"; folderId: string; name: string }
    | { kind: "deleteFolder"; folderId: string }
  ) => {
    if (a.kind === "move") {
      const ok = await patchRun(a.runId, { folder_id: a.folderId });
      bump();
      if (!ok) toastFail();
    } else if (a.kind === "newFolder") {
      // Create instantly with a default name and hand the folder back so the rail
      // drops it into inline-rename — no blocking prompt(). Blur keeps the default.
      const folder = await createFolder(t("rail.newFolderDefault"));
      bump();
      if (folder) pushToast({ msg: t("toast.folderCreated"), variant: "success" });
      else toastFail();
      return folder;
    } else if (a.kind === "renameFolder") {
      const ok = await renameFolder(a.folderId, a.name);
      bump();
      if (ok) pushToast({ msg: t("toast.folderRenamed"), variant: "success" });
      else toastFail();
    } else if (a.kind === "deleteFolder") {
      if (!window.confirm(t("rail.confirmDeleteFolder"))) return;
      const ok = await deleteFolder(a.folderId);
      bump();
      if (ok) pushToast({ msg: t("toast.folderDeleted"), variant: "success" });
      else toastFail();
    } else if (a.kind === "pin") {
      const ok = await patchRun(a.runId, { pinned: a.pinned });
      bump();
      if (!ok) toastFail();
    } else if (a.kind === "rename") {
      const ok = await patchRun(a.runId, { name: a.name });
      bump();
      if (ok) pushToast({ msg: t("toast.renamed"), variant: "success" });
      else toastFail();
    } else if (a.kind === "archive") {
      const ok = await patchRun(a.runId, { archived: a.archived });
      bump();
      if (!ok) { toastFail(); return; }
      if (a.archived) {
        // KEEP the archive-undo behavior — migrated into the unified lane.
        pushToast({
          msg: t("rail.toast.archived"),
          variant: "info",
          undo: async () => { await patchRun(a.runId, { archived: false }); bump(); },
        });
      }
    } else if (a.kind === "delete") {
      if (!window.confirm(t("rail.confirmDelete"))) return;
      const ok = await deleteRun(a.runId);
      // if we just deleted the open conversation, fall back to a fresh draft
      if (ok && a.runId === runId) { setAttachments([]); setRunId(newDraftId()); }
      bump();
      if (ok) pushToast({ msg: t("toast.deleted"), variant: "success" });
      else toastFail();
    }
  };

  // Fleet batch control (§5.2): the rail already confirmed (for stop) and
  // filtered eligibility; here we fan out the existing single-run HITL API —
  // no batch endpoint, no history deletion.
  const onBatch = async (action: BatchAction, runIds: string[]) => {
    const results = await Promise.all(runIds.map((id) => sendRunHitl(id, action)));
    const okCount = results.filter(Boolean).length;
    if (okCount > 0) {
      pushToast({
        msg: t("toast.batchDone", { n: okCount, action: t(`fleet.batch.${action}`) }),
        variant: okCount === runIds.length ? "success" : "info",
      });
    }
    if (okCount < runIds.length) toastFail();
    bump();
  };

  // Detail views are real pages now (/run/<id>/<view>): navigate in-tab —
  // links elsewhere keep native middle/ctrl-click new-tab semantics. The old
  // in-page drawer is gone (it crammed a fourth column into the deck).
  const openArtifact = (view: DetailView) => {
    if (!runId || isDraft(runId)) return;
    window.location.href = detailUrlForRun(runId, view);
  };

  // Roster/inspector worker click → the worker firehose page (same tab).
  const onOpenWorker = (_id: string) => {
    openArtifact("workers");
  };

  // Inspector worker card "Redirect" → seed the Operator Command Bar with that
  // worker as the target and focus its input (the bar sends via onCommand).
  const onRedirectWorker = (id: string) => {
    setOpTarget(`solver:${id}`);
    setOpFocusNonce((n) => n + 1);
  };

  // Stage Rail click → scroll the Decision Timeline to that stage's anchor.
  const onJumpStage = (stage: Stage) =>
    setStageJump((prev) => ({ stage, nonce: (prev?.nonce ?? 0) + 1 }));

  // operator runtime worker control (BE-worker-management): add/kill a worker on
  // the LIVE run. Best-effort; the planner drains the command next tick and the
  // worker lifecycle events (worker_spawned / worker_killed) fold back over SSE.
  const onSpawnWorker = async (engine?: string) => {
    if (!runId) return;
    const ok = await spawnWorker(runId, engine);
    if (ok) pushToast({ msg: t("toast.workerSpawned"), variant: "success", icon: "cpu" });
    else toastFail();
  };
  const onKillWorker = async (solverId: string) => {
    if (!runId) return;
    const ok = await killWorker(runId, solverId);
    if (ok) pushToast({ msg: t("toast.workerKilled"), variant: "info", icon: "cpu" });
    else toastFail();
  };

  // Do not let a failed recovery become an unhandled promise rejection or look
  // like a successful "swarm relaunched" event. The backend now rejects before
  // RUN_REOPENED for configuration failures; surface that detail in the same
  // action-feedback lane as the other operator controls.
  const onResolve = async () => {
    try {
      await resolve();
    } catch (err) {
      const detail = err instanceof Error ? err.message : "";
      pushToast({
        msg: detail ? `${t("toast.dispatchFailed")}：${detail}` : t("toast.dispatchFailed"),
        variant: "error",
      });
    }
  };

  const showTimeline = deck.started || loading;

  return (
    <div ref={shellRef} className="shell cc-shell motion-root">
      <a href="#main-conversation" className="skip-link">{t("a11y.skipToMain")}</a>
      <TopBar
        deck={deck}
        connected={connected}
        running={running}
        stageInfo={deriveStage(deck)}
        inspectorOpen={inspectorOpen}
        onToggleRail={() => setRailCollapsed((v) => !v)}
        onToggleInspector={() => setInspectorOpen((v) => !v)}
        onJumpStage={onJumpStage}
        onCommand={onCommand}
        onResolve={onResolve}
        onOpenBtw={() => {
          if (runId && !isDraft(runId)) {
            // the dedicated BTW page is the primary surface now
            window.location.href = detailUrlForRun(runId, "btw");
          } else {
            setBtwOpen(true);
          }
        }}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
      <div className="cc-body layout-refined">
        <ThreadRail
          collapsed={railCollapsed}
          width={railWidth}
          runs={runs}
          folders={folders}
          activeRunId={runId}
          draftActive={isDraft(runId)}
          connected={connected}
          onNew={onNewSolve}
          onSelect={onSelectRun}
          onAction={onRailAction}
          onResize={onRailResize}
          onOpenSettings={() => { window.location.href = "/settings/workers"; }}
          onStartupTest={() => setStartupTestOpen(true)}
          onBatch={onBatch}
        />
        <main id="main-conversation" className="main motion-shell-piece" aria-label={t("a11y.main")}>
          {showTimeline ? (
            <DecisionTimeline
              deck={deck}
              loading={loading}
              jump={stageJump}
              onOpenWorker={onOpenWorker}
              onOpenEvidence={() => openArtifact("evidence")}
            />
          ) : (
            <Conversation
              onDispatch={dispatch}
              attachments={attachments}
              onAddFiles={addFiles}
              onRemoveFile={removeFile}
            />
          )}
        </main>
        {deck.started && inspectorOpen && (
          <SwarmInspector
            deck={deck}
            running={running}
            runId={runId}
            onOpenWorker={onOpenWorker}
            onRedirectWorker={onRedirectWorker}
            onKillWorker={onKillWorker}
            onCommand={onCommand}
            onHitlAnswered={() => pushToast({ msg: t("hitl.answered"), variant: "success" })}
            width={inspectorWidth}
            onResize={onInspectorResize}
            minWidth={INSPECTOR_WIDTH_MIN}
            maxWidth={inspectorWidthMax(winW)}
            defaultWidth={INSPECTOR_WIDTH_DEFAULT}
            budgetSnapshot={budget.snapshot}
            runtimeSnapshot={runtime.snapshot}
            runtimeLoading={runtime.loading}
            runtimeError={runtime.error}
            budgetLoading={budget.loading}
            budgetRebuilding={budget.rebuilding}
            budgetError={budget.error}
            onRebuildBudget={async () => {
              try {
                await budget.rebuild();
                pushToast({ msg: t("budget.rebuildSuccess"), variant: "success" });
              } catch (err) {
                pushToast({ msg: `${t("budget.rebuildFailed")}: ${err instanceof Error ? err.message : String(err)}`, variant: "error" });
              }
            }}
          />
        )}
      </div>
      <OperatorBar
        started={deck.started}
        running={running}
        solvers={Object.keys(deck.lanes)}
        target={opTarget}
        onTargetChange={setOpTarget}
        focusNonce={opFocusNonce}
        onCommand={onCommand}
      />
      <ToastLane toasts={toasts} onDismiss={dismissToast} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        started={deck.started}
        running={running}
        runs={runs}
        activeRunId={runId}
        onNewSolve={onNewSolve}
        onOpenArtifact={openArtifact}
        onSelectRun={onSelectRun}
        onSpawnWorker={onSpawnWorker}
        onOpenSettings={() => { window.location.href = "/settings/workers"; }}
      />
      <BtwPanel
        open={btwOpen}
        onClose={() => setBtwOpen(false)}
        runId={runId}
      />
      <StartupTestPanel
        open={startupTestOpen}
        onClose={() => setStartupTestOpen(false)}
      />
    </div>
  );
}
