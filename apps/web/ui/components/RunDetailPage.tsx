"use client";

import { useEffect, useMemo, useState } from "react";
import { useT, useLang } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { Icon, type IconName } from "@/components/Icon";
import {
  useBudgetSnapshot,
  useRun,
  useRunList,
  useRunObservability,
  spawnWorker,
  killWorker,
} from "@/lib/useRun";
import { isRunActive, type GraphNode } from "@/lib/events";
import { deckUrlForRun, type DetailView } from "@/lib/runRoute";
import { EvidenceChain } from "@/components/EvidenceChain";
import { WorkerLanes } from "@/components/WorkerLanes";
import { GraphView } from "@/components/GraphView";
import { Blackboard } from "@/components/Blackboard";
import { ActivityStream } from "@/components/ActivityStream";
import {
  CredentialsPanel,
  DirectivesPanel,
  PocsPanel,
  ReviewFindingsPanel,
  RoutesPanel,
} from "@/components/ArtifactPanel";
import { PanelEmpty } from "@/components/PanelEmpty";
import { NodeInspector } from "@/components/NodeInspector";
import { BtwPage } from "@/components/BtwPage";
import { RunBindingInspector } from "@/components/RunBindingInspector";
import { IntentWorkspace } from "@/components/IntentWorkspace";
import { LedgerWorkspace } from "@/components/LedgerWorkspace";
import { RoutesWorkspace } from "@/components/RoutesWorkspace";
import { OperatorBar } from "@/components/OperatorBar";

interface NavItem {
  view: DetailView;
  labelKey: string;
  icon: IconName;
  section: "investigate" | "artifacts";
}

const NAV_ITEMS: NavItem[] = [
  { view: "workers", labelKey: "panelbtn.workers", icon: "cpu", section: "investigate" },
  { view: "intents", labelKey: "panelbtn.intents", icon: "crosshair", section: "investigate" },
  { view: "evidence", labelKey: "panelbtn.evidence", icon: "check", section: "investigate" },
  { view: "routes", labelKey: "panelbtn.routes", icon: "network", section: "investigate" },
  { view: "timeline", labelKey: "panelbtn.timeline", icon: "clock", section: "investigate" },
  { view: "ledger", labelKey: "panelbtn.ledger", icon: "list", section: "investigate" },
  { view: "btw", labelKey: "btw.title", icon: "help", section: "investigate" },
  { view: "graph", labelKey: "panelbtn.graph", icon: "grid", section: "artifacts" },
  { view: "blackboard", labelKey: "panelbtn.blackboard", icon: "board", section: "artifacts" },
  { view: "findings", labelKey: "panelbtn.findings", icon: "alert", section: "artifacts" },
  { view: "credentials", labelKey: "panelbtn.credentials", icon: "lock", section: "artifacts" },
  { view: "pocs", labelKey: "panelbtn.pocs", icon: "target", section: "artifacts" },
  { view: "directives", labelKey: "panelbtn.directives", icon: "send", section: "artifacts" },
];

