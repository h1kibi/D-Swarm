"use client";

import {
  useEffect, useMemo, useRef, useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  isFactRetired,
  workerChat,
  workerLanes,
  type BlackboardFact,
  type BlackboardPoc,
  type DeckState,
  type SolverLane,
} from "@/lib/events";
import { useT } from "@/lib/i18n";
import type { ReasonIntentView } from "@/lib/reason";
import { tsMs } from "@/lib/timeline";
import {
  findingForFactSeq,
  pheromoneBand,
  pheromoneStrength,
} from "@/lib/pheromone";
import { usePheromoneClock } from "@/lib/usePheromoneClock";
import { HitlCard } from "@/components/HitlCard";
import { Icon } from "@/components/Icon";
import { workerColor, workerInitial, workerShortLabel } from "@/lib/workers";
import { BudgetStatus } from "@/components/BudgetStatusPanel";
import { RuntimeStatus } from "@/components/RuntimeStatusPanel";
import { formatDurationMs, reasonCycleRows, reasonLoopTone } from "@/lib/reason";
import { detailUrlForRun, type DetailView } from "@/lib/runRoute";
import type { RuntimePoolsSnapshot } from "@/components/runtimeStatus";
import type { BudgetSnapshot } from "@/components/budgetStatus";

/**
 * Live Swarm Inspector (docs/07 §5.5) — the right column. Default Workers view:
 * worker cards per §6.2 —
 *   collapsed: identity (worker/profile/mode) · current intent (id + goal) ·
 *     latest structured finding · provenance status · latest activity ·
 *     tokens/cost/runtime · health/timeout;
 *   expanded (hierarchical, in this order): structured findings → intent &
 *     dispatch reason → provenance → reasoning → tool calls → tool results →
 *     raw terminal output (collapsed by default, fully expandable) → artifacts →
 *     runtime diagnostics → kill/redirect controls.
 * The Intent queue lists proposed/queued intents by priority. The Attention
 * block hosts the HITL cards. Every other artifact
 * view (graph / blackboard / evidence / …) opens as the drawer panel from the
 * Panels tab — no capability is removed.
 */

const PANEL_VIEWS: { view: DetailView; key: string }[] = [
  { view: "evidence", key: "panelbtn.evidence" },
  { view: "workers", key: "panelbtn.workers" },
  { view: "graph", key: "rc.factGraph" },
  { view: "timeline", key: "panelbtn.timeline" },
  { view: "blackboard", key: "rc.blackboard" },
  { view: "findings", key: "panelbtn.findings" },
  { view: "credentials", key: "panelbtn.credentials" },
  { view: "pocs", key: "panelbtn.pocs" },
  { view: "routes", key: "panelbtn.routes" },
  { view: "directives", key: "panelbtn.directives" },
];

type Tab = "workers" | "intents" | "panels";

function fmtTokens(n?: number): string {
  if (!n) return "0";
  if (n < 1000) return String(Math.round(n));
  return `${(n / 1000).toFixed(1)}k`;
}

function fmtCost(usd?: number): string {
  if (!usd) return "";
  return `$${usd < 0.01 ? usd.toFixed(4) : usd.toFixed(2)}`;
}

