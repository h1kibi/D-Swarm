"use client";

import { useMemo } from "react";
import type { DeckState } from "@/lib/events";
import type { RunSummary, RunObservabilitySnapshot } from "@/lib/useRun";
import { hasTargetDispatchMismatch } from "@/lib/useRun";
import { groupTargetRunsByDirection, type TargetProjection } from "@/lib/targetProjection";
import { Icon } from "@/components/Icon";
import { useLang } from "@/lib/i18n";
import type { DetailView } from "@/lib/runRoute";
import { RunBindingInspector } from "@/components/RunBindingInspector";
import { ObserverSummaryCard } from "@/components/ObserverSummaryCard";

const ZH = {
  solved: "\u5df2\u89e3\u51b3", draft: "\u8349\u7a3f", queued: "\u6392\u961f\u4e2d", running: "\u8fd0\u884c\u4e2d",
  paused: "\u5df2\u6682\u505c", reason_blocked: "Reason \u5df2\u963b\u65ad", degraded: "\u8fd0\u884c\u5df2\u964d\u7ea7", fallback_active: "\u5df2\u542f\u7528 fallback", finished: "\u5df2\u5b8c\u6210", failed: "\u5931\u8d25", cancelled: "\u5df2\u53d6\u6d88",
  select: "\u9009\u62e9\u4e00\u4e2a Target \u5f00\u59cb", empty: "Target \u4fdd\u5b58\u9898\u76ee\u4e0a\u4e0b\u6587\uff0c\u6bcf\u4e2a Run \u90fd\u662f\u4e00\u6b21\u72ec\u7acb\u7684\u667a\u80fd\u4f53\u96c6\u7fa4\u5c1d\u8bd5\u3002",
  create: "\u521b\u5efa Target / Run", newRun: "\u65b0\u5efa Run", deleteRun: "\u5220\u9664 Run", knowledge: "\u5df2\u9a8c\u8bc1\u77e5\u8bc6", facts: "\u6761\u53ef\u8ffd\u6eaf\u4e8b\u5b9e",
  context: "\u9898\u76ee\u4e0a\u4e0b\u6587\u53ef\u5728\u8be6\u60c5\u4e2d\u67e5\u770b", structure: "\u63a2\u7d22\u7ed3\u6784", attempts: "\u6b21\u5c1d\u8bd5", persistent: "\u6301\u4e45\u5316\u9898\u76ee\u5bf9\u8c61", swarms: "\u667a\u80fd\u4f53\u96c6\u7fa4\u5c1d\u8bd5", workers: "\u4e2a Worker", evidence: "\u8bc1\u636e", timeline: "\u65f6\u95f4\u7ebf", details: "\u8be6\u60c5", routes: "\u8def\u7ebf", ledger: "\u8d26\u672c", intents: "Intent", tokens: "\u4ee4\u724c", primary: "\u4e3b\u65b9\u5411", noRuns: "\u8fd8\u6ca1\u6709 Run", runStack: "Run \u5217\u8868", selectedRun: "\u5f53\u524d Run", isolated: "\u72ec\u7acb\u8c03\u5ea6 \u00b7 \u8d26\u672c \u00b7 \u56de\u653e",
};

function statusLabel(status: RunSummary["status"], solved: boolean, zh: boolean): string {
  if (solved) return zh ? ZH.solved : "SOLVED";
  const labels: Record<RunSummary["status"], string> = {
    draft: zh ? ZH.draft : "DRAFT", queued: zh ? ZH.queued : "QUEUED", running: zh ? ZH.running : "RUNNING",
    paused: zh ? ZH.paused : "PAUSED", reason_blocked: zh ? ZH.reason_blocked : "REASON BLOCKED",
    degraded: zh ? ZH.degraded : "DEGRADED", fallback_active: zh ? ZH.fallback_active : "FALLBACK ACTIVE",
    solved: zh ? ZH.solved : "SOLVED", finished: zh ? ZH.finished : "FINISHED",
    failed: zh ? ZH.failed : "FAILED", cancelled: zh ? ZH.cancelled : "CANCELLED",
  };
  return labels[status];
}
function targetStatusLabel(status: TargetProjection["status"], zh: boolean): string {
  if (!zh) return status.toUpperCase();
  return ({ active: "进行中", waiting: "等待中", reason_blocked: "Reason 已阻断", degraded: "运行已降级", fallback_active: "已启用 fallback", solved: "已解决", failed: "需检查", archived: "已归档", idle: "未开始" } as Record<string, string>)[status] || status;
}

