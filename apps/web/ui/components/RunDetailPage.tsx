"use client";

/**
 * Full-page detail view for one run (docs/07 Phase-4 evolution): the run deck
 * stays a clean timeline-first page; heavy content (evidence chain, worker
 * firehose, blackboard, ...) opens here in its own browser tab. Same SSE
 * pipeline as the deck (useRun), full-width layout, view switcher as links so
 * middle-click / ctrl+click keep native browser semantics.
 */

import { useEffect, useMemo, useState } from "react";
import { useT, useLang } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { Icon } from "@/components/Icon";
import { useRun, spawnWorker, killWorker } from "@/lib/useRun";
import { isRunActive, type GraphNode } from "@/lib/events";
import { deriveStage } from "@/lib/timeline";
import { deckUrlForRun, DETAIL_VIEWS, type DetailView } from "@/lib/runRoute";
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

const VIEW_TITLE_KEY: Record<DetailView, string> = {
  evidence: "panelbtn.evidence",
  workers: "panelbtn.workers",
  graph: "panelbtn.graph",
  timeline: "panelbtn.timeline",
  blackboard: "panelbtn.blackboard",
  findings: "panelbtn.findings",
  credentials: "panelbtn.credentials",
  pocs: "panelbtn.pocs",
  routes: "panelbtn.routes",
  directives: "panelbtn.directives",
  btw: "btw.title",
};

export function RunDetailPage({ view }: { view: DetailView }) {
  const t = useT();
  const { lang, setLang } = useLang();
  const { theme, toggleTheme } = useTheme();
  const [runId, setRunId] = useState("");
  useEffect(() => {
    // /run/<id>/<view> deep link: the id lives in the second path segment
    const m = window.location.pathname.match(/^\/run\/([^/]+)\/[^/]+\/?$/);
    setRunId(m ? decodeURIComponent(m[1]) : "");
  }, []);
  const { deck, connected } = useRun(runId);
  const running = isRunActive(deck);
  const [selected, setSelected] = useState<GraphNode | null>(null);

  const body = useMemo(() => {
    // SSE replay in flight: show a loading state instead of a false "empty"
    // flash (the fold populates deck.started within the first replay events).
    if (!runId) return null;
    if (!deck.started && !deck.finished && !deck.reasonLoop.cycles.length
        && !deck.blackboard.facts.length) {
      return <PanelEmpty icon="clock" title={t("detail.loading")} />;
    }
    switch (view) {
      case "evidence":
        return (
          <EvidenceChain
            deck={deck}
            onOpenWorker={(id) => {
              window.location.href = deckUrlForRun(runId);
            }}
          />
        );
      case "workers":
        return (
          <WorkerLanes
            deck={deck}
            running={running}
            onSpawnWorker={(engine) => void spawnWorker(runId, engine)}
            onKillWorker={(id) => void killWorker(runId, id)}
          />
        );
      case "graph":
        return (
          <>
            <GraphView model={deck.model} onSelect={setSelected} />
            {selected && (
              <div className="insp-float">
                <NodeInspector node={selected} onClose={() => setSelected(null)} />
              </div>
            )}
          </>
        );
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
        return <RoutesPanel deck={deck} />;
      case "directives":
        return <DirectivesPanel deck={deck} />;
      case "btw":
        return <BtwPage runId={runId} />;
    }
  }, [view, runId, deck, running, t]);

  return (
    <div className="shell detail-shell motion-root">
      <nav className="detail-nav" aria-label={t("detail.navAria")}>
        <a className="detail-back" href={deckUrlForRun(runId)}>{t("detail.back")}</a>
        <span className="detail-brand">{t("detail.brand")}</span>
        <span className="detail-run" title={runId}>{deck.challengeName || runId}</span>
        {connected && <span className="detail-live">{t("topbar.live")}</span>}
        <span className="detail-spacer" />
        <button className="icon-btn" onClick={toggleTheme}
          title={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
          aria-label={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}>
          <Icon name={theme === "dark" ? "sun" : "moon"} />
        </button>
        <button className="lang-btn" onClick={() => setLang(lang === "zh" ? "en" : "zh")}
          title={t("lang.toggleTitle")} aria-label={t("lang.toggleTitle")}>
          {t("lang.toggle")}
        </button>
        <span className="detail-tabs">
          {DETAIL_VIEWS.map((v) => (
            <a key={v} className={`detail-tab ${v === view ? "on" : ""}`}
              href={`/run/${encodeURIComponent(runId)}/${v}`}>{t(VIEW_TITLE_KEY[v])}</a>
          ))}
        </span>
      </nav>
      <main className="detail-body" aria-label={t(VIEW_TITLE_KEY[view])}>
        {!runId || !body
          ? <PanelEmpty icon="grid" title={t("detail.loading")} />
          : body}
      </main>
    </div>
  );
}
