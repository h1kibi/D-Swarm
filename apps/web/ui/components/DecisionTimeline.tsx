"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { DeckState } from "@/lib/events";
import { useT } from "@/lib/i18n";
import type { Stage } from "@/lib/normalize";
import {
  buildTimeline,
  stageAnchorId,
  stageAnchors,
  type TimelineItem,
} from "@/lib/timeline";
import type { ReasonCycleView, ReasonIntentView } from "@/lib/reason";
import { Icon } from "@/components/Icon";
import { PanelSkeleton } from "@/components/Skeleton";
import { workerColor, workerInitial, workerShortLabel } from "@/lib/workers";

/**
 * Decision Timeline (docs/07 §5.4) — the Command-center's main axis, replacing
 * the conversation spine. Time-ordered Recon / Reason cycle / Dispatch / Fact /
 * Provenance / HITL / flag / directive events; chat messages are ONE event
 * class here, and worker raw tool output never enters the body — each lane
 * shows as a collapsed summary that jumps to the worker detail panel.
 *
 * Assembly is pure (lib/timeline.ts); this component is a thin renderer with
 * stick-to-bottom scrolling and stage anchors for the Stage Rail jump.
 */

function clock(tsMs: number): string {
  if (!tsMs) return "--:--:--";
  const d = new Date(tsMs);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDurationMs(ms?: number): string {
  if (ms == null) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function IntentStatusChip({ status }: { status: ReasonIntentView["status"] }) {
  const t = useT();
  return <span className={`tl-chip intent-${status}`}>{t(`intent.status.${status}`)}</span>;
}

function IntentRow({ intent }: { intent: ReasonIntentView }) {
  const t = useT();
  return (
    <div className="tl-intent">
      <div className="tl-intent-top">
        <span className="tl-intent-id">{intent.id}</span>
        {intent.mode && <span className="tl-chip">{intent.mode}</span>}
        <span className="tl-intent-goal">{intent.goal}</span>
        {intent.priority != null && (
          <span className="tl-priority" title="priority">P{intent.priority.toFixed(2)}</span>
        )}
        <IntentStatusChip status={intent.status} />
      </div>
      <div className="tl-intent-meta">
        {intent.fromFactIds.length > 0 && (
          <span>{t("timeline.intent.fromFacts", { n: intent.fromFactIds.length })}</span>
        )}
        {intent.surfaceTarget && <span>{t("timeline.intent.surface")}: {intent.surfaceTarget}</span>}
        {intent.taskKind && <span>{t("timeline.intent.task")}: {intent.taskKind}</span>}
        {intent.workerId && <span>→ {intent.workerId}</span>}
        {intent.skipReason && <span className="tl-muted">{intent.skipReason}</span>}
      </div>
    </div>
  );
}

function CycleCard({
  cycle,
  open,
  onToggle,
}: {
  cycle: ReasonCycleView;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  return (
    <div className={`tl-cycle st-${cycle.status}`}>
      <button
        type="button"
        className="tl-cycle-head"
        onClick={onToggle}
        aria-expanded={open}
        title={t(open ? "timeline.cycle.collapse" : "timeline.cycle.expand")}
      >
        <Icon name={open ? "chevronDown" : "chevronRight"} size={13} />
        <span className="tl-item-title">{t("timeline.cycle", { n: cycle.generation })}</span>
        <span className={`tl-chip cycle-${cycle.status}`}>{t(`timeline.cycleStatus.${cycle.status}`)}</span>
        {cycle.durationMs != null && <span className="tl-duration">{fmtDurationMs(cycle.durationMs)}</span>}
      </button>
      {cycle.trigger && <div className="tl-sub">{t("timeline.cycle.trigger", { reason: cycle.trigger })}</div>}
      {open && (
        <div className="tl-cycle-body">
          {cycle.audits.length > 0 && (
            <div className="tl-sec">
              <div className="tl-sec-head">{t("timeline.cycle.audit")}</div>
              {cycle.audits.map((a, i) => (
                <div className="tl-audit" key={`a${i}`}>✓ {a}</div>
              ))}
            </div>
          )}
          {cycle.intents.length > 0 && (
            <div className="tl-sec">
              <div className="tl-sec-head">{t("timeline.cycle.decisions")}</div>
              {cycle.intents.map((it) => <IntentRow key={it.id} intent={it} />)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TimelineRow({
  item,
  anchorId,
  onOpenWorker,
  onOpenEvidence,
}: {
  item: TimelineItem;
  anchorId?: string;
  onOpenWorker: (id: string) => void;
  /** fact provenance click → the Evidence panel (cross-view link, Phase 5). */
  onOpenEvidence?: () => void;
}) {
  const t = useT();
  let body: React.ReactNode = null;

  switch (item.kind) {
    case "recon":
      body = (
        <>
          <span className="tl-item-title">
            {t(item.recon.status === "running" ? "timeline.recon.running" : "timeline.recon.done")}
          </span>
          <span className="tl-sub">
            {item.recon.newFindings != null && t("timeline.recon.findings", { n: item.recon.newFindings })}
            {item.recon.durationMs != null && ` · ${fmtDurationMs(item.recon.durationMs)}`}
          </span>
        </>
      );
      break;
    case "cycle":
      // rendered by the parent (expandable card needs state)
      return null;
    case "dispatch":
      body = (
        <>
          <span className="tl-item-title">{t("timeline.dispatch")}</span>
          <span className="tl-dispatch-line">
            {item.intent.id} →{" "}
            {item.intent.workerId ? (
              <button type="button" className="evi-link" title={t("evidence.viewWorker")}
                onClick={() => onOpenWorker(item.intent.workerId!)}><b>{item.intent.workerId}</b></button>
            ) : <b>?</b>}
            {item.intent.priority != null && <span className="tl-priority">P{item.intent.priority.toFixed(2)}</span>}
          </span>
          <span className="tl-sub">
            {item.intent.profile && <span>{item.intent.profile} · </span>}
            {item.intent.goal}
          </span>
          {item.intent.dispatchReason && (
            <span className="tl-sub tl-muted">{t("timeline.dispatch.reason")}: {item.intent.dispatchReason}</span>
          )}
        </>
      );
      break;
    case "fact":
      body = (
        <>
          <span className={`tl-chip ${item.fact.verified ? "ok" : "candidate"}`}>
            {t(item.fact.verified ? "timeline.fact.verified" : "timeline.fact.candidate")}
          </span>
          <span className="tl-fact-text">{item.fact.summary || item.fact.fact}</span>
          <span className="tl-sub tl-muted">
            {t("timeline.fact.provenance")}:{" "}
            <button type="button" className="evi-link" title={t("evidence.viewWorker")}
              onClick={() => onOpenWorker(item.fact.actor)}>{item.fact.actor}</button>
            {item.fact.factSeq != null && (
              <>
                {" · "}
                <button type="button" className="evi-link" title={t("timeline.openEvidence")}
                  onClick={() => onOpenEvidence?.()}>#{item.fact.factSeq}</button>
              </>
            )}
          </span>
        </>
      );
      break;
    case "flag":
      body = (
        <>
          <span className="tl-item-title">{t("timeline.flag")}</span>
          <span className="tl-flag">{item.flag}</span>
          {item.actor && <span className="tl-sub tl-muted">{item.actor}</span>}
        </>
      );
      break;
    case "hitl":
      body = (
        <>
          <span className="tl-chip warn">{t("timeline.hitl")}</span>
          <span className="tl-fact-text">{item.req.promptZh || item.req.prompt}</span>
          {item.req.worker && <span className="tl-sub tl-muted">{item.req.worker}</span>}
        </>
      );
      break;
    case "directive":
      body = (
        <>
          <span className="tl-item-title">{t("timeline.directive")}</span>
          <span className={`tl-chip dir-${item.lifecycle}`}>{t(`directive.lifecycle.${item.lifecycle}`)}</span>
          <span className="tl-fact-text">{item.directive.action}: {item.directive.text}</span>
          {item.directive.boundWorker && (
            <span className="tl-sub tl-muted">→ {item.directive.boundWorker}</span>
          )}
        </>
      );
      break;
    case "worker": {
      const lane = item.lane;
      const label = workerShortLabel(lane.solverId, lane.engine);
      const color = workerColor(lane.solverId, lane.engine);
      body = (
        <>
          <span className="tl-avatar" style={{ background: color }} aria-hidden="true">
            {workerInitial(lane.solverId)}
          </span>
          <span className="tl-item-title">{t("timeline.workerOutput")} · {label}</span>
          <span className="tl-sub">
            {t("timeline.toolCalls", { n: lane.toolLines.length })}
            {lane.status && ` · ${lane.status}`}
          </span>
          <button type="button" className="tl-open" onClick={() => onOpenWorker(lane.solverId)}>
            {t("timeline.openWorker")}
          </button>
        </>
      );
      break;
    }
    case "legacy":
      body = (
        <>
          <span className="tl-chip legacy">{t(item.i18nKey)}</span>
          {item.label && <span className="tl-sub tl-muted">{item.label}</span>}
        </>
      );
      break;
    case "chat": {
      const m = item.message;
      const who = m.role === "human"
        ? t("timeline.chat.operator")
        : m.role === "system"
          ? t("timeline.chat.system")
          : (m.solverId || "");
      const text = m.i18nKey ? t(m.i18nKey, m.i18nVars) : m.content;
      body = (
        <>
          <span className={`tl-chip chat-${m.role}`}>{who}</span>
          <span className="tl-fact-text">{text}</span>
        </>
      );
      break;
    }
  }

  return (
    // every row carries a stable `tl-${item.id}` DOM anchor so other views
    // (evidence cards, worker cards) can scroll/flash it (Phase 5 cross-links);
    // the Stage Rail anchor lives on an inner marker span.
    <div className={`tl-item tl-${item.kind}`} id={`tl-${item.id}`} data-stage={item.stage}>
      {anchorId && <span className="tl-stage-anchor" id={anchorId} aria-hidden="true" />}
      <span className="tl-time">{clock(item.ts)}</span>
      <div className="tl-body">{body}</div>
    </div>
  );
}

export function DecisionTimeline({
  deck,
  loading,
  jump,
  onOpenWorker,
  onOpenEvidence,
}: {
  deck: DeckState;
  loading: boolean;
  /** Stage Rail click target — {stage, nonce}; the nonce re-fires a re-click. */
  jump?: { stage: Stage; nonce: number } | null;
  onOpenWorker: (id: string) => void;
  /** fact `#seq` click → open the Evidence panel (cross-view link, Phase 5). */
  onOpenEvidence?: () => void;
}) {
  const t = useT();
  const items = useMemo(() => buildTimeline(deck), [deck]);
  const anchors = useMemo(() => stageAnchors(items), [items]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [openCycles, setOpenCycles] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items.length, deck.finished]);

  // Stage Rail jump → scroll to the first item of that stage (§5.3).
  useEffect(() => {
    if (!jump) return;
    const el = document.getElementById(stageAnchorId(jump.stage));
    if (el) {
      stick.current = false;
      el.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [jump]);

  const toggleCycle = (id: string) =>
    setOpenCycles((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const anchorFor = (item: TimelineItem): string | undefined =>
    item.stage !== "legacy" && anchors[item.stage] === item.id
      ? stageAnchorId(item.stage)
      : undefined;

  return (
    <section className="dectl motion-run-enter" aria-label={t("timeline.aria")}>
      <div
        className="dectl-scroll"
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 70;
        }}
      >
        {loading ? (
          <div className="panel-scroll" aria-busy="true"><PanelSkeleton rows={5} /></div>
        ) : items.length === 0 ? (
          <div className="dectl-empty">{t("timeline.empty")}</div>
        ) : (
          items.map((item) =>
            item.kind === "cycle" ? (
              <div className="tl-item tl-cycle-row" key={item.id} id={`tl-${item.id}`} data-stage={item.stage}>
                {anchorFor(item) && <span className="tl-stage-anchor" id={anchorFor(item)} aria-hidden="true" />}
                <span className="tl-time">{clock(item.ts)}</span>
                <div className="tl-body">
                  <CycleCard
                    cycle={item.cycle}
                    open={openCycles.has(item.cycle.id)}
                    onToggle={() => toggleCycle(item.cycle.id)}
                  />
                </div>
              </div>
            ) : (
              <TimelineRow key={item.id} item={item} anchorId={anchorFor(item)} onOpenWorker={onOpenWorker} onOpenEvidence={onOpenEvidence} />
            ),
          )
        )}
      </div>
    </section>
  );
}
