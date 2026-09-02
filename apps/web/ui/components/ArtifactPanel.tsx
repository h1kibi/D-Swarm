"use client";

import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { DeckState, GraphNode, reasonStopZh } from "@/lib/events";
import { useT } from "@/lib/i18n";
import { formatDurationMs } from "@/lib/reason";
import { GraphView } from "@/components/GraphView";
import { NodeInspector } from "@/components/NodeInspector";
import { Blackboard } from "@/components/Blackboard";
import { WorkerLanes } from "@/components/WorkerLanes";
import { ActivityStream } from "@/components/ActivityStream";
import { EvidenceChain } from "@/components/EvidenceChain";
import { PanelSkeleton } from "@/components/Skeleton";
import { Icon } from "@/components/Icon";
import { apiFetch } from "@/lib/useRun";

/** The five secondary detail panels (the conversation stays the primary view). */
export type ArtifactView =
  | "graph" | "blackboard" | "workers" | "timeline" | "evidence"
  | "findings" | "credentials" | "pocs" | "routes" | "directives";

const TABS: { view: ArtifactView; key: string }[] = [
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

type CredentialRow = { entity: string; value: string; seq?: number };

function EmptyPanel({ label }: { label: string }) {
  return <div className="artifact-list-empty">{label}</div>;
}

export function ReviewFindingsPanel({ deck }: { deck: DeckState }) {
  const t = useT();
  const rows = deck.blackboard.reviewFindings ?? [];
  const claims = deck.blackboard.unverifiedFlags ?? [];
  if (!rows.length && !claims.length) return <EmptyPanel label={t("panel.empty")} />;
  return (
    <div className="artifact-list">
      {claims.slice().reverse().map((c) => (
        <div className="artifact-row" key={c.id}>
          <div className="artifact-row-top">
            <span className={`artifact-badge ${c.status === "invalidated" ? "bad" : "sev-warn"}`}>{t("panel.flagClaim")}</span>
            <span className="artifact-row-title">{c.flag}</span>
            {c.audit?.verdict && <span className="artifact-chip">{c.audit.verdict}</span>}
          </div>
          {c.reason && <div className="artifact-row-body">{c.reason}</div>}
          {c.audit?.reason && <div className="artifact-row-body"><b>{t("panel.flagAudit")}:</b> {c.audit.reason}</div>}
          {c.audit?.recommendedAction && <div className="artifact-row-body"><b>{t("panel.recommendedAction")}:</b> {c.audit.recommendedAction}</div>}
          <div className="artifact-row-meta">{[c.actor, c.intentId, c.artifactId, c.seq ? `#${c.seq}` : ""].filter(Boolean).join(" · ")}</div>
        </div>
      ))}
      {rows.slice().reverse().map((r) => (
        <div className="artifact-row" key={r.id}>
          <div className="artifact-row-top">
            <span className={`artifact-badge sev-${r.severity}`}>{r.severity}</span>
            <span className="artifact-row-title">{r.kind}</span>
            {r.routeHash && <span className="artifact-chip">{r.routeHash}</span>}
          </div>
          <div className="artifact-row-body">{r.summary}</div>
          <div className="artifact-row-meta">{r.actor}</div>
        </div>
      ))}
    </div>
  );
}

export function PocsPanel({ deck }: { deck: DeckState }) {
  const t = useT();
  const rows = deck.blackboard.pocs ?? [];
  if (!rows.length) return <EmptyPanel label={t("panel.empty")} />;
  return (
    <div className="artifact-list">
      {rows.slice().reverse().map((p) => (
        <div className="artifact-row" key={p.id}>
          <div className="artifact-row-top">
            <span className="artifact-badge">{p.status}</span>
            <span className="artifact-row-title">{p.name || p.id}</span>
          </div>
          {p.entryCommand && <code className="artifact-code">{p.entryCommand}</code>}
          {p.note && <div className="artifact-row-body">{p.note}</div>}
          <div className="artifact-row-meta">{[p.worker, p.intentId, p.path].filter(Boolean).join(" · ")}</div>
        </div>
      ))}
    </div>
  );
}

export function RoutesPanel({ deck }: { deck: DeckState }) {
  const t = useT();
  const routes = deck.blackboard.suppressedRoutes ?? [];
  const branches = deck.blackboard.branches ?? [];
  const loop = deck.reasonLoop;
  const intentsById = new Map(deck.blackboard.intents.map((i) => [i.id, i]));
  const hasExploration = !!loop.recon || loop.cycles.length > 0 || !!loop.stopReason;
  if (!hasExploration && !routes.length && !branches.length) {
    return (
      <div className="explore-empty">
        <span className="rail-noresult-ico" aria-hidden="true"><Icon name="crosshair" size={18} /></span>
        <span className="rail-noresult-title">{t("explore.empty")}</span>
        <span className="rail-noresult-hint">{t("explore.emptyHint")}</span>
      </div>
    );
  }
  return (
    <div className="artifact-list explore">
      {/* 结局 — the first thing an operator wants to know */}
      {(loop.stopReason || loop.solved) && (
        <div className="explore-outcome">
          <span className={`artifact-badge ${loop.solved ? "ok" : "sev-warn"}`}>{t("explore.stop")}</span>
          <span className="explore-outcome-text">
            {loop.solved
              ? t("reason.loopSolved")
              : t("explore.loopDone", { reason: reasonStopZh(loop.stopReason) })}
          </span>
        </div>
      )}

      {/* 侦察段 */}
      {loop.recon && (
        <div className="explore-block">
          <div className="explore-block-h">
            {t("explore.recon")}
            <span className={`artifact-chip ${loop.recon.status === "completed" ? "ok" : ""}`}>
              {t(loop.recon.status === "completed" ? "reason.reconDone" : "reason.reconRunning")}
            </span>
            {loop.recon.durationMs != null && (
              <span className="explore-meta">{formatDurationMs(loop.recon.durationMs)}</span>
            )}
          </div>
          {loop.recon.newFindings != null && (
            <div className="explore-note">{t("reason.reconFindings", { n: loop.recon.newFindings })}</div>
          )}
        </div>
      )}

      {/* 规划循环：每周期 = 规划意图 → 派工 → 执行结果 → 审计 */}
      {loop.cycles.map((cycle, idx) => (
        <div className="explore-block" key={cycle.id}>
          <div className="explore-block-h">
            {t("explore.cycle", { n: idx + 1 })}
            <span className="artifact-chip">{t(`explore.cycleStatus.${cycle.status}`)}</span>
            {cycle.durationMs != null && <span className="explore-meta">{formatDurationMs(cycle.durationMs)}</span>}
            {cycle.planner && <span className="explore-meta">{cycle.planner}</span>}
            {cycle.goalMet && <span className="artifact-chip ok">{t("reason.goalMet")}</span>}
          </div>
          {cycle.intents.length > 0 && (
            <div className="explore-intents">
              {cycle.intents.map((it) => {
                const gist = intentsById.get(it.id)?.summary?.trim();
                const title = gist || it.goal || it.id;
                return (
                  <div className="explore-intent" key={`${cycle.id}-${it.id}`}>
                    <div className="explore-intent-top">
                      <span className={`explore-status st-${it.status}`}>{t(`explore.intentStatus.${it.status}`)}</span>
                      <span className="explore-intent-goal" title={it.goal || it.id}>{title}</span>
                    </div>
                    <div className="explore-intent-meta">
                      {it.workerId && <span>{t("explore.dispatchTo", { worker: it.workerId })}</span>}
                      {it.mode && <span>{t("explore.modePriority", { mode: it.mode, p: it.priority ?? "-" })}</span>}
                      {it.profile && <span>{it.profile}</span>}
                      {it.skipReason && <span className="explore-warn">{it.skipReason}</span>}
                      {it.status === "failed" && it.flag === undefined && it.dispatchReason && (
                        <span>{it.dispatchReason}</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {cycle.audits.length > 0 && (
            <div className="explore-audits">
              <div className="explore-note-h">{t("explore.audits", { n: cycle.audits.length })}</div>
              {cycle.audits.map((a, i) => <div className="explore-note" key={i}>{a}</div>)}
            </div>
          )}
        </div>
      ))}

      {/* 死路 / 分支 */}
      {routes.length > 0 && (
        <div className="explore-block">
          <div className="explore-block-h">{t("explore.deadends")} <span className="artifact-chip">{routes.length}</span></div>
          {routes.slice().reverse().map((r) => (
            <div className="explore-intent" key={`route-${r.routeHash}`}>
              <div className="explore-intent-top">
                <span className={`explore-status ${r.reopened ? "st-completed" : "st-failed"}`}>
                  {r.reopened ? t("panel.reopened") : t("panel.suppressed")}
                </span>
                <span className="explore-intent-goal" title={r.routeHash}>{r.label || r.routeHash}</span>
              </div>
              {r.reason && <div className="explore-intent-meta">{r.reason}</div>}
            </div>
          ))}
        </div>
      )}
      {branches.length > 0 && (
        <div className="explore-block">
          <div className="explore-block-h">{t("explore.branches")} <span className="artifact-chip">{branches.length}</span></div>
          {branches.slice().reverse().map((b) => (
            <div className="explore-intent" key={`branch-${b.branchId}`}>
              <div className="explore-intent-top">
                <span className={`explore-status ${b.status === "resolved" ? "st-completed" : "st-proposed"}`}>
                  {b.status === "resolved" ? t("explore.intentStatus.completed") : t("explore.intentStatus.proposed")}
                </span>
                <span className="explore-intent-goal">{b.title || b.branchId}</span>
              </div>
              <div className="explore-intent-meta">{[b.actor, b.branchId].filter(Boolean).join(" · ")}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function DirectivesPanel({ deck }: { deck: DeckState }) {
  const t = useT();
  const rows = deck.blackboard.directives ?? [];
  const lifecycle = deck.operatorDirectives ?? [];
  if (!rows.length && !lifecycle.length) return <EmptyPanel label={t("panel.empty")} />;
  return (
    <div className="artifact-list">
      {lifecycle.slice().reverse().map((d) => (
        <div className="artifact-row" key={`op-${d.id}`}>
          <div className="artifact-row-top">
            <span className="artifact-badge">{d.status}</span>
            <span className="artifact-row-title">{d.action}</span>
          </div>
          <div className="artifact-row-body">{d.text}</div>
          {d.boundWorker && <div className="artifact-row-meta">{d.boundWorker}</div>}
        </div>
      ))}
      {rows.slice().reverse().map((d, i) => (
        <div className="artifact-row" key={`bb-${i}-${d.ts}`}>
          <div className="artifact-row-top">
            <span className="artifact-badge">{d.action}</span>
            <span className="artifact-row-title">{d.actor}</span>
          </div>
          <div className="artifact-row-body">{d.directive}</div>
        </div>
      ))}
    </div>
  );
}

export function CredentialsPanel({ runId }: { runId: string }) {
  const t = useT();
  const [rows, setRows] = useState<CredentialRow[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    apiFetch(`/api/runs/${encodeURIComponent(runId)}/credentials`)
      .then((r) => r.ok ? r.json() : { credentials: [] })
      .then((j) => { if (alive) setRows(Array.isArray(j.credentials) ? j.credentials : []); })
      .catch(() => { if (alive) setRows([]); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [runId]);
  if (loading) return <div className="panel-scroll" aria-busy="true"><PanelSkeleton rows={3} /></div>;
  if (!rows.length) return <EmptyPanel label={t("panel.empty")} />;
  return (
    <div className="artifact-list">
      {rows.map((c) => (
        <div className="artifact-row" key={`${c.entity}-${c.seq ?? c.value}`}>
          <div className="artifact-row-top">
            <span className="artifact-badge ok">cred</span>
            <span className="artifact-row-title">{c.entity}</span>
            {c.seq && <span className="artifact-chip">#{c.seq}</span>}
          </div>
          <code className="artifact-code">{c.value}</code>
        </div>
      ))}
    </div>
  );
}

/**
 * The secondary-panel canvas. Opens beside the conversation (replacing the
 * inspector column) and shows ONE detail view at a time: the live fact-graph,
 * the collaborative blackboard, the rich worker lanes, the raw activity stream,
 * or the evidence chain. The Decision Timeline is never displaced.
 */
export function ArtifactPanel({
  open,
  width,
  view,
  deck,
  running,
  loading,
  selected,
  onSelect,
  onView,
  onClose,
  onResize,
  minWidth,
  maxWidth,
  defaultWidth,
  onSpawnWorker,
  onKillWorker,
  focusWorker,
  onOpenWorker,
}: {
  open: boolean;
  width: number;
  view: ArtifactView;
  deck: DeckState;
  running: boolean;
  loading: boolean;
  selected: GraphNode | null;
  onSelect: (n: GraphNode | null) => void;
  onView: (v: ArtifactView) => void;
  onClose: () => void;
  onResize: (width: number) => void;
  minWidth: number;
  maxWidth: number;
  defaultWidth: number;
  onSpawnWorker: (engine?: string) => void;
  onKillWorker: (id: string) => void;
  // seed the WorkerLanes focus filter to a single worker (roster row click).
  focusWorker?: { id: string; nonce: number } | null;
  // evidence-card provenance click → jump to that worker's detail (Phase 5).
  onOpenWorker?: (id: string) => void;
}) {
  const t = useT();
  const [resizing, setResizing] = useState(false);
  const cleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => () => cleanupRef.current?.(), []);

  const resizeToClientX = (clientX: number) => {
    if (typeof window === "undefined") return;
    onResize(window.innerWidth - clientX);
  };

  const startResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!open) return;
    e.preventDefault();
    e.stopPropagation();
    cleanupRef.current?.();
    setResizing(true);
    document.body.classList.add("artifact-resizing");

    const onMove = (ev: PointerEvent) => {
      ev.preventDefault();
      resizeToClientX(ev.clientX);
    };
    const stop = () => {
      setResizing(false);
      document.body.classList.remove("artifact-resizing");
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      cleanupRef.current = null;
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    cleanupRef.current = stop;
    resizeToClientX(e.clientX);
  };

  const onResizeKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!open) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      onResize(width + (e.shiftKey ? 32 : 12));
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      onResize(width - (e.shiftKey ? 32 : 12));
    } else if (e.key === "Home") {
      e.preventDefault();
      onResize(minWidth);
    } else if (e.key === "End") {
      e.preventDefault();
      onResize(maxWidth);
    } else if (e.key === "Enter") {
      e.preventDefault();
      onResize(defaultWidth);
    }
  };

  return (
    <div
      className={`artifact motion-artifact ${resizing ? "resizing" : ""}`}
      // flex-basis MUST be explicit (not the `flex:none` → basis:auto from CSS):
      // inside the flex row, basis:auto let the panel collapse to ~1px even with
      // shrink:0. Pinning basis = width makes it hold its size and push .convo over.
      style={{ width: (open ? width : 0) + "px", flexBasis: (open ? width : 0) + "px" }}
      role="region"
      aria-label={t("a11y.artifact")}
    >
      {open && (
        <>
          <div
            className="artifact-resizer"
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
          <div className="artifact-head">
            <div className="tabs" role="tablist" aria-label={t("a11y.panelTabs")}>
              {TABS.map((tab) => (
                <button key={tab.view} type="button" role="tab" aria-selected={view === tab.view}
                  className={`small ${view === tab.view ? "on" : ""}`}
                  onClick={() => onView(tab.view)}>{t(tab.key)}</button>
              ))}
            </div>
            <span className="a-title">{deck.challengeName}</span>
            <span className="spacer" />
            <button className="x" onClick={onClose} title={t("art.closeCanvas")} aria-label={t("art.closeCanvas")}><Icon name="x" size={15} /></button>
          </div>

          <div className="artifact-body">
            {/* Keyed by loading/view so React remounts on every tab switch and the
                .artifact-view mount animation (CSS) re-fires as a quick cross-fade.
                The heavy canvases (cytoscape GraphView / react-flow Blackboard) are
                rendered INSIDE this keyed branch — they already unmount when you
                switch tabs, so the key adds the fade without any extra teardown. */}
            <div className="artifact-view motion-artifact-view" key={loading ? "loading" : view}>
              {loading ? (
                <div className="panel-scroll" aria-busy="true">
                  <PanelSkeleton rows={4} />
                </div>
              ) : view === "graph" ? (
                <>
                  <GraphView model={deck.model} onSelect={onSelect} />
                  {selected && (
                    <div className="insp-float">
                      <NodeInspector node={selected} onClose={() => onSelect(null)} />
                    </div>
                  )}
                </>
              ) : view === "blackboard" ? (
                <Blackboard bb={deck.blackboard} runId={deck.runId} />
              ) : view === "workers" ? (
                <WorkerLanes deck={deck} running={running} focusWorker={focusWorker} onSpawnWorker={onSpawnWorker} onKillWorker={onKillWorker} />
              ) : view === "timeline" ? (
                <ActivityStream deck={deck} />
              ) : view === "evidence" ? (
                <EvidenceChain deck={deck} onOpenWorker={onOpenWorker} />
              ) : view === "findings" ? (
                <ReviewFindingsPanel deck={deck} />
              ) : view === "credentials" ? (
                <CredentialsPanel runId={deck.runId} />
              ) : view === "pocs" ? (
                <PocsPanel deck={deck} />
              ) : view === "routes" ? (
                <RoutesPanel deck={deck} />
              ) : (
                <DirectivesPanel deck={deck} />
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