function elapsedLabel(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

function compactNumber(value: number): string {
  if (value < 1_000) return Math.round(value).toLocaleString();
  return `${(value / 1_000).toFixed(value < 10_000 ? 1 : 0)}k`;
}
function lifecycleLabel(status: string, zh: boolean): string {
  if (!zh) return status;
  return ({ loading: "载入中", running: "运行中", queued: "排队中", paused: "已暂停", reason_blocked: "Reason 已阻断", degraded: "运行已降级", fallback_active: "已启用 fallback", solved: "已解出", finished: "已完成", failed: "失败", cancelled: "已取消" } as Record<string, string>)[status] || status;
}
function healthLabel(status: string, zh: boolean): string {
  if (!zh) return status;
  return ({ healthy: "正常", ready: "就绪", active: "运行中", degraded: "已降级", blocked: "已阻断", unknown: "未知" } as Record<string, string>)[status] || status;
}

export function RunDetailPage({ view }: { view: DetailView }) {
  const t = useT();
  const { lang, setLang } = useLang();
  const zh = lang === "zh";
  const { theme, toggleTheme } = useTheme();
  const [runId, setRunId] = useState("");
  const [opTarget, setOpTarget] = useState("global");
  // `?worker=<id>` focuses that worker's lane on the workers view. The route tree
  // and the evidence chain both deep-link with it, so the target must actually be
  // honoured instead of landing on an unfiltered list.
  const [workerFocus, setWorkerFocus] = useState<{ id: string; nonce: number } | null>(null);
  useEffect(() => {
    const match = window.location.pathname.match(/^\/run\/([^/]+)\/[^/]+\/?$/);
    setRunId(match ? decodeURIComponent(match[1]) : "");
    const worker = new URLSearchParams(window.location.search).get("worker");
    if (worker && /\/workers\/?$/.test(window.location.pathname)) {
      setWorkerFocus({ id: worker, nonce: 1 });
    }
  }, []);
  const { deck, connected, sendHitl, resolve } = useRun(runId);
  const runs = useRunList(5000);
  const summary = runs.find((run) => run.run_id === runId);
  const observability = useRunObservability(runId);
  const budget = useBudgetSnapshot(runId);
  const running = isRunActive(deck);
  const rebuildBudget = budget.rebuild;
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const workerCount = Object.keys(deck.lanes).length;
  const intentCount = deck.reasonLoop.cycles.reduce((total, cycle) => total + cycle.intents.length, 0)
    || deck.blackboard.intents.length;
  const verifiedCount = deck.blackboard.facts.filter((fact) => fact.verified).length;
  const lifecycle = observability.snapshot?.lifecycle.status || summary?.status || (running ? "running" : deck.finished ? "finished" : "loading");
  const terminal = Boolean(summary?.finished || deck.finished);
  const elapsed = observability.snapshot?.lifecycle.elapsed_ms
    ?? ((deck.startedAt ? ((deck.finishedAt || Date.now()) - deck.startedAt) : 0));
  const tokens = observability.snapshot?.usage.tokens ?? (deck.tokensIn + deck.tokensOut);
  const cost = observability.snapshot?.usage.usd ?? deck.usd;
  const targetName = summary?.target_name || deck.challengeName || (zh ? "未命名 Target" : "Untitled target");
  const direction = summary?.direction || summary?.category || deck.category || (zh ? "综合探索" : "General exploration");
  const onCommand = (target: string, action: string, text: string, recipients?: string[]) => {
    if (action === "resume") {
      void resolve(text || undefined).catch(() => undefined);
      return;
    }
    void sendHitl(target, action, text, { recipients });
  };
  const onReview = () => {
    if (terminal && summary?.target_id) window.location.href = `${deckUrlForRun(runId)}?review=1`;
  };

  const body = useMemo(() => {
    if (!runId) return null;
    if (!deck.started && !deck.finished && !deck.reasonLoop.cycles.length && !deck.blackboard.facts.length) {
      return <PanelEmpty icon="clock" title={t("detail.loading")} />;
    }
    switch (view) {
      case "evidence":
        return <EvidenceChain deck={deck} onOpenWorker={(id) => { window.location.href = `/run/${encodeURIComponent(runId)}/workers?worker=${encodeURIComponent(id)}`; }} />;
      case "workers":
        return (
          <div className="run-workers-view">
            <RunBindingInspector observability={observability.snapshot} compact={false} />
            {observability.error && <p className="detail-observability-error" role="status">{zh ? "运行可观测数据不可用" : "Runtime observability unavailable"}: {observability.error}</p>}
            <WorkerLanes deck={deck} running={running} focusWorker={workerFocus} onSpawnWorker={(engine) => void spawnWorker(runId, engine)} onKillWorker={(id) => void killWorker(runId, id)} />
          </div>
        );
      case "intents":
        return <IntentWorkspace deck={deck} runId={runId} />;
      case "ledger":
        return <LedgerWorkspace budget={budget.snapshot} observability={observability.snapshot} loading={budget.loading || observability.loading} rebuilding={budget.rebuilding} error={budget.error || observability.error} onRebuild={async () => { await rebuildBudget(); }} />;
      case "graph":
        return <><GraphView model={deck.model} onSelect={setSelected} />{selected && <div className="insp-float"><NodeInspector node={selected} onClose={() => setSelected(null)} /></div>}</>;
      case "timeline":
        return <ActivityStream deck={deck} />;
      case "blackboard":
        return <Blackboard bb={deck.blackboard} runId={runId} />;
      case "findings":
        return <ReviewFindingsPanel deck={deck} />;
      case "credentials":
        return <CredentialsPanel runId={runId} />;
      case "pocs":
        return <PocsPanel deck={deck} />;
      case "routes":
        return <RoutesWorkspace deck={deck} runId={runId} />;
      case "directives":
        return <DirectivesPanel deck={deck} />;
      case "btw":
        return <BtwPage runId={runId} />;
    }
  }, [view, runId, deck, running, selected, t, zh, observability.snapshot, observability.error, observability.loading, budget.snapshot, budget.loading, budget.rebuilding, budget.error, rebuildBudget]);

  const inherited = observability.snapshot?.knowledge?.inherited ?? [];
  const primaryItems = NAV_ITEMS.filter((item) => item.section === "investigate");
  const artifactItems = NAV_ITEMS.filter((item) => item.section === "artifacts");

  return (
    <div className="run-workbench detail-shell motion-root">
      <header className="run-workbench-topbar">
        <a className="run-workbench-back" href={deckUrlForRun(runId)} aria-label={t("detail.back")}><Icon name="chevronRight" size={15} />{t("detail.back")}</a>
        <div className="run-workbench-identity">
          <span>{t("detail.brand")}</span>
          <strong title={targetName}>{targetName}</strong>
          <i>/</i>
          <code title={runId}>{runId || "run"}</code>
        </div>
        <div className="run-workbench-top-actions">
          <span className={`run-workbench-connection ${connected ? "live" : "offline"}`}><i />{connected ? (zh ? "实时" : "Live") : (zh ? "回放" : "Replay")}</span>
          <button className="icon-btn" onClick={toggleTheme} title={t(theme === "dark" ? "theme.toLight" : "theme.toDark")} aria-label={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}><Icon name={theme === "dark" ? "sun" : "moon"} /></button>
          <button className="lang-btn" onClick={() => setLang(lang === "zh" ? "en" : "zh")} title={t("lang.toggleTitle")} aria-label={t("lang.toggleTitle")}>{t("lang.toggle")}</button>
        </div>
      </header>

      <div className="run-workbench-summary">
        <div className="run-workbench-title">
          <span className="eyebrow">RUN / {direction.toUpperCase()}</span>
          <h1>{deck.challengeName || summary?.name || runId}</h1>
          <p>{zh ? "Reason、Worker、Intent 与证据的独立调查空间" : "Independent investigation space for Reason, workers, intents, and evidence"}</p>
        </div>
        <div className="run-workbench-metrics">
          <div><span>{zh ? "状态" : "Status"}</span><strong className={`run-status-text ${lifecycle}`}>{lifecycleLabel(lifecycle, zh)}</strong></div>
          <div><span>{zh ? "Worker" : "Workers"}</span><strong>{workerCount}</strong></div>
          <div><span>{zh ? "Intent" : "Intents"}</span><strong>{intentCount}</strong></div>
          <div><span>{zh ? "已验证" : "Verified"}</span><strong>{verifiedCount}</strong></div>
          <div><span>{zh ? "令牌" : "Tokens"}</span><strong>{compactNumber(tokens)}</strong></div>
          <div><span>{zh ? "耗时" : "Elapsed"}</span><strong>{elapsedLabel(elapsed)}</strong></div>
          <div><span>{zh ? "成本" : "Cost"}</span><strong>${cost.toFixed(cost > 0 && cost < 0.01 ? 4 : 2)}</strong></div>
        </div>
      </div>

      <div className="run-workbench-layout">
        <aside className="run-workbench-sidebar">
          <nav aria-label={t("detail.navAria")}>
            <span className="run-nav-label">{zh ? "调查" : "INVESTIGATE"}</span>
            {primaryItems.map((item) => <a key={item.view} className={`run-nav-item ${view === item.view ? "active" : ""}`} href={`/run/${encodeURIComponent(runId)}/${item.view}`}><Icon name={item.icon} size={15} /><span>{t(item.labelKey)}</span>{view === item.view && <i />}</a>)}
            <span className="run-nav-label artifacts">{zh ? "工件" : "ARTIFACTS"}</span>
            {artifactItems.map((item) => <a key={item.view} className={`run-nav-item ${view === item.view ? "active" : ""}`} href={`/run/${encodeURIComponent(runId)}/${item.view}`}><Icon name={item.icon} size={15} /><span>{t(item.labelKey)}</span>{view === item.view && <i />}</a>)}
          </nav>
          <div className="run-nav-context">
            <span>{zh ? "当前方向" : "Direction"}</span><strong>{direction}</strong>
            <span>{zh ? "继承事实" : "Inherited facts"}</span><strong>{inherited.length}</strong>
            <span>{zh ? "健康" : "Health"}</span><strong className={`health-${observability.snapshot?.health.status || "unknown"}`}>{healthLabel(observability.snapshot?.health.status || "unknown", zh)}</strong>
          </div>
        </aside>

        <main className={`run-workbench-content view-${view}`} aria-label={t(NAV_ITEMS.find((item) => item.view === view)?.labelKey || "detail.brand")}>
          {inherited.length > 0 && view !== "btw" && (
            <details className="run-inherited-banner">
              <summary><Icon name="check" size={13} /><span>{zh ? `继承了 ${inherited.length} 条已验证事实（只读）` : `${inherited.length} inherited verified facts (read-only)`}</span><Icon name="chevronDown" size={13} /></summary>
              <div>{inherited.map((fact) => <p key={fact.fact_id}><strong>{fact.fact}</strong><small>{fact.source_run_id || "unknown"} · #{fact.fact_seq ?? "?"}</small></p>)}</div>
            </details>
          )}
          {!runId || !body ? <PanelEmpty icon="grid" title={t("detail.loading")} /> : body}
        </main>
      </div>
      <OperatorBar
        started={deck.started}
        running={running}
        lanes={deck.lanes}
        target={opTarget}
        onTargetChange={setOpTarget}
        onCommand={onCommand}
        onReview={onReview}
        terminal={terminal}
        canReview={terminal && Boolean(summary?.target_id)}
      />
    </div>
  );
}
