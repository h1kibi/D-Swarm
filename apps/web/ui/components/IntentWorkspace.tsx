"use client";

import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { PanelEmpty } from "@/components/PanelEmpty";
import type { DeckState } from "@/lib/events";
import { formatDurationMs, type ReasonIntentView } from "@/lib/reason";
import { detailUrlForRun } from "@/lib/runRoute";
import { useLang } from "@/lib/i18n";

const STATUS_ORDER: Record<string, number> = {
  running: 0, claimed: 1, awaiting_validation: 2, queued: 3, proposed: 4,
  failed: 5, skipped: 6, completed: 7,
};

function statusLabel(status: string, zh: boolean): string {
  const labels: Record<string, [string, string]> = {
    proposed: ["已提出", "Proposed"], queued: ["排队中", "Queued"], claimed: ["已领取", "Claimed"],
    running: ["执行中", "Running"], awaiting_validation: ["等待验证", "Awaiting validation"],
    completed: ["已完成", "Completed"], skipped: ["已跳过", "Skipped"], failed: ["失败", "Failed"],
    open: ["待领取", "Open"], done: ["已完成", "Done"],
  };
  return labels[status]?.[zh ? 0 : 1] ?? status;
}


export function IntentWorkspace({ deck, runId }: { deck: DeckState; runId: string }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  // `?intent=<id>` deep-links a specific intent (the worker detail's "查看 Intent"
  // action and the timeline both use it). Unknown ids simply highlight nothing.
  const [focusIntentId, setFocusIntentId] = useState("");
  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("intent") || "";
    if (!wanted) return;
    setFocusIntentId(wanted);
    const node = document.getElementById(`intent-${wanted}`);
    if (node) node.scrollIntoView({ block: "center" });
  }, [deck.blackboard.intents.length, deck.reasonLoop.cycles.length]);
  const summaryById = useMemo(
    () => new Map(deck.blackboard.intents.map((intent) => [intent.id, intent.summary?.trim() || intent.goal])),
    [deck.blackboard.intents],
  );
  const reasonIntents = useMemo(
    () => deck.reasonLoop.cycles.flatMap((cycle) => cycle.intents),
    [deck.reasonLoop.cycles],
  );
  const visibleCycles = useMemo(
    () => [...deck.reasonLoop.cycles].reverse(),
    [deck.reasonLoop.cycles],
  );
  const knownIds = useMemo(() => new Set(reasonIntents.map((intent) => intent.id)), [reasonIntents]);
  const legacy = useMemo(
    () => deck.blackboard.intents.filter((intent) => !knownIds.has(intent.id)),
    [deck.blackboard.intents, knownIds],
  );
  const ordered = useMemo(
    () => [...reasonIntents].sort((a, b) =>
      (STATUS_ORDER[a.status] ?? 99) - (STATUS_ORDER[b.status] ?? 99)
      || (b.priority ?? -1) - (a.priority ?? -1)),
    [reasonIntents],
  );
  const activeCount = ordered.filter((intent) => ["proposed", "queued", "claimed", "running"].includes(intent.status)).length;
  const completedCount = ordered.filter((intent) => intent.status === "completed").length + legacy.filter((intent) => intent.status === "done").length;
  const blockedCount = ordered.filter((intent) => intent.status === "failed" || intent.status === "skipped").length;

  if (!reasonIntents.length && !legacy.length && !deck.reasonLoop.recon) {
    return <PanelEmpty icon="crosshair" title={zh ? "尚无 Intent" : "No intents yet"} hint={zh ? "Reason 产生的探索任务会在这里显示。" : "Exploration tasks proposed by Reason will appear here."} />;
  }

  return (
    <section className="intent-workspace" aria-label={zh ? "Intent 工作区" : "Intent workspace"}>
      <header className="run-view-heading">
        <div>
          <span className="eyebrow">REASON / INTENTS</span>
          <h2>{zh ? "探索意图" : "Exploration intents"}</h2>
          <p>{zh ? "查看 Reason 为什么派发任务、任务来自哪些事实，以及每个 Intent 当前由谁执行。" : "See why Reason dispatched work, which facts informed it, and who owns each intent."}</p>
        </div>
        <div className="run-view-stats" aria-label={zh ? "Intent 统计" : "Intent totals"}>
          <span><b>{activeCount}</b>{zh ? "进行中" : "active"}</span>
          <span><b>{completedCount}</b>{zh ? "已完成" : "done"}</span>
          <span className={blockedCount ? "warn" : ""}><b>{blockedCount}</b>{zh ? "跳过/失败" : "skipped/failed"}</span>
        </div>
      </header>

      <div className="intent-layout">
        <aside className="reason-cycle-rail" aria-label={zh ? "Reason 周期" : "Reason cycles"}>
          <div className="intent-section-label">REASON CYCLES</div>
          {deck.reasonLoop.recon && (
            <div className="reason-cycle-card recon">
              <div><Icon name="search" size={13} /><strong>Recon</strong></div>
              <span className={`intent-state ${deck.reasonLoop.recon.status}`}>{deck.reasonLoop.recon.status}</span>
              {deck.reasonLoop.recon.newFindings != null && <small>{deck.reasonLoop.recon.newFindings} {zh ? "条发现" : "findings"}</small>}
            </div>
          )}
          {visibleCycles.map((cycle, index) => (
            <div className={`reason-cycle-card ${cycle.status}`} key={cycle.id}>
              <div><Icon name="refresh" size={13} /><strong>{zh ? `周期 ${visibleCycles.length - index}` : `Cycle ${visibleCycles.length - index}`}</strong></div>
              <span className={`intent-state ${cycle.status}`}>{statusLabel(cycle.status, zh)}</span>
              <small>{cycle.id}</small>
              {cycle.durationMs != null && <small>{formatDurationMs(cycle.durationMs)}</small>}
              {cycle.trigger && <small title={cycle.trigger}>{cycle.trigger}</small>}
            </div>
          ))}
          {deck.reasonLoop.stopReason && (
            <div className="reason-stop-card"><span>{zh ? "停止原因" : "Stop reason"}</span><strong>{deck.reasonLoop.stopReason}</strong></div>
          )}
        </aside>

        <div className="intent-board">
          <div className="intent-section-label">INTENT QUEUE & HISTORY</div>
          {ordered.map((intent) => (
            <article className={`intent-card status-${intent.status}${focusIntentId === intent.id ? " focused" : ""}`} key={intent.id} id={`intent-${intent.id}`}>
              <div className="intent-card-top">
                <span className={`intent-state ${intent.status}`}>{statusLabel(intent.status, zh)}</span>
                <code>{intent.id}</code>
                {intent.priority != null && <span className="intent-priority">P{intent.priority.toFixed(2)}</span>}
              </div>
              <h3 title={intent.goal}>{summaryById.get(intent.id) || intent.goal || intent.id}</h3>
              <div className="intent-card-meta">
                {intent.mode && <span>{intent.mode}</span>}
                {intent.profile && <span>{intent.profile}</span>}
                {intent.taskKind && <span>{intent.taskKind}</span>}
                {intent.surfaceTarget && <span title={intent.surfaceTarget}>{intent.surfaceTarget}</span>}
              </div>
              <div className="intent-lineage">
                <div>
                  <span className="intent-key">{zh ? "来源事实" : "From facts"}</span>
                  {intent.fromFactIds.length ? (
                    <span className="intent-fact-links">{intent.fromFactIds.map((factId) => (
                      <a key={factId} href={`${detailUrlForRun(runId, "evidence")}?fact=${encodeURIComponent(factId)}`}>{factId}</a>
                    ))}</span>
                  ) : <em>{zh ? "初始探索" : "Initial exploration"}</em>}
                </div>
                <div>
                  <span className="intent-key">Worker</span>
                  {intent.workerId ? <a href={`${detailUrlForRun(runId, "workers")}?worker=${encodeURIComponent(intent.workerId)}`}>{intent.workerId}</a> : <em>{zh ? "尚未派发" : "Not dispatched"}</em>}
                </div>
              </div>
              {intent.dispatchReason && <p className="intent-note"><b>{zh ? "派发原因" : "Dispatch"}</b>{intent.dispatchReason}</p>}
              {intent.skipReason && <p className="intent-note warning"><b>{zh ? "跳过原因" : "Skip reason"}</b>{intent.skipReason}</p>}
              {intent.dedupeKey && <div className="intent-dedupe">dedupe · {intent.dedupeKey}</div>}
            </article>
          ))}

          {legacy.length > 0 && (
            <section className="legacy-intents">
              <div className="intent-section-label">LEGACY / BLACKBOARD</div>
              {legacy.map((intent) => (
                <article className={`intent-card status-${intent.status}${focusIntentId === intent.id ? " focused" : ""}`} key={intent.id} id={`intent-${intent.id}`}>
                  <div className="intent-card-top"><span className={`intent-state ${intent.status}`}>{statusLabel(intent.status, zh)}</span><code>{intent.id}</code></div>
                  <h3>{intent.summary || intent.goal}</h3>
                  <div className="intent-card-meta"><span>{intent.workerClass}</span>{intent.worker && <span>{intent.worker}</span>}</div>
                </article>
              ))}
            </section>
          )}
        </div>
      </div>
    </section>
  );
}