function fmtTime(value?: number): string {
  if (!value) return "--";
  const date = new Date(value > 1e12 ? value : value * 1000);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function directionFor(run: RunSummary, zh: boolean): string {
  const raw = (run.name || "").trim();
  const parts = raw.split(/\s+[|/:.-]\s+/);
  return parts.length > 1 ? parts[0] : run.category || (zh ? "\u901a\u7528\u63a2\u7d22" : "General exploration");
}

export function TargetWorkspace({
  target, deck, activeRunId, onSelectRun, onNewRun, onDeleteRun, onOpenDetails, onOpenKnowledge, onOpenTargetDetails, observability,
}: {
  target: TargetProjection | undefined;
  deck: DeckState;
  activeRunId: string;
  onSelectRun: (runId: string) => void;
  onNewRun: () => void;
  onDeleteRun: (run: RunSummary) => void;
  onOpenDetails: (view: DetailView) => void;
  onOpenKnowledge?: () => void;
  onOpenTargetDetails?: () => void;
  observability?: RunObservabilitySnapshot | null;
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const runs = useMemo(() => target?.runs ?? [], [target]);
  const activeRun = runs.find((run) => run.run_id === activeRunId) ?? runs[0];
  const activeRunMismatch = hasTargetDispatchMismatch(activeRun);
  const directionMap = useMemo(() => groupTargetRunsByDirection(target, runs, zh), [runs, target, zh]);
  const verifiedCount = deck.sharedGraph.verified.length + deck.blackboard.facts.filter((fact) => fact.verified).length;
  const totalTokens = deck.tokensIn + deck.tokensOut;

  if (!target) return (
    <section className="target-workspace target-workspace-empty">
      <div className="target-empty-hero">
        <Icon name="target" size={30} />
        <h1>{zh ? ZH.select : "Select a target to begin"}</h1>
        <p>{zh ? ZH.empty : "A target persists the problem while each run is an independent swarm attempt."}</p>
        <button className="target-primary-button" onClick={onNewRun}><Icon name="target" size={15} />{zh ? ZH.create : "Create target / run"}</button>
      </div>
    </section>
  );

  return (
    <section className="target-workspace">
      <header className="target-workspace-header">
        <div className="target-breadcrumb"><span className="eyebrow">{zh ? "目标" : "TARGET"}</span><span>/</span><span>{target.category.toUpperCase()}</span></div>
        <div className="target-title-row">
          <div><h1>{target.name}</h1><p>{deck.target || (zh ? ZH.context : "Target context available in details")}</p></div>
          <div className={`target-status-pill ${target.status}`}><i />{targetStatusLabel(target.status, zh)}</div>
        </div>
        <div className="target-toolbar">
          <button className="target-primary-button" onClick={onNewRun}><Icon name="play" size={14} />{zh ? ZH.newRun : "New run"}</button>
          <button className="target-ghost-button" onClick={onOpenTargetDetails}><Icon name="eye" size={14} />{zh ? ZH.details : "Details"}</button>
          <button className="target-ghost-button" onClick={onOpenKnowledge}><Icon name="board" size={14} />{zh ? "知识" : "Knowledge"}</button>
          <span className="target-toolbar-spacer" /><span className="target-metric"><b>{target.runs.length}</b> {zh ? "个 Run" : "runs"}</span><span className="target-metric"><b>{totalTokens > 0 ? `${(totalTokens / 1000).toFixed(1)}k` : "0"}</b> {zh ? ZH.tokens : "tokens"}</span><span className="target-metric"><b>${deck.usd.toFixed(2)}</b></span>
        </div>
      </header>

      <div className="target-overview-grid">
        <div className="relationship-card">
          <div className="target-section-head"><div><span className="eyebrow">{zh ? "结构" : "STRUCTURE"}</span><h2>{zh ? ZH.structure : "Exploration structure"}</h2></div><span className="target-section-note">Target -&gt; Direction -&gt; Run</span></div>
          <div className="relationship-map" role="list" aria-label={zh ? "Target、方向与 Run 的关系" : "Target, direction and run relationship"}>
            <div className="relationship-root" role="listitem"><span className="relationship-node-icon"><Icon name="target" size={16} /></span><div><strong>{target.name}</strong><span>{zh ? ZH.persistent : "persistent problem object"}</span></div><em>{target.runs.length} {zh ? ZH.attempts : "attempts"}</em></div>
            <div className="relationship-branches">
              {directionMap.map((direction) => <div className="relationship-direction" key={direction.id}>
                <div className="relationship-line" /><div className="relationship-direction-label"><Icon name="layers" size={14} /><span>{direction.label}</span>{direction.id === target.primaryDirectionId && <b>{zh ? ZH.primary : "PRIMARY"}</b>}</div>
                <div className="relationship-runs">{direction.runs.length ? direction.runs.map((run) => <article role="listitem" className="relationship-run-card" key={run.run_id}><button type="button" className={`relationship-run ${run.run_id === activeRunId ? "selected" : ""}`} onClick={() => onSelectRun(run.run_id)} aria-label={`${run.run_id} ${statusLabel(run.status, run.solved, zh)}`}><span className={`run-status-dot ${run.status}`} /><span><strong>{run.run_id}</strong><small>{statusLabel(run.status, run.solved, zh)}</small></span><Icon name="chevronRight" size={13} /></button><button type="button" className="relationship-run-delete" onClick={() => onDeleteRun(run)} aria-label={`${zh ? ZH.deleteRun : "Delete Run"} ${run.run_id}`} title={zh ? ZH.deleteRun : "Delete Run"}><Icon name="xCircle" size={13} /></button></article>) : <span className="relationship-empty-run">{zh ? ZH.noRuns : "No runs yet"}</span>}</div>
              </div>)}
            </div>
          </div>
        </div>
        <div className="target-signal-card">
          <div className="target-section-head"><div><span className="eyebrow">{zh ? "知识" : "KNOWLEDGE"}</span><h2>{zh ? ZH.knowledge : "Verified knowledge"}</h2></div><Icon name="check" size={16} /></div>
          <div className="knowledge-stat"><strong>{verifiedCount}</strong><span>{zh ? ZH.facts : "traceable facts"}</span></div>
          <p>{zh ? "事实来自真实 Run 输出，并保留 provenance 链路。" : "Facts are projected from real run output and retain provenance."}</p>
          <button className="target-link-button" onClick={onOpenKnowledge}>{zh ? "打开知识" : "Open knowledge"}<Icon name="chevronRight" size={13} /></button>
        </div>
      </div>

      <div className="target-section-head target-runs-head"><div><span className="eyebrow">{zh ? "RUN 列表" : "RUN STACK"}</span><h2>{zh ? ZH.swarms : "Swarm attempts"}</h2></div><span className="target-section-note">{zh ? ZH.isolated : "isolated scheduling - ledger - replay"}</span></div>
      <div className="run-stack">{runs.map((run) => <article className={`run-stack-card ${run.run_id === activeRunId ? "selected" : ""}`} key={run.run_id}><button type="button" className="run-stack-select" onClick={() => onSelectRun(run.run_id)} aria-label={`${run.run_id} ${statusLabel(run.status, run.solved, zh)}`}><div className="run-stack-leading"><span className={`run-status-dot ${run.status}`} /><span className="run-stack-id">{run.run_id}</span><span className={`run-stack-status ${run.status}`}>{statusLabel(run.status, run.solved, zh)}</span></div><strong>{directionFor(run, zh)}</strong><div className="run-stack-meta"><span><Icon name="cpu" size={12} />{run.run_id === activeRunId ? Object.keys(deck.lanes).length : "--"} {zh ? ZH.workers : "workers"}</span><span><Icon name="flag" size={12} />{run.flag_count ?? run.flags?.length ?? (run.flag ? 1 : 0)} / {run.expected_flags ?? 1}</span><span><Icon name="clock" size={12} />{run.run_id === activeRunId ? fmtTime(deck.startedAt) : "--"}</span></div><span className="run-stack-arrow"><Icon name="chevronRight" size={15} /></span></button><button type="button" className="run-stack-delete" onClick={() => onDeleteRun(run)} aria-label={`${zh ? ZH.deleteRun : "Delete Run"} ${run.run_id}`} title={zh ? ZH.deleteRun : "Delete Run"}><Icon name="xCircle" size={14} /></button></article>)}</div>

      {activeRun && (
        <section className="selected-run-workspace" aria-label={zh ? "已选 Run 调查入口" : "Selected run investigation entry"}>
          <div className="selected-run-summary">
            <span className="eyebrow">{zh ? ZH.selectedRun : "SELECTED RUN"}</span>
            <div><strong>{activeRun.name || activeRun.run_id}</strong><code>{activeRun.run_id}</code><span className={`run-stack-status ${activeRun.status}`}>{statusLabel(activeRun.status, activeRun.solved, zh)}</span></div>
            <p>{zh ? "进入独立调查空间，查看 Worker、Intent、证据来源链、路线、活动与调用账本。" : "Open the independent investigation space for workers, intents, evidence lineage, routes, activity, and usage."}</p>
          </div>
          <button className="selected-run-delete" type="button" onClick={() => onDeleteRun(activeRun)}><Icon name="xCircle" size={14} />{zh ? ZH.deleteRun : "Delete Run"}</button>
          {activeRunMismatch && (
            <div className="selected-run-warning" role="alert">
              <div><Icon name="alert" size={15} /><strong>{zh ? "历史派发绑定与当前 Target 不一致" : "Historical dispatch binding does not match this Target"}</strong></div>
              <p>{zh ? "这是启动检测或其他旧上下文产生的 Run，不能继续恢复。历史回放仍可查看，请新建 Target Run。" : "This Run came from a startup check or another old context and cannot be resumed. Replay remains available; create a new Target Run."}</p>
              <button className="target-ghost-button" onClick={onNewRun}><Icon name="play" size={13} />{zh ? "新建 Run" : "Create new Run"}</button>
            </div>
          )}
          <button className="selected-run-open" onClick={() => onOpenDetails("workers")}><Icon name="panel" size={16} /><span>{zh ? "打开调查工作台" : "Open investigation workspace"}</span><Icon name="chevronRight" size={15} /></button>
          <nav className="selected-run-quicklinks" aria-label={zh ? "Run 快捷入口" : "Run quick links"}>
            <button onClick={() => onOpenDetails("intents")}><Icon name="crosshair" size={13} />{zh ? ZH.intents : "Intents"}</button>
            <button onClick={() => onOpenDetails("evidence")}><Icon name="check" size={13} />{ZH.evidence}</button>
            <button onClick={() => onOpenDetails("routes")}><Icon name="network" size={13} />{zh ? ZH.routes : "Routes"}</button>
            <button onClick={() => onOpenDetails("ledger")}><Icon name="list" size={13} />{zh ? ZH.ledger : "Ledger"}</button>
            <button onClick={() => onOpenDetails("btw")}><Icon name="help" size={13} />{zh ? "询问进展" : "Ask Progress"}</button>
          </nav>
          <RunBindingInspector snapshot={activeRun.binding_snapshot} compact />
          <ObserverSummaryCard
            deck={deck}
            observability={observability}
            onOpenProgress={() => onOpenDetails("btw")}
          />
        </section>
      )}
    </section>
  );
}
