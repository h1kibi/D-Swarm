"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRun, useRunList, useFolders, useBudgetSnapshot, useRuntimePools, useRunObservability, hasTargetDispatchMismatch, newRun, patchRun, deleteRun, retitleRun, uploadFiles, spawnWorker, killWorker, createFolder, renameFolder, deleteFolder, sendRunHitl, SavedFile, type RunSummary } from "@/lib/useRun";
import { useLang, useT } from "@/lib/i18n";
import { GraphNode, isRunActive } from "@/lib/events";
import { Icon } from "@/components/Icon";
import { ThreadRail } from "@/components/ThreadRail";
import { StartupTestPanel } from "@/components/StartupTestPanel";
import { Conversation } from "@/components/Conversation";
import type { DispatchOpts } from "@/components/Conversation";
import { CommandPalette } from "@/components/CommandPalette";
import { BtwPanel } from "@/components/BtwPanel";
import { ToastLane, useToasts } from "@/components/Toast";
import { detailUrlForRun, type DetailView } from "@/lib/runRoute";
import { useTheme } from "@/lib/theme";
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
import { TargetLibrary } from "@/components/TargetLibrary";
import { TargetWorkspace } from "@/components/TargetWorkspace";
import { TargetDetailsModal } from "@/components/TargetDetailsModal";
import { TargetCreateModal, type TargetCreateForm } from "@/components/TargetCreateModal";
import { RunLaunchWizard, type LaunchBindingSelection } from "@/components/RunLaunchWizard";
import { initialTargetId, projectTargets, targetForRun, type TargetProjection } from "@/lib/targetProjection";
import { addTargetHint, archiveTarget, auditTargetReview, createTarget, createTargetDirection, createTargetRun, patchTarget, useTargets, type TargetRecord } from "@/lib/useTargets";
import { workerBackendOverride } from "@/lib/launch";
import { RunDeleteDialog } from "@/components/RunDeleteDialog";

/**
 * D-Swarm Command Deck 鈥?Command-center shell (docs/07 搂5, Phase 4).
 *
 * The conversation is NO LONGER the spine. The layout is:
 *
 *   TopBar (brand 路 run/target 路 Stage Rail 路 flags 路 cost 路 connection 路 controls)
 *   Run Fleet (left) 鈹?Decision Timeline (center) 鈹?Live Swarm Inspector (right)
 *   Operator Command Bar (persistent bottom)
 *
 * The deck stays a dumb subscriber (搂3): dispatch POSTs /start with the prose
 * prompt, commands POST /hitl, and everything else folds back from the run's
 * SSE stream. A not-yet-dispatched DRAFT still gets the conversational
 * welcome/composer (that surface is how a run is launched); once a run exists,
 * the Decision Timeline takes over the center.
 */