function fmtRuntime(lane: SolverLane): string {
  const start = tsMs(lane.runtime?.started_at);
  if (!start) return "";
  const end = tsMs(lane.runtime?.finished_at ?? undefined) || Date.now();
  const sec = Math.max(0, Math.round((end - start) / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function healthOf(lane: SolverLane): { key: string; cls: string } {
  if (lane.runtime?.timed_out) return { key: "swarm.health.timeout", cls: "bad" };
  if (lane.runtime?.oom_killed) return { key: "swarm.health.oom", cls: "bad" };
  if (lane.paused) return { key: "swarm.health.paused", cls: "warn" };
  if (lane.online) return { key: "swarm.health.online", cls: "ok" };
  return { key: "swarm.health.offline", cls: "muted" };
}

/** Per-worker chat-derived streams: reasoning, tool CALLS (▶) and tool RESULTS
 *  (↳) as separate layers (§6.2 levels 4–6), plus the untouched concatenation
 *  for the raw-output disclosure (level 7). */
interface WorkerStreams {
  reasoning: string[];
  toolCalls: string[];
  toolResults: string[];
  raw: string;
}

/** Pheromone strength chip for one structured finding — N/A when the session
 *  has no pheromone parameters for it. Never merged with the verified chip. */
function FindingPheromone({ deck, fact, nowSec }: {
  deck: DeckState;
  fact: BlackboardFact;
  nowSec: number;
}) {
  const t = useT();
  const finding = findingForFactSeq(deck.findings, fact.factSeq);
  const strength = finding ? pheromoneStrength(finding, nowSec) : undefined;
  const band = pheromoneBand(strength);
  return (
    <span className={`sw-pher band-${band}`} title={t("pheromone.label")}>
      {strength != null ? (
        <>
          <span className="pher-bar" aria-hidden="true">
            <span className={`pher-fill ${band}`} style={{ width: `${Math.round(strength * 100)}%` }} />
          </span>
          <span className="sw-pher-val">{strength.toFixed(2)}</span>
        </>
      ) : (
        <span className="sw-pher-na">{t("pheromone.na")}</span>
      )}
    </span>
  );
}

function WorkerCard({
  lane,
  deck,
  intent,
  tokens,
  facts,
  pocs,
  streams,
  nowSec,
  expanded,
  onToggle,
  onInspect,
  onRedirect,
  onKill,
  onOpenEvidence,
  onOpenIntent,
}: {
  lane: SolverLane;
  deck: DeckState;
  intent?: ReasonIntentView;
  tokens?: number;
  /** this worker's structured findings (non-retired facts), chronological. */
  facts: BlackboardFact[];
  pocs: BlackboardPoc[];
  streams: WorkerStreams;
  nowSec: number;
  expanded: boolean;
  onToggle: () => void;
  onInspect: (id: string) => void;
  onRedirect: (id: string) => void;
  onKill: (id: string) => void;
  /** structured finding click → Evidence panel (cross-view link). */
  onOpenEvidence: () => void;
  /** intent id click → the inspector's own Intents tab (anchor + highlight). */
  onOpenIntent: (intentId: string) => void;
}) {
  const t = useT();
  const health = healthOf(lane);
  const latest = lane.toolLines[lane.toolLines.length - 1];
  const latestFact = facts[facts.length - 1];
  const verifiedCount = facts.filter((f) => f.verified).length;
  const color = workerColor(lane.solverId, lane.engine);
  const cost = fmtCost(deck.costBySolver[lane.solverId]?.usd);
  const runtime = fmtRuntime(lane);
  const reasoning = (lane.reasoning || streams.reasoning[streams.reasoning.length - 1] || "").trim();
  const artifactIds = Array.from(new Set(facts.map((f) => f.artifactId).filter(Boolean))) as string[];
  const controls = (
    <div className="sw-worker-ctl">
      <button type="button" onClick={() => onInspect(lane.solverId)}>{t("swarm.inspect")}</button>
      <button type="button" onClick={() => onRedirect(lane.solverId)}>{t("swarm.redirect")}</button>
      <button
        type="button"
        className="danger"
        onClick={() => {
          if (window.confirm(t("swarm.killConfirm", { id: lane.solverId }))) onKill(lane.solverId);
        }}
      >{t("swarm.kill")}</button>
    </div>
  );
  return (
    <div className={`sw-worker ${lane.solved ? "solved" : ""} ${expanded ? "expanded" : ""}`.trim()}>
      <div className="sw-worker-top">
        <button
          type="button"
          className="sw-worker-toggle"
          aria-expanded={expanded}
          title={t(expanded ? "swarm.collapse" : "swarm.expand")}
          onClick={onToggle}
        >
          <Icon name={expanded ? "chevronDown" : "chevronRight"} size={13} />
        </button>
        <span className="sw-avatar" style={{ background: color }} aria-hidden="true">
          {workerInitial(lane.solverId)}
        </span>
        <span className="sw-worker-id">{workerShortLabel(lane.solverId, lane.engine)}</span>
        {intent?.profile && <span className="tl-chip">{intent.profile}</span>}
        <span className={`tl-chip ${health.cls}`}>{t(health.key)}</span>
      </div>
      <div className="sw-worker-intent">
        {intent ? (
          <>
            <span className="tl-chip">{intent.mode || "intent"}</span>
            <button type="button" className="evi-link" title={t("swarm.viewIntent")}
              onClick={() => onOpenIntent(intent.id)}>{intent.id}</button>
            {" · "}{intent.goal}
          </>
        ) : (
          <span className="tl-muted">{t("swarm.noIntent")}</span>
        )}
      </div>
      {latestFact && (
        <button type="button" className="sw-worker-fact" title={t("swarm.viewEvidence")} onClick={onOpenEvidence}>
          <span className={`tl-chip ${latestFact.verified ? "ok" : "candidate"}`}>
            {t(latestFact.verified ? "timeline.fact.verified" : "timeline.fact.candidate")}
          </span>
          <span className="sw-worker-fact-text">{latestFact.summary || latestFact.fact}</span>
        </button>
      )}
      {facts.length > 0 && (
        <div className="sw-worker-prov">
          {t("swarm.provenanceFacts", { verified: verifiedCount, total: facts.length })}
        </div>
      )}
      {latest && <div className="sw-worker-activity" title={latest}>{latest}</div>}
      <div className="sw-worker-meta">
        {lane.runtime?.backend && <span>{lane.runtime.backend}</span>}
        {runtime && <span>{runtime}</span>}
        <span>{fmtTokens(tokens ?? lane.tokensSpent)} tokens</span>
        {cost && <span>{cost}</span>}
        {lane.status && <span className="tl-muted">{lane.status}</span>}
      </div>
      {controls}
      {expanded && (
        <div className="sw-worker-detail">
          {/* 1 — structured findings */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.findings")}</div>
            {facts.length === 0 ? (
              <div className="sw-sec-empty">{t("swarm.none")}</div>
            ) : (
              facts.slice().reverse().map((f, i) => (
                <button type="button" className="sw-finding" key={`${f.factSeq ?? i}`}
                  title={t("swarm.viewEvidence")} onClick={onOpenEvidence}>
                  <span className={`tl-chip ${f.verified ? "ok" : "candidate"}`}>
                    {t(f.verified ? "timeline.fact.verified" : "timeline.fact.candidate")}
                  </span>
                  <span className="sw-finding-text">{f.summary || f.fact}</span>
                  <FindingPheromone deck={deck} fact={f} nowSec={nowSec} />
                </button>
              ))
            )}
          </div>
          {/* 2 — intent & dispatch reason */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.intent")}</div>
            {intent ? (
              <div className="sw-sec-body">
                <div>
                  <button type="button" className="evi-link" title={t("swarm.viewIntent")}
                    onClick={() => onOpenIntent(intent.id)}>{intent.id}</button>
                  {" · "}{intent.mode && <span className="tl-chip">{intent.mode}</span>}
                  {intent.priority != null && <span className="tl-priority">P{intent.priority.toFixed(2)}</span>}
                </div>
                <div className="sw-sec-line">{intent.goal}</div>
                {intent.dispatchReason && (
                  <div className="sw-sec-line tl-muted">{t("swarm.dispatchReason")}: {intent.dispatchReason}</div>
                )}
              </div>
            ) : (
              <div className="sw-sec-empty">{t("swarm.noIntent")}</div>
            )}
          </div>
          {/* 3 — provenance */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.provenance")}</div>
            {facts.length === 0 ? (
              <div className="sw-sec-empty">{t("swarm.none")}</div>
            ) : (
              <div className="sw-sec-body">
                <div className="sw-sec-line">{t("swarm.provenanceFacts", { verified: verifiedCount, total: facts.length })}</div>
                {facts.slice().reverse().map((f, i) => (
                  <div className="sw-sec-line tl-muted" key={`prov-${f.factSeq ?? i}`}>
                    {f.factSeq != null && <>#{f.factSeq} · </>}
                    {f.verifier && f.verifier !== "none" ? f.verifier : t("insp.unverified")}
                    {f.witness && <> · {t("evidence.hasWitness")}</>}
                    {f.artifactId && <> · {t("evidence.hasArtifact")}</>}
                  </div>
                ))}
              </div>
            )}
          </div>
          {/* 4 — reasoning */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.reasoning")}</div>
            <div className={`sw-sec-pre ${reasoning ? "" : "idle"}`.trim()}>{reasoning || t("swarm.none")}</div>
          </div>
          {/* 5 — tool calls */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.toolCalls")}</div>
            {streams.toolCalls.length === 0 ? (
              <div className="sw-sec-empty">{t("swarm.none")}</div>
            ) : (
              <div className="sw-sec-pre">{streams.toolCalls.join("\n")}</div>
            )}
          </div>
          {/* 6 — tool results */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.toolResults")}</div>
            {streams.toolResults.length === 0 ? (
              <div className="sw-sec-empty">{t("swarm.none")}</div>
            ) : (
              <div className="sw-sec-pre">{streams.toolResults.join("\n")}</div>
            )}
          </div>
          {/* 7 — raw terminal output: collapsed by default, FULLY expandable */}
          <div className="sw-sec">
            <details className="sw-raw">
              <summary className="sw-sec-h sw-raw-summary">{t("swarm.sec.rawOutput")}</summary>
              <div className="sw-sec-pre sw-raw-body">{streams.raw || t("swarm.none")}</div>
            </details>
          </div>
          {/* 8 — artifacts */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.artifacts")}</div>
            {artifactIds.length === 0 && pocs.length === 0 ? (
              <div className="sw-sec-empty">{t("swarm.none")}</div>
            ) : (
              <div className="sw-sec-body">
                {artifactIds.map((a) => <div className="sw-sec-line" key={a}>{a}</div>)}
                {pocs.map((p) => (
                  <div className="sw-sec-line" key={p.id}>
                    <span className="artifact-badge">{p.status}</span> {p.name || p.id}
                  </div>
                ))}
              </div>
            )}
          </div>
          {/* 9 — runtime diagnostics */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.runtime")}</div>
            <dl className="evi-prov">
              <dt>{t("wlane.runtime")}</dt>
              <dd>{lane.runtime?.backend ?? t("pheromone.na")}{lane.runtime?.status ? `:${lane.runtime.status}` : ""}</dd>
              {lane.runtime?.container && <><dt>container</dt><dd>{lane.runtime.container}</dd></>}
              {lane.phase && <><dt>phase</dt><dd>{lane.phase}</dd></>}
              {typeof lane.runtime?.rc === "number" && <><dt>rc</dt><dd>{lane.runtime.rc}</dd></>}
              {lane.runtime?.timed_out && <><dt>timeout</dt><dd>{t("swarm.health.timeout")}</dd></>}
              {lane.runtime?.oom_killed && <><dt>oom</dt><dd>{t("swarm.health.oom")}</dd></>}
              {lane.session && <><dt>session</dt><dd>{lane.session}</dd></>}
              {typeof lane.tokensSpent === "number" && <><dt>tokens</dt><dd>{lane.tokensSpent.toLocaleString()}</dd></>}
              {cost && <><dt>cost</dt><dd>{cost}</dd></>}
              {lane.runtime?.error && <><dt>error</dt><dd>{lane.runtime.error}</dd></>}
            </dl>
          </div>
          {/* 10 — kill / redirect controls */}
          <div className="sw-sec">
            <div className="sw-sec-h">{t("swarm.sec.controls")}</div>
            {controls}
          </div>
        </div>
      )}
    </div>
  );
}

export function SwarmInspector({
  deck,
  running,
  onOpenWorker,
  onRedirectWorker,
  onKillWorker,
  runId,
  onCommand,
  onHitlAnswered,
  width,
  onResize,
  minWidth,
  maxWidth,
  defaultWidth,
  budgetSnapshot,
  runtimeSnapshot,
  runtimeLoading,
  runtimeError,
  budgetLoading,
  budgetRebuilding,
  budgetError,
  onRebuildBudget,
}: {
  deck: DeckState;
  running: boolean;
  onOpenWorker: (id: string) => void;
  /** seed the Operator Command Bar with this worker as the redirect target. */
  onRedirectWorker: (id: string) => void;
  onKillWorker: (id: string) => void;
  /** active run id — panel chips deep-link to the detail routes (new-tab friendly). */
  runId: string;
  onCommand: (target: string, action: string, text: string) => void;
  onHitlAnswered?: () => void;
  width: number;
  onResize: (width: number) => void;
  minWidth: number;
  maxWidth: number;
  defaultWidth: number;
  budgetSnapshot?: BudgetSnapshot | null;
  runtimeSnapshot?: RuntimePoolsSnapshot | null;
  runtimeLoading?: boolean;
  runtimeError?: string | null;
  budgetLoading?: boolean;
  budgetRebuilding?: boolean;
  budgetError?: string | null;
  onRebuildBudget?: () => void | Promise<void>;
}) {
  const t = useT();
  const [tab, setTab] = useState<Tab>("workers");
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  const [flashIntent, setFlashIntent] = useState<string | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resizeCleanupRef = useRef<(() => void) | null>(null);
  const [resizing, setResizing] = useState(false);
  const lanes = useMemo(() => workerLanes(deck), [deck]);
  const nowSec = usePheromoneClock(deck);
  useEffect(() => () => {
    if (flashTimer.current) clearTimeout(flashTimer.current);
    resizeCleanupRef.current?.();
    document.body.classList.remove("swarm-resizing");
  }, []);

  const resizeToClientX = (clientX: number) => {
    if (typeof window === "undefined") return;
    onResize(window.innerWidth - clientX);
  };

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    resizeCleanupRef.current?.();
    setResizing(true);
    document.body.classList.add("swarm-resizing");
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      resizeToClientX(moveEvent.clientX);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", cleanup);
      setResizing(false);
      document.body.classList.remove("swarm-resizing");
      resizeCleanupRef.current = null;
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", cleanup, { once: true });
    resizeCleanupRef.current = cleanup;
    resizeToClientX(event.clientX);
  };

  const onResizeKey = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onResize(width + (event.shiftKey ? 32 : 12));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onResize(width - (event.shiftKey ? 32 : 12));
    } else if (event.key === "Home") {
      event.preventDefault();
      onResize(minWidth);
    } else if (event.key === "End") {
      event.preventDefault();
      onResize(maxWidth);
    } else if (event.key === "Enter") {
      event.preventDefault();
      onResize(defaultWidth);
    }
  };

  // intent currently bound to each worker (latest dispatch wins) + the pending
  // intent queue (proposed/queued), priority desc (§5.5).
  const { intentByWorker, queue, dispatched } = useMemo(() => {
    const byWorker = new Map<string, ReasonIntentView>();
    const pending: ReasonIntentView[] = [];
    const dispatched: ReasonIntentView[] = [];
    for (const cycle of deck.reasonLoop.cycles) {
      for (const it of cycle.intents) {
        if (it.workerId) byWorker.set(it.workerId, it);
        if (it.status === "proposed" || it.status === "queued") pending.push(it);
        // worker-bound intents stay listed so worker-card → intent links have
        // an anchor to land on (the queue alone drops them once dispatched).
        else if (it.workerId && (it.status === "running" || it.status === "claimed")) dispatched.push(it);
      }
    }
    pending.sort((a, b) => (b.priority ?? -1) - (a.priority ?? -1));
    return { intentByWorker: byWorker, queue: pending, dispatched };
  }, [deck.reasonLoop]);

  // §6.2 card data: structured findings (non-retired facts) per worker,
  // chat-derived streams (reasoning / tool calls / tool results / raw), PoCs.
  const factsByActor = useMemo(() => {
    const m = new Map<string, BlackboardFact[]>();
    for (const f of deck.blackboard.facts) {
      if (!f.actor || isFactRetired(f)) continue;
      m.set(f.actor, [...(m.get(f.actor) ?? []), f]);
    }
    return m;
  }, [deck.blackboard.facts]);
  const streamsByWorker = useMemo(() => {
    const m = new Map<string, WorkerStreams>();
    for (const msg of workerChat(deck)) {
      const id = msg.solverId!;
      const e = m.get(id) ?? { reasoning: [], toolCalls: [], toolResults: [], raw: "" };
      if (msg.kind === "reasoning" || msg.kind === "text") {
        e.reasoning.push(msg.content);
        e.raw += (e.raw ? "\n" : "") + msg.content;
      } else if (msg.kind === "tool") {
        if (msg.content.startsWith("▶")) e.toolCalls.push(msg.content.replace(/^▶\s*/, ""));
        else e.toolResults.push(msg.content.replace(/^↳\s*/, ""));
        e.raw += (e.raw ? "\n" : "") + msg.content;
      }
      m.set(id, e);
    }
    return m;
  }, [deck]);
  const pocsByWorker = useMemo(() => {
    const m = new Map<string, BlackboardPoc[]>();
    for (const p of deck.blackboard.pocs ?? []) {
      if (!p.worker) continue;
      m.set(p.worker, [...(m.get(p.worker) ?? []), p]);
    }
    return m;
  }, [deck.blackboard.pocs]);

  const active = lanes.filter((l) => l.online).length;
  const hasAttention = deck.hitlRequests.length > 0;

  const toggleExpanded = (id: string) =>
    setExpandedIds((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  // Fact/intent cross-link: a worker card's intent id switches to the Intents
  // tab, scrolls the row into view and flashes it (anchor + highlight, §6.2).
  const openIntent = (intentId: string) => {
    setTab("intents");
    setFlashIntent(intentId);
    if (flashTimer.current) clearTimeout(flashTimer.current);
    flashTimer.current = setTimeout(() => setFlashIntent(null), 1600);
  };
  useEffect(() => {
    if (tab !== "intents" || !flashIntent) return;
    document.getElementById(`sw-intent-${flashIntent}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [tab, flashIntent]);

  return (
    <aside
      className={`swarm motion-inspector ${resizing ? "resizing" : ""}`}
      aria-label={t("swarm.title")}
      style={{ width: `${width}px`, flexBasis: `${width}px`, minWidth: `${minWidth}px`, maxWidth: `${maxWidth}px` } as CSSProperties}
    >
      <div
        className="swarm-resizer"
        role="separator"
        tabIndex={0}
        aria-label={t("art.resizeCanvas")}
        title={t("art.resizeCanvas")}
        aria-orientation="vertical"
        aria-valuemin={minWidth}
        aria-valuemax={maxWidth}
        aria-valuenow={width}
        onPointerDown={startResize}
        onKeyDown={onResizeKey}
        onDoubleClick={() => onResize(defaultWidth)}
      />
      <div className="swarm-tabs" role="tablist" aria-label={t("swarm.title")}>
        {(["workers", "intents", "panels"] as Tab[]).map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            className={`small ${tab === k ? "on" : ""}`}
            onClick={() => setTab(k)}
          >{t(`swarm.tab.${k}`)}</button>
        ))}
      </div>

      <div className="swarm-body">
        <BudgetStatus
          snapshot={budgetSnapshot}
          loading={budgetLoading}
          rebuilding={budgetRebuilding}
          error={budgetError}
          onRebuild={onRebuildBudget}
        />
        <RuntimeStatus
          snapshot={runtimeSnapshot}
          loading={runtimeLoading}
          error={runtimeError}
        />
        {tab === "workers" && (
          <>
            <div className="swarm-count">
              {t("swarm.activeWorkers", { active, total: lanes.length })}
            </div>
            {lanes.length === 0 && (
              <div className="swarm-empty">
                {t("insp.run.noWorkers")}
                <span className="swarm-empty-hint">{t("insp.run.noWorkersHint")}</span>
              </div>
            )}
            {lanes.map((lane) => (
              <WorkerCard
                key={lane.solverId}
                lane={lane}
                deck={deck}
                intent={intentByWorker.get(lane.solverId)}
                tokens={deck.costBySolver[lane.solverId]?.tokensIn != null
                  ? (deck.costBySolver[lane.solverId].tokensIn + deck.costBySolver[lane.solverId].tokensOut)
                  : lane.tokensSpent}
                facts={factsByActor.get(lane.solverId) ?? []}
                pocs={pocsByWorker.get(lane.solverId) ?? []}
                streams={streamsByWorker.get(lane.solverId) ?? { reasoning: [], toolCalls: [], toolResults: [], raw: "" }}
                nowSec={nowSec}
                expanded={expandedIds.has(lane.solverId)}
                onToggle={() => toggleExpanded(lane.solverId)}
                onInspect={onOpenWorker}
                onRedirect={onRedirectWorker}
                onKill={onKillWorker}
                onOpenEvidence={() => { window.location.href = detailUrlForRun(runId, "evidence"); }}
                onOpenIntent={openIntent}
              />
            ))}
          </>
        )}

        {tab === "intents" && (
          <>
            <section className="reason-strip" aria-label={t("reason.strip")}>
              <div className="reason-strip-head">
                <span className="budget-eyebrow">{t("reason.strip")}</span>
                <span className={`budget-state ${reasonLoopTone(deck.reasonLoop)}`}>
                  <span className="budget-state-dot" aria-hidden="true" />
                  {deck.reasonLoop.solved
                    ? t("reason.loopSolved")
                    : deck.reasonLoop.paused
                      ? t("reason.loopPaused")
                      : deck.reasonLoop.stopReason
                        ? t("reason.loopStopped", { reason: deck.reasonLoop.stopReason })
                        : t("reason.loopRunning", { n: deck.reasonLoop.cycles.length })}
                </span>
              </div>
              {deck.reasonLoop.recon && (
                <div className="reason-recon">
                  {t(deck.reasonLoop.recon.status === "completed" ? "reason.reconDone" : "reason.reconRunning")}
                  {deck.reasonLoop.recon.durationMs != null && (
                    <span> · {formatDurationMs(deck.reasonLoop.recon.durationMs)}</span>
                  )}
                  {deck.reasonLoop.recon.newFindings != null && (
                    <span> · {t("reason.reconFindings", { n: deck.reasonLoop.recon.newFindings })}</span>
                  )}
                </div>
              )}
              {reasonCycleRows(deck.reasonLoop).map((row) => (
                <div className="reason-cycle-row" key={row.id}>
                  <span className="tl-intent-id">{row.id}</span>
                  <span className={`tl-chip intent-${row.status}`}>{t(`intent.status.${row.status}`)}</span>
                  <span className="reason-cycle-meta">
                    g{row.generation} · {formatDurationMs(row.durationMs)}
                    {row.trigger ? ` · ${row.trigger}` : ""}
                    {row.auditCount > 0 ? ` · ${t("reason.auditShort", { n: row.auditCount })}` : ""}
                  </span>
                </div>
              ))}
            </section>
            <div className="swarm-count">{t("swarm.intentQueue")}</div>
            {queue.length === 0 && <div className="swarm-empty">{t("swarm.noIntents")}</div>}
            {queue.map((it) => (
              <div
                className={`sw-intent ${flashIntent === it.id ? "tl-flash" : ""}`.trim()}
                key={it.id}
                id={`sw-intent-${it.id}`}
              >
                <div className="sw-intent-top">
                  <span className="tl-intent-id">{it.id}</span>
                  <span className="sw-intent-goal">{it.goal}</span>
                  {it.priority != null && <span className="tl-priority">{it.priority.toFixed(2)}</span>}
                </div>
                <div className="tl-intent-meta">
                  <span className={`tl-chip intent-${it.status}`}>{t(`intent.status.${it.status}`)}</span>
                  {it.fromFactIds.length > 0 && (
                    <span>{t("timeline.intent.fromFacts", { n: it.fromFactIds.length })}</span>
                  )}
                </div>
              </div>
            ))}
            {dispatched.length > 0 && (
              <>
                <div className="swarm-count">{t("swarm.activeIntents")}</div>
                {dispatched.map((it) => (
                  <div
                    className={`sw-intent ${flashIntent === it.id ? "tl-flash" : ""}`.trim()}
                    key={it.id}
                    id={`sw-intent-${it.id}`}
                  >
                    <div className="sw-intent-top">
                      <span className="tl-intent-id">{it.id}</span>
                      <span className="sw-intent-goal">{it.goal}</span>
                      {it.priority != null && <span className="tl-priority">{it.priority.toFixed(2)}</span>}
                    </div>
                    <div className="tl-intent-meta">
                      <span className={`tl-chip intent-${it.status}`}>{t(`intent.status.${it.status}`)}</span>
                      {it.workerId && (
                        <button type="button" className="evi-link" title={t("evidence.viewWorker")}
                          onClick={() => onOpenWorker(it.workerId!)}>→ {it.workerId}</button>
                      )}
                    </div>
                  </div>
                ))}
              </>
            )}
          </>
        )}

        {tab === "panels" && (
          <div className="swarm-panels">
            {PANEL_VIEWS.map((p) => (
              <a key={p.view} href={detailUrlForRun(runId, p.view)}>
                {t(p.key)}
              </a>
            ))}
          </div>
        )}

        <div className="swarm-attention">
          <div className="swarm-count">{t("swarm.attention")}</div>
          {!hasAttention && <div className="swarm-empty">{t("swarm.allClear")}</div>}
          {deck.hitlRequests.map((r, i) => (
            <HitlCard
              key={r.id}
              req={r}
              first={i === 0}
              onAnswer={(opt) => { onCommand("global", "submit", opt); onHitlAnswered?.(); }}
              onDismiss={() => { onCommand("global", "dismiss", ""); onHitlAnswered?.(); }}
            />
          ))}
        </div>
      </div>
    </aside>
  );
}
