"use client";

import { useMemo } from "react";
import { Icon } from "@/components/Icon";
import { PanelEmpty } from "@/components/PanelEmpty";
import type { DeckState } from "@/lib/events";
import { formatDurationMs } from "@/lib/reason";
import { detailUrlForRun } from "@/lib/runRoute";
import { projectRouteLineage, type RouteLineageNode } from "@/lib/lineage";
import { useLang } from "@/lib/i18n";

function nodeClass(node: RouteLineageNode): string {
  return `route-lineage-node kind-${node.kind} state-${node.status || "open"}`;
}

export function RoutesWorkspace({ deck, runId }: { deck: DeckState; runId: string }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const lineage = useMemo(() => projectRouteLineage(deck), [deck]);
  const intents = deck.blackboard.intents;
  const summaryByIntent = useMemo(
    () => new Map(intents.map((intent) => [intent.id, intent.summary || intent.goal])),
    [intents],
  );
  const suppressed = deck.blackboard.suppressedRoutes ?? [];
  const branches = deck.blackboard.branches ?? [];
  const cycles = deck.reasonLoop.cycles;
  const linked = new Set(lineage.nodes.map((node) => node.id));
  const legacyIntents = intents.filter((intent) => !linked.has(`intent:${intent.id}`));

  if (!cycles.length && !intents.length && !suppressed.length && !branches.length && !lineage.edges.length) {
    return <PanelEmpty icon="network" title={zh ? "尚无路线" : "No routes yet"} hint={zh ? "Reason 规划和派发 Intent 后，这里会形成可追溯路线树。" : "A traceable route tree appears after Reason plans and dispatches intents."} />;
  }

  const byFrom = new Map<string, typeof lineage.edges>();
  for (const edge of lineage.edges) byFrom.set(edge.from, [...(byFrom.get(edge.from) ?? []), edge]);
  const nodeById = new Map(lineage.nodes.map((node) => [node.id, node]));
  // Roots come only from edges that have no incoming parent. No cycle, index, or
  // array position is used to invent a parent; disconnected roots stay separate.
  const rootNodes = lineage.roots.map((id) => nodeById.get(id)).filter((node): node is RouteLineageNode => Boolean(node));

  const renderNode = (node: RouteLineageNode, depth: number, trail: Set<string>) => {
    const children = (byFrom.get(node.id) ?? []).map((edge) => nodeById.get(edge.to)).filter((item): item is RouteLineageNode => Boolean(item));
    const deduped = children.filter((child, index) => children.findIndex((item) => item.id === child.id) === index);
    const nextTrail = new Set(trail).add(node.id);
    return (
      <div className="route-lineage-branch" key={`${node.id}-${depth}`}>
        <article className={nodeClass(node)}>
          <span className="route-node-icon"><Icon name={node.kind === "run" ? "target" : node.kind === "worker" ? "cpu" : node.kind === "fact" ? "check" : node.kind === "reason" ? "refresh" : "network"} size={14} /></span>
          <div className="route-lineage-copy">
            <span>{node.kind.toUpperCase()}</span>
            <strong title={node.label}>{node.kind === "intent" ? (summaryByIntent.get(node.intentId || "") || node.label) : node.label}</strong>
            {node.kind === "intent" && node.intentId && <code>{node.intentId}</code>}
            {node.status && <em className={`intent-state ${node.status}`}>{node.status}</em>}
            {node.durationMs != null && <span className="route-node-time">{formatDurationMs(node.durationMs)}</span>}
            {node.kind === "worker" && node.workerId && <a href={`${detailUrlForRun(runId, "workers")}?worker=${encodeURIComponent(node.workerId)}`}>{node.workerId}</a>}
            {node.kind === "fact" && node.factSeq && <a href={detailUrlForRun(runId, "evidence")}>{zh ? "查看证据" : "open evidence"}</a>}
          </div>
        </article>
        {deduped.length > 0 && (
          <div className={`route-lineage-children count-${Math.min(deduped.length, 4)}`}>
            {deduped.map((child) => nextTrail.has(child.id) ? null : renderNode(child, depth + 1, nextTrail))}
          </div>
        )}
      </div>
    );
  };

  return (
    <section className="routes-workspace" aria-label={zh ? "路线树" : "Route tree"}>
      <header className="run-view-heading">
        <div>
          <span className="eyebrow">ROUTES / LINEAGE</span>
          <h2>{zh ? "调查路线树" : "Investigation route tree"}</h2>
          <p>{zh ? "只沿事件明确发布的 lineage edge 展示 Reason、Intent、Worker 与 Evidence；缺失关系不猜测。" : "Only explicit event-published lineage edges connect Reason, Intent, Worker, and Evidence; missing relationships are never inferred."}</p>
        </div>
        <div className="run-view-stats">
          <span><b>{lineage.edges.length}</b>{zh ? "显式边" : "explicit edges"}</span>
          <span><b>{intents.length}</b>intents</span>
          <span className={suppressed.length ? "warn" : ""}><b>{suppressed.length}</b>{zh ? "已压制" : "suppressed"}</span>
        </div>
      </header>

      <div className="route-tree route-lineage-tree">
        {lineage.explicit && rootNodes.length > 0 ? rootNodes.map((node) => renderNode(node, 0, new Set())) : (
          <div className="route-legacy-banner"><Icon name="archive" size={14} /><strong>{zh ? "legacy / lineage unavailable" : "legacy / lineage unavailable"}</strong><span>{zh ? "事件没有发布显式父子关系；不按 cycle、index 或数组顺序猜测。" : "Events did not publish explicit parent/child edges; cycle, index, and array order are not used."}</span></div>
        )}
      </div>

      {legacyIntents.length > 0 && (
        <section className="route-audit-zone route-legacy-zone">
          <div className="intent-section-label">LEGACY / LINEAGE UNAVAILABLE</div>
          <div className="route-audit-grid">
            {legacyIntents.map((intent) => <article key={intent.id}><span className="intent-state">legacy</span><strong>{intent.summary || intent.goal || intent.id}</strong><code>{intent.id}</code><small>{zh ? "没有显式 lineage edge" : "No explicit lineage edge"}</small></article>)}
          </div>
        </section>
      )}

      {cycles.length > 0 && (
        <details className="route-audit route-cycle-audit">
          <summary>{zh ? `${cycles.length} 个 Reason 周期审计` : `${cycles.length} Reason cycle audit records`}</summary>
          {cycles.map((cycle) => <article key={cycle.id}><strong>{cycle.id}</strong><span>{cycle.status} · {cycle.planner || "planner unknown"} · {cycle.durationMs != null ? formatDurationMs(cycle.durationMs) : "duration unknown"}</span>{cycle.audits.map((audit, index) => <p key={index}>{audit}</p>)}</article>)}
        </details>
      )}

      {(suppressed.length > 0 || branches.length > 0) && (
        <section className="route-audit-zone">
          <div className="intent-section-label">ROUTE AUDIT</div>
          <div className="route-audit-grid">
            {suppressed.slice().reverse().map((route) => <article key={`${route.routeHash}-${route.ts}`}><span className={`intent-state ${route.reopened ? "completed" : "failed"}`}>{route.reopened ? (zh ? "已重开" : "reopened") : (zh ? "已压制" : "suppressed")}</span><strong>{route.label || route.routeHash}</strong><p>{route.reason}</p><small>{route.actor} · {route.routeHash}</small></article>)}
            {branches.slice().reverse().map((branch) => <article key={`${branch.branchId}-${branch.ts}`}><span className={`intent-state ${branch.status === "resolved" ? "completed" : "running"}`}>{branch.status || "open"}</span><strong>{branch.title || branch.branchId}</strong><small>{branch.actor} · {branch.branchId}</small></article>)}
          </div>
        </section>
      )}
    </section>
  );
}