// A draft is a local, not-yet-dispatched conversation. It never hits the backend
// until the operator sends a prompt 鈥?see dispatch() / onNewSolve().
const newDraftId = () => `draft-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
const isDraft = (id: string) => id.startsWith("draft-");
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
  return <Deck />;
}

function Deck() {
  // Start every page load on a FRESH draft conversation (ChatGPT-style: open 鈫?
  // empty new chat, history lives in the rail). We must NOT bind to a shared
  // fixed id like "deck-run" 鈥?that reopens (and replays) one ever-growing log,
  // which is exactly the "new solve still shows old chat" bug.
  //
  // The draft id is LOCAL until the operator actually dispatches: a draft never
  // touches the backend, so opening the deck (or hitting "+ New solve") never
  // mints empty run-NNNN stubs that clutter the rail. dispatch() promotes the
  // draft to a real backend run id at send time.
  const { lang } = useLang();
  const t = useT();
  // Init to "" (deterministic across the static-export prerender AND client
  // hydration), then mint the real random draft id in a mount-only effect.
  // newDraftId() uses Date.now()+Math.random(), so calling it during useState
  // initialization runs it twice 鈥?once at build prerender, once at hydration 鈥?
  // producing different ids and a React hydration "text content does not match"
  // error. Deferring to a post-mount effect keeps the first render identical.
  const [runId, setRunId] = useState("");
  // Hydration-safe: "" on the prerender, then adopt the path's run id on mount.
  // `/run/<id>` deep links (including `?review=1` from a terminal Run) must
  // restore THAT run — without this the deck always ignored the path. With no
  // path we keep "" (the welcome surface); a draft id is minted by explicit
  // actions like "New solve", exactly as before.
  useEffect(() => {
    const fromPath = runIdFromPath();
    if (fromPath) setRunId(fromPath);
  }, []);
  const [activeTargetId, setActiveTargetId] = useState<string | null>(null);
  const [targetCreateOpen, setTargetCreateOpen] = useState(false);
  // Select the newest durable Target once on boot so returning operators land on
  // the Target workspace instead of the generic welcome composer. After that
  // explicit navigation (including “New solve”) always wins.
  const initialTargetSelectionDone = useRef(false);
  const [launchWizardTarget, setLaunchWizardTarget] = useState<TargetRecord | null>(null);
  // A Target Run is created before /start. Keep its id across a failed launch
  // so retrying the wizard never creates a second Target -> Run relationship.
  const [launchRunId, setLaunchRunId] = useState<string | null>(null);
  const [targetDetails, setTargetDetails] = useState<TargetProjection | null>(null);
  const [deleteRunTarget, setDeleteRunTarget] = useState<RunSummary | null>(null);
  // keep the URL in sync with the active run (replace, not push — internal run
  // switches shouldn't pile up history entries; deep-link/refresh still work).
  // A `?review=1` deep link is preserved until the Review effect below consumes
  // it, otherwise this sync would strip the query before the modal can open.
  useEffect(() => {
    if (!runId) return;
    const url = urlForRun(runId);
    if (window.location.pathname !== url) {
      const keepQuery = window.location.search;
      window.history.replaceState({}, "", `${url}${keepQuery}`);
    }
  }, [runId]);
  const { deck, connected, start, sendHitl, resolve } = useRun(runId);
  const budget = useBudgetSnapshot(runId);
  const runtime = useRuntimePools(runId);
  const observability = useRunObservability(runId);

  // Desktop opens on the focused workspace; the target rail is available from
  // the top-left control and never consumes space while hidden.
  const [railCollapsed, setRailCollapsed] = useState(true);
  const [railWidth, setRailWidth] = useState(RAIL_WIDTH_DEFAULT);
  const [railWidthReady, setRailWidthReady] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [btwOpen, setBtwOpen] = useState(false);
  const [startupTestOpen, setStartupTestOpen] = useState(false);
  // Roster-row 鈫?Worker 璇︽儏 focus seed. The nonce bumps on every click so
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
  // Phase 4 shell state: right-column inspector visibility, Stage Rail 鈫?
  // Decision Timeline jump target, and the Operator Command Bar's target.
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(INSPECTOR_WIDTH_DEFAULT);
  const [inspectorWidthReady, setInspectorWidthReady] = useState(false);
  const [stageJump, setStageJump] = useState<{ stage: Stage; nonce: number } | null>(null);
  const [opTarget, setOpTarget] = useState("global");
  const [opFocusNonce, setOpFocusNonce] = useState(0);

  const runs = useRunList(4000, listBump);
  const persistedTargets = useTargets(5000, listBump);
  const targets = projectTargets(runs, persistedTargets);
  const selectedTarget = targetForRun(targets, runId) || targets.find((target) => target.id === activeTargetId);
  useEffect(() => {
    if (initialTargetSelectionDone.current) return;
    // A deep-linked Run already determines its Target; do not overwrite it.
    if (runId && !isDraft(runId)) {
      initialTargetSelectionDone.current = true;
      return;
    }
    const firstTargetId = initialTargetId(targets, runId);
    if (firstTargetId) {
      setActiveTargetId(firstTargetId);
      initialTargetSelectionDone.current = true;
    }
  }, [persistedTargets.length, targets, runs.length, runId]);
  const folders = useFolders(8000, listBump);
  const bump = () => setListBump((n) => n + 1);

  // shorthand for a failure toast 鈥?shown whenever a mutation helper returns
  // null/false (network / backend error), the biggest silent-failure gap today.
  const toastFail = () => pushToast({ msg: t("toast.actionFailed"), variant: "error" });

  const { theme, toggleTheme } = useTheme();

  // Operator commands are scoped to this Run.  The bar supplies explicit
  // recipients for Prompt; stop/resume are normalized as Run-wide by the bar
  // and backend. Review is a local Target audit entry point.
  const onCommand = useCallback(
    (target: string, action: string, text: string, recipients?: string[]) => {
      return sendHitl(target, action, text, { recipients });
    },
    [sendHitl]
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

  // Cmd/Ctrl+K opens (toggles) the command palette 鈥?the single, more-powerful
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
  // on IME composition, and when focus is in a text field 鈥?checked on BOTH the
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

  // When the auto-title lands live (RUN_TITLED 鈫?deck.challengeName), refetch the
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
  const activeSummary = runs.find((r) => r.run_id === runId)
    ?? selectedTarget?.runs.find((r) => r.run_id === runId);
  // A historical Target Run with a binding mismatch is replayable but cannot be
  // resumed: it would execute a different challenge under the old sidecar.
  const resolveBlocked = hasTargetDispatchMismatch(activeSummary);
  const loading = !isDraft(runId)
    && !!activeSummary?.started
    && !activeSummary?.finished
    && !deck.started;
  const terminal = Boolean(activeSummary?.finished || deck.finished);
  const onReview = useCallback(() => {
    if (!terminal || !selectedTarget) return;
    setTargetDetails(selectedTarget);
  }, [terminal, selectedTarget]);
  useEffect(() => {
    // A terminal Run's Review action uses the home Target workspace as the
    // durable audit entry point. Open it only after the target projection has
    // arrived; the modal still requires explicit operator approval before write.
    if (!initialTargetSelectionDone.current || !selectedTarget || !terminal) return;
    const review = new URLSearchParams(window.location.search).get("review");
    if (review !== "1") return;
    setTargetDetails(selectedTarget);
    window.history.replaceState({}, "", window.location.pathname);
  }, [selectedTarget, terminal]);
  useDeckMotion(shellRef, { flagCount: deck.flags.length });
  // Attach files to the next dispatch. They're uploaded to the run's folder
  // immediately and we keep the saved absolute paths. A draft conversation has
  // no backend run yet, so promote it to a real run-NNNN FIRST (same idiom as
  // dispatch) 鈥?then the upload lands under that id and dispatch reuses it.
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

  // Dispatch the REAL swarm conversationally: one prose prompt 鈫?/start. The
  // backend infers category/target from the prompt and dispatches the shelled
  // pi CLI workers. The flag still only counts if it traces to real
  // execution output (provenance gate).
  const dispatch = async (prompt: string, opts?: DispatchOpts) => {
    // Promote a local draft to a real backend run id at send time, so the run
    // persists + orders as run-NNNN. Already-real ids (selected from the rail,
    // or promoted by addFiles when a file was attached) dispatch as-is. start()
    // reads the current `runId`; pass the freshly minted id explicitly to avoid
    // racing the setRunId state update.
    let targetId = opts?.targetId || (selectedTarget?.id && selectedTarget.id.startsWith("target-") ? selectedTarget.id : null);
    if (!targetId) {
      const createdTarget = await createTarget({
        name: prompt.trim().split(/\s+/).slice(0, 8).join(" ") || "Untitled target",
        description: prompt,
        category: "misc",
      });
      targetId = createdTarget?.id || null;
      if (targetId) setActiveTargetId(targetId);
    }
    let id = runId;
    if (opts?.runId) {
      id = opts.runId;
      setRunId(id);
    } else if (!id || isDraft(id)) {
      id = await newRun();
      setRunId(id);
    }
    // webSearch off 鈫?offline (backend denies the worker's WebSearch/WebFetch and,
    // by implication, the KB 鈥?a clean black-box run).
    const offline = opts ? !opts.webSearch : false;
    // pentest mode (goal-driven, no flag) adds goal/scope. CTF (default) sends
    // nothing extra so its dispatch body stays byte-identical.
    const launchTarget = opts?.targetId ? targets.find((item) => item.id === opts.targetId) : selectedTarget;
    const challenge: Record<string, unknown> = {
      name: launchTarget?.name || undefined,
      category: launchTarget?.category || undefined,
      target: launchTarget?.targetAddress || undefined,
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
    // An omitted backend is intentional: the server resolves the configured
    // worker_config/env/default backend. A local request remains explicit and
    // still goes through the backend's dual local-worker safety gate.
    const runOverrides: Record<string, unknown> = workerBackendOverride(opts?.containerMode);
    if (opts?.wallClockBudget != null) runOverrides.wall_clock_budget = opts.wallClockBudget;
    if (opts?.maxTotalWorkers != null) runOverrides.max_total_workers = opts.maxTotalWorkers;
    if (opts?.costBudgetUsd != null) runOverrides.cost_budget_usd = opts.costBudgetUsd;
    if (opts?.directionId) runOverrides.direction_id = opts.directionId;
    if (opts?.workerProfileId) runOverrides.engines = [opts.workerProfileId];
    if (opts?.llmMode) runOverrides.llm_mode = opts.llmMode;
    if (opts?.llmOverrides) runOverrides.llm_overrides = opts.llmOverrides;
    if (opts?.reviewPolicy) runOverrides.review_policy = opts.reviewPolicy;
    // attachments: absolute paths the backend saved; the worker stages them into
    // its cwd. Filtered server-side to existing paths, so a stale one is harmless.
    try {
      await start({ kind: "swarm", prompt, offline, challenge, target_id: targetId, ...runOverrides }, id);
      setAttachments([]); // chips consumed by this dispatch
      setListBump((n) => n + 1);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "";
      pushToast({
        msg: detail ? `${t("toast.dispatchFailed")}: ${detail}` : t("toast.dispatchFailed"),
        variant: "error",
      });
      if (opts?.throwOnError) throw err;
    }
  };

  const onNewSolve = () => {
    setAttachments([]);
    setLaunchRunId(null);
    setLaunchWizardTarget(null);
    setTargetCreateOpen(true);
  };

  const createTargetFromModal = async (form: TargetCreateForm) => {
    const created = await createTarget({
      name: form.name,
      kind: form.kind,
      category: form.category,
      description: form.description,
      target_address: form.target_address || undefined,
      flag_format_hint: form.flag_format_hint || undefined,
      expected_flags: form.expected_flags,
      multi_flag: form.multi_flag,
      primary_direction: form.direction ? {
        label: form.direction,
        description: form.direction_description,
        category: form.category,
      } : undefined,
    });
    if (!created?.id) throw new Error("Target 创建失败，请检查后端连接。");
    setTargetCreateOpen(false);
    setLaunchRunId(null);
    setLaunchWizardTarget(null);
    setActiveTargetId(created.id);
    setRunId(newDraftId());
    bump();
    pushToast({ msg: "目标已创建", variant: "success" });
  };

  const onNewTargetRun = async () => {
    const targetId = selectedTarget?.id;
    if (!targetId || !targetId.startsWith("target-")) {
      setTargetCreateOpen(true);
      return;
    }
    const targetRecord = persistedTargets.find((item) => item.id === targetId);
    if (!targetRecord) {
      setTargetCreateOpen(true);
      return;
    }
    setLaunchRunId(null);
    setLaunchWizardTarget(targetRecord);
  };

  const launchConfiguredRun = async (target: TargetRecord, selection: LaunchBindingSelection) => {
    let id = launchRunId;
    if (!id) {
      const created = await createTargetRun(target.id, {
        direction_id: selection.directionId || undefined,
        direction: selection.direction || undefined,
        inherit_verified_facts: selection.inheritVerifiedFacts,
        verified_fact_ids: selection.verifiedFactIds,
        include_target_hints: selection.includeTargetHints,
        target_hint_ids: selection.targetHintIds,
      });
      id = created.run_id;
      setLaunchRunId(id);
    }
    setActiveTargetId(target.id);
    setRunId(id);
    // Target metadata and saved hints are merged by /start; this prompt is the
    // operator's per-Run instruction and remains independent from the Target.
    // Launch errors are rethrown by dispatch so RunLaunchWizard keeps the form
    // open and can retry this same already-created Run.
    await dispatch(selection.prompt, {
      webSearch: true,
      mode: target.kind === "pentest" ? "pentest" : "ctf",
      targetId: target.id,
      directionId: selection.directionId || undefined,
      runId: id,
      workerProfileId: selection.workerProfileId,
      llmMode: selection.llmMode,
      llmOverrides: selection.llmOverrides,
      reviewPolicy: selection.review,
      throwOnError: true,
    });
    // Only a successful /start closes the wizard and releases the retry id.
    setLaunchRunId(null);
    setLaunchWizardTarget(null);
  };

  const updateTargetDetails = async (changes: Record<string, unknown>): Promise<boolean> => {
    if (!targetDetails) return false;
    const updated = await patchTarget(targetDetails.id, changes);
    if (!updated) {
      toastFail();
      return false;
    }
    bump();
    const nextProjection = projectTargets(runs, [updated]).find((item) => item.id === targetDetails.id);
    if (nextProjection) setTargetDetails(nextProjection);
    pushToast({ msg: "目标已更新", variant: "success" });
    return true;
  };

  const addHintToTarget = async (text: string): Promise<boolean> => {
    if (!targetDetails) return false;
    const hint = await addTargetHint(targetDetails.id, text);
    if (!hint) {
      toastFail();
      return false;
    }
    bump();
    setTargetDetails((current) => current ? {
      ...current,
      hints: [...(current.hints || []), hint],
      updated: Date.now(),
    } : current);
    pushToast({ msg: "提示已保存到目标", variant: "success" });
    return true;
  };
  const auditTargetFacts = async (facts: Array<Record<string, unknown>>): Promise<boolean> => {
    if (!targetDetails || !facts.length) return false;
    const ok = await auditTargetReview(targetDetails.id, facts);
    if (!ok) {
      toastFail();
      return false;
    }
    bump();
    pushToast({ msg: "已将审计事实写入目标", variant: "success" });
    return true;
  };

  const addDirectionToTarget = async (label: string, description?: string, category?: string): Promise<boolean> => {
    if (!targetDetails) return false;
    const direction = await createTargetDirection(targetDetails.id, { label, description, category });
    if (!direction) {
      toastFail();
      return false;
    }
    bump();
    setTargetDetails((current) => current ? {
      ...current,
      directions: [...(current.directions || []), direction],
      primaryDirection: current.primaryDirection || String(direction.label || label),
      primaryDirectionId: current.primaryDirectionId || String(direction.id || ""),
      updated: Date.now(),
    } : current);
    pushToast({ msg: "方向已添加到目标", variant: "success" });
    return true;
  };

  const onSelectTarget = (targetId: string) => {
    const target = targets.find((item) => item.id === targetId);
    if (!target) return;
    setLaunchRunId(null);
    setLaunchWizardTarget(null);
    setActiveTargetId(targetId);
    const firstRun = target.runs[0]?.run_id;
    if (firstRun) {
      onSelectRun(firstRun);
    } else {
      setAttachments([]);
      setOpTarget("global");
      setRunId(newDraftId());
    }
  };

  const onSelectRun = (id: string) => {
    if (id === runId) return;
    setLaunchRunId(null);
    setLaunchWizardTarget(null);
    setAttachments([]);
    setOpTarget("global");
    const nextTarget = targets.find((target) => target.runs.some((run) => run.run_id === id));
    if (nextTarget?.id.startsWith("target-")) setActiveTargetId(nextTarget.id);
    setRunId(id);
  };

  // Pin / archive / rename / delete from a row's ⋯ menu. Optimistic: mutate the
  // backend, then bump the rail poll. Archive shows an undo toast; delete asks
  // for confirmation first (irreversible).
  const onRailAction = async (a:
    | { kind: "pin"; runId: string; pinned: boolean }
    | { kind: "archive"; runId: string; archived: boolean }
    | { kind: "rename"; runId: string; name: string }
    | { kind: "retitle"; runId: string }
    | { kind: "delete"; runId: string }
    | { kind: "move"; runId: string; folderId: string | null }
    | { kind: "newFolder" }
    | { kind: "renameFolder"; folderId: string; name: string }
    | { kind: "deleteFolder"; folderId: string }
    | { kind: "renameTarget"; targetId: string; name: string }
    | { kind: "archiveTarget"; targetId: string; archived: boolean }
    | { kind: "detailsTarget"; targetId: string }
  ) => {
    if (a.kind === "detailsTarget") {
      const target = targets.find((item) => item.id === a.targetId);
      if (target) {
        setActiveTargetId(target.id);
        setTargetDetails(target);
      }
    } else if (a.kind === "renameTarget") {
      const updated = await patchTarget(a.targetId, { name: a.name });
      bump();
      if (updated) {
        if (a.targetId === activeTargetId) setActiveTargetId(a.targetId);
        pushToast({ msg: t("toast.renamed"), variant: "success" });
      } else toastFail();
    } else if (a.kind === "archiveTarget") {
      const updated = await archiveTarget(a.targetId, a.archived);
      bump();
      if (!updated) { toastFail(); return; }
      if (a.targetId === activeTargetId && a.archived) setActiveTargetId(null);
      pushToast({
        msg: a.archived ? "目标已归档" : "目标已恢复",
        variant: "info",
        undo: a.archived ? async () => { await archiveTarget(a.targetId, false); bump(); } : undefined,
      });
    } else if (a.kind === "move") {
      const ok = await patchRun(a.runId, { folder_id: a.folderId });
      bump();
      if (!ok) toastFail();
    } else if (a.kind === "newFolder") {
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
    } else if (a.kind === "retitle") {
      const res = await retitleRun(a.runId);
      bump();
      if (!res.ok) {
        pushToast({ msg: res.detail ? `${t("toast.retitleFailed")}: ${res.detail}` : t("toast.retitleFailed"), variant: "error" });
      } else if (res.pending) {
        pushToast({ msg: t("toast.retitlePending"), variant: "info" });
      } else {
        pushToast({ msg: t("toast.retitled", { name: res.name || "" }), variant: "success" });
      }
    } else if (a.kind === "archive") {
      const ok = await patchRun(a.runId, { archived: a.archived });
      bump();
      if (!ok) { toastFail(); return; }
      if (a.archived) {
        pushToast({
          msg: t("rail.toast.archived"),
          variant: "info",
          undo: async () => { await patchRun(a.runId, { archived: false }); bump(); },
        });
      }
    } else if (a.kind === "delete") {
      const targetRun = runs.find((run) => run.run_id === a.runId);
      if (targetRun) requestRunDelete(targetRun);
    }
  };

  const requestRunDelete = (run: RunSummary) => setDeleteRunTarget(run);
  const confirmRunDelete = async (run: RunSummary): Promise<boolean> => {
    const ok = await deleteRun(run.run_id);
    if (!ok) return false;
    if (targetDetails) setTargetDetails((current) => current ? {
      ...current,
      runs: current.runs.filter((item) => item.run_id !== run.run_id),
    } : current);
    if (run.run_id === runId) {
      setAttachments([]);
      setLaunchRunId(null);
      setLaunchWizardTarget(null);
      setRunId(newDraftId());
    }
    bump();
    pushToast({ msg: t("toast.deleted"), variant: "success" });
    return true;
  };


  // Fleet batch control (搂5.2): the rail already confirmed (for stop) and
  // filtered eligibility. pause/resume/stop fan out the single-run HITL API;
  // archive/unarchive fan out the run-metadata PATCH (no history is touched).
  const onBatch = async (action: BatchAction, runIds: string[]) => {
    if (action === "archive" || action === "unarchive") {
      const archived = action === "archive";
      const results = await Promise.all(runIds.map((id) => patchRun(id, { archived })));
      const okCount = results.filter(Boolean).length;
      if (okCount > 0) {
        pushToast({
          msg: t(archived ? "toast.batchArchived" : "toast.batchUnarchived", { n: okCount }),
          variant: okCount === runIds.length ? "success" : "info",
        });
      }
      if (okCount < runIds.length) toastFail();
      bump();
      return;
    }
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

  // Detail views are real pages now (/run/<id>/<view>): navigate in-tab 鈥?
  // links elsewhere keep native middle/ctrl-click new-tab semantics. The old
  // in-page drawer is gone (it crammed a fourth column into the deck).
  const openArtifact = (view: DetailView) => {
    if (!runId || isDraft(runId)) return;
    window.location.href = detailUrlForRun(runId, view);
  };

  const openTargetFact = (sourceRunId: string) => {
    if (!sourceRunId || isDraft(sourceRunId)) return;
    setTargetDetails(null);
    window.location.href = detailUrlForRun(sourceRunId, "evidence");
  };

  // Roster/inspector worker click 鈫?the worker firehose page (same tab).
  const onOpenWorker = (_id: string) => {
    openArtifact("workers");
  };

  // Inspector worker card "Redirect" 鈫?seed the Operator Command Bar with that
  // worker as the target and focus its input (the bar sends via onCommand).
  const onRedirectWorker = (id: string) => {
    setOpTarget(`solver:${id}`);
    setOpFocusNonce((n) => n + 1);
  };

  // Stage Rail click 鈫?scroll the Decision Timeline to that stage's anchor.
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
        msg: detail ? `${t("toast.dispatchFailed")}: ${detail}` : t("toast.dispatchFailed"),
        variant: "error",
      });
    }
  };

  const showTimeline = deck.started || loading;
  // Target is the primary navigation object. A persisted Target with no Run is
  // still a useful workspace (metadata, directions, facts and the New Run entry
  // point), so it should not fall back to the first-run composer.
  const showTargetWorkspace = Boolean(selectedTarget);

  return (
    <div ref={shellRef} className="shell cc-shell target-shell motion-root">
      <a href="#main-conversation" className="skip-link">{t("a11y.skipToMain")}</a>
      <TopBar
        deck={deck}
        connected={connected}
        running={running}
        stageInfo={deriveStage(deck)}
        inspectorOpen={inspectorOpen}
        railCollapsed={railCollapsed}
        onToggleRail={() => setRailCollapsed((v) => !v)}
        onToggleInspector={() => setInspectorOpen((v) => !v)}
        onJumpStage={onJumpStage}
        onCommand={onCommand}
        onResolve={onResolve}
        resolveBlocked={resolveBlocked}
        onNewRun={selectedTarget ? onNewTargetRun : onNewSolve}
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
        <TargetLibrary
          collapsed={railCollapsed}
          width={railWidth}
          targets={targets}
          runs={runs}
          folders={folders}
          activeRunId={runId}
          activeTargetId={activeTargetId}
          draftActive={isDraft(runId)}
          connected={connected}
          onNew={onNewSolve}
          onSelect={onSelectRun}
          onSelectTarget={onSelectTarget}
          onDeleteRun={requestRunDelete}
          onAction={onRailAction}
          onResize={onRailResize}
          onOpenSettings={() => { window.location.href = "/settings/workers"; }}
          onStartupTest={() => setStartupTestOpen(true)}
          onBatch={onBatch}
        />
        <main id="main-conversation" className="main motion-shell-piece target-main" aria-label={t("a11y.main")}>
          {showTargetWorkspace ? (
            <>
              <TargetWorkspace
                target={selectedTarget}
                deck={deck}
                activeRunId={runId}
                onSelectRun={onSelectRun}
                onNewRun={onNewTargetRun}
                onDeleteRun={requestRunDelete}
                onOpenDetails={openArtifact}
                onOpenTargetDetails={() => { if (selectedTarget) setTargetDetails(selectedTarget); }}
                onOpenKnowledge={() => openArtifact("evidence")}
                observability={observability.snapshot}
              />
              {showTimeline && <div className="target-decision-feed">
                <div className="target-feed-header"><span className="eyebrow">{lang === "zh" ? "实时决策流" : "LIVE DECISION FEED"}</span><button onClick={() => openArtifact("timeline")}>{lang === "zh" ? "打开时间线" : "Open timeline"} <Icon name="chevronRight" size={13} /></button></div>
                <DecisionTimeline deck={deck} loading={loading} jump={stageJump} onOpenWorker={onOpenWorker} onOpenEvidence={() => openArtifact("evidence")} />
              </div>}
            </>
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
            bindingSnapshot={activeSummary?.binding_snapshot}
            observability={observability.snapshot}
          />
        )}
      </div>
      {targetCreateOpen && (
        <TargetCreateModal
          onClose={() => setTargetCreateOpen(false)}
          onCreate={createTargetFromModal}
        />
      )}
      {runId && !isDraft(runId) && (
        <OperatorBar
          started={deck.started}
          running={running}
          lanes={deck.lanes}
          target={opTarget}
          onTargetChange={setOpTarget}
          focusNonce={opFocusNonce}
          onCommand={onCommand}
          onReview={onReview}
          terminal={terminal}
          canReview={terminal && Boolean(selectedTarget)}
        />
      )}
      {targetDetails && (
        <TargetDetailsModal
          target={targetDetails}
          onClose={() => setTargetDetails(null)}
          onSelectRun={onSelectRun}
          onDeleteRun={requestRunDelete}
          onOpenFact={openTargetFact}
          onPatch={updateTargetDetails}
          onAddHint={addHintToTarget}
          onAddDirection={addDirectionToTarget}
          onAuditReview={auditTargetFacts}
        />
      )}
      <RunDeleteDialog
        run={deleteRunTarget}
        onClose={() => setDeleteRunTarget(null)}
        onConfirm={confirmRunDelete}
      />
      {launchWizardTarget && (
        <RunLaunchWizard
          target={launchWizardTarget}
          onClose={() => { setLaunchWizardTarget(null); setLaunchRunId(null); }}
          onLaunch={(selection) => launchConfiguredRun(launchWizardTarget, selection)}
        />
      )}
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
