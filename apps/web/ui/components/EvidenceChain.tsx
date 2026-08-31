"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { DeckState, BlackboardFact, isFactRetired } from "@/lib/events";
import { prettyFact } from "@/lib/factText";
import { useT, useLang } from "@/lib/i18n";
import { readKey, writeKey } from "@/lib/storage";
import { useCopied } from "@/lib/useCopied";
import { PanelEmpty } from "@/components/PanelEmpty";
import { Icon } from "@/components/Icon";
import {
  findingForFactSeq,
  findingKinds,
  formatAgeSec,
  pheromoneAgeSec,
  pheromoneBand,
  pheromoneStrength,
  type PheromoneFindingView,
} from "@/lib/pheromone";
import { usePheromoneClock } from "@/lib/usePheromoneClock";
import { jumpToFactEvent, jumpToIntentDispatch } from "@/lib/crosslink";

/**
 * The evidence chain — every provenance-gated fact (verified ✓ / candidate ?),
 * each with its witness / artifact / verifier disclosure, plus the dead-ends the
 * swarm ruled out. This is the "show me the proof" panel: the provenance verdict
 * for each fact, not just its text.
 *
 * Phase 5 (docs/07 §6.3): every fact card presents THREE INDEPENDENT dimensions —
 *   1. Truth status   (verified / candidate, plus dead-ends) — the gate verdict.
 *   2. Pheromone      (strength bar + value, Experimental badge, and base /
 *                      half-life / age in the expanded detail) — current
 *                      scheduling heat ONLY; never merged with truth/confidence.
 *                      Missing parameters (legacy sessions) render N/A.
 *   3. Provenance     (source worker + source event seq + owning intent) — each
 *                      clickable: worker → worker panel, seq / intent → the
 *                      Decision Timeline row (anchor + flash, no routing).
 *
 * Operator affordances: newest / oldest / strength sort, truth-status tabs,
 * finding-kind filter, Experimental-only filter, per-item copy.
 */

const SORT_KEY = "dswarm.evidence.newestFirst";
type EvidenceFilter = "all" | "verified" | "candidates" | "dead";
type SortMode = "newest" | "oldest" | "strength";

// ts may be unix seconds or ms — normalise to ms (mirrors ActivityStream).
function tsMs(ts: number): number {
  if (!ts) return 0;
  return ts < 1e12 ? ts * 1000 : ts;
}

// Coarse "x ago" stamp; facts carry a real `ts`, so this is genuine, not faked.
function relTime(ts: number, zh: boolean): string {
  const ms = tsMs(ts);
  if (!ms) return "";
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (sec < 5) return zh ? "刚刚" : "just now";
  if (sec < 60) return zh ? `${sec} 秒前` : `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return zh ? `${min} 分钟前` : `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return zh ? `${hr} 小时前` : `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return zh ? `${day} 天前` : `${day}d ago`;
}

function CopyFact({ text, t }: { text: string; t: (k: string, v?: Record<string, string | number>) => string }) {
  const [copied, copy] = useCopied();
  return (
    <button
      type="button"
      className={`evi-copy ${copied ? "copied" : ""}`.trim()}
      title={t("evidence.copyFact")}
      aria-label={t("evidence.copyFact")}
      onClick={() => copy(text)}
    >
      <Icon name={copied ? "check" : "copy"} size={13} />
    </button>
  );
}

function factKey(f: BlackboardFact, prefix: string, index: number): string {
  return `${prefix}${f.factSeq ?? `${f.actor}-${f.ts}-${index}`}`;
}

/** Dimension 2 — pheromone. Always its own row, NEVER merged into the truth
 *  status or confidence (§6.3 risk control). N/A when the session carries no
 *  pheromone parameters for this fact. */
function PheromoneRow({ finding, nowSec, t }: {
  finding?: PheromoneFindingView;
  nowSec: number;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const strength = finding ? pheromoneStrength(finding, nowSec) : undefined;
  const band = pheromoneBand(strength);
  return (
    <div className={`evi-pher band-${band}`}>
      <span className="evi-pher-label">{t("pheromone.label")}</span>
      {strength != null ? (
        <>
          <span className="pher-bar" aria-hidden="true">
            <span className={`pher-fill ${band}`} style={{ width: `${Math.round(strength * 100)}%` }} />
          </span>
          <span className="evi-pher-val">{strength.toFixed(2)}</span>
          {finding?.experimental && <span className="pher-exp">{t("pheromone.experimental")}</span>}
        </>
      ) : (
        <span className="evi-pher-na">{t("pheromone.na")}</span>
      )}
    </div>
  );
}

function FactItem({ f, t, zh, expanded, onToggle, finding, nowSec, onOpenWorker }: {
  f: BlackboardFact;
  t: (k: string, v?: Record<string, string | number>) => string;
  zh: boolean;
  expanded: boolean;
  onToggle: () => void;
  finding?: PheromoneFindingView;
  nowSec: number;
  onOpenWorker?: (id: string) => void;
}) {
  const gist = (f.summary || "").trim() || prettyFact(f.fact);
  const text = gist || f.fact;
  // When a gist is shown as the label, the FULL raw fact must stay one click away —
  // a gist can truncate/omit an anchor (flag/cred/port), so the operator needs the
  // verbatim text. Mirror the Blackboard card's <details> disclosure. Only render it
  // when the gist actually differs from the raw (no point disclosing identical text).
  const hasRaw = !!gist && gist !== f.fact;
  const when = relTime(f.ts, zh);
  const age = finding ? pheromoneAgeSec(finding, nowSec) : undefined;
  return (
    <div className={`evi-item ${f.verified ? "v" : "c"} ${expanded ? "expanded" : ""}`.trim()}>
      <div className="evi-head">
        <button
          type="button"
          className="evi-row"
          aria-expanded={expanded}
          title={t(expanded ? "evidence.collapseFact" : "evidence.expandFact")}
          onClick={onToggle}
        >
          <span className="evi-fact">{text}</span>
          <span className="evi-meta-inline">
            <span className={f.verified ? "ok" : "warn"}>{f.verified ? t("insp.verified") : t("insp.unverified")}</span>
            <span>{Number(f.confidence).toFixed(2)}</span>
            <span>{f.actor}</span>
            {when && <span>{when}</span>}
            {f.witness && <span>{t("evidence.hasWitness")}</span>}
            {f.artifactId && <span>{t("evidence.hasArtifact")}</span>}
          </span>
          <Icon name="chevronDown" size={13} />
        </button>
        <CopyFact text={f.fact || text} t={t} />
      </div>
      <PheromoneRow finding={finding} nowSec={nowSec} t={t} />
      {expanded && (
        <div className="evi-detail">
          {hasRaw && (
            <details className="evi-raw-d">
              <summary className="evi-raw-more">{t("insp.raw")}</summary>
              <div className="evi-raw-t">{f.fact}</div>
            </details>
          )}
          {/* Dimension 3 — provenance: source worker / source event / intent,
              each a jump target. */}
          <dl className="evi-prov">
            <dt>{t("insp.provenance")}</dt>
            <dd className={f.verified ? "ok" : "warn"}>{f.verified ? t("insp.verified") : t("insp.unverified")}</dd>
            <dt>{t("insp.confidence")}</dt><dd>{Number(f.confidence).toFixed(2)}</dd>
            {f.verifier && f.verifier !== "none" && <><dt>{t("insp.verifier")}</dt><dd>{f.verifier}</dd></>}
            {f.witness && <><dt>{t("bb.witness")}</dt><dd className="witness">{f.witness}</dd></>}
            {f.artifactId && <><dt>{t("bb.artifact")}</dt><dd>{f.artifactId}</dd></>}
            <dt>{t("insp.actor")}</dt>
            <dd>
              {onOpenWorker ? (
                <button type="button" className="evi-link" title={t("evidence.viewWorker")}
                  onClick={() => onOpenWorker(f.actor)}>{f.actor}</button>
              ) : f.actor}
            </dd>
            {f.factSeq != null && (
              <>
                <dt>{t("evidence.sourceEvent")}</dt>
                <dd>
                  <button type="button" className="evi-link"
                    title={t("evidence.jumpSource")}
                    onClick={() => jumpToFactEvent(f.factSeq!)}>#{f.factSeq}</button>
                </dd>
              </>
            )}
            {f.intentId && (
              <>
                <dt>{t("insp.intent")}</dt>
                <dd>
                  <button type="button" className="evi-link"
                    title={t("evidence.jumpIntent")}
                    onClick={() => jumpToIntentDispatch(f.intentId!)}>{f.intentId}</button>
                </dd>
              </>
            )}
            {when && <><dt>{t("evidence.sortLabel")}</dt><dd className="evi-when">{when}</dd></>}
          </dl>
          {/* Dimension 2 detail — the immutable decay parameters (N/A each when
              the session predates the pheromone channel). */}
          <dl className="evi-prov evi-pher-detail">
            <dt>{t("pheromone.label")}</dt>
            <dd className="evi-pher-note">{t("pheromone.note")}</dd>
            <dt>{t("pheromone.base")}</dt>
            <dd>{finding?.base != null ? finding.base.toFixed(2) : t("pheromone.na")}</dd>
            <dt>{t("pheromone.halfLife")}</dt>
            <dd>{finding?.halfLifeSec != null ? formatAgeSec(finding.halfLifeSec) : t("pheromone.na")}</dd>
            <dt>{t("pheromone.age")}</dt>
            <dd>{age != null ? formatAgeSec(age) : t("pheromone.na")}</dd>
            {finding?.kind && <><dt>{t("pheromone.kind")}</dt><dd>{finding.kind}</dd></>}
            {finding?.target && <><dt>{t("pheromone.target")}</dt><dd>{finding.target}</dd></>}
          </dl>
        </div>
      )}
    </div>
  );
}

function DeadEndItem({ d, t, zh, expanded, onToggle }: {
  d: { reason: string; actor: string; ts: number };
  t: (k: string, v?: Record<string, string | number>) => string;
  zh: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  const when = relTime(d.ts, zh);
  return (
    <div className={`evi-item d ${expanded ? "expanded" : ""}`.trim()}>
      <div className="evi-head">
        <button
          type="button"
          className="evi-row"
          aria-expanded={expanded}
          title={t(expanded ? "evidence.collapseFact" : "evidence.expandFact")}
          onClick={onToggle}
        >
          <span className="evi-fact">{d.reason}</span>
          <span className="evi-meta-inline">
            <span>{t("evidence.deadShort")}</span>
            <span>{d.actor}</span>
            {when && <span>{when}</span>}
          </span>
          <Icon name="chevronDown" size={13} />
        </button>
        <CopyFact text={d.reason} t={t} />
      </div>
      {expanded && (
        <div className="evi-detail">
          <dl className="evi-prov">
            <dt>{t("insp.actor")}</dt><dd>{d.actor}</dd>
            {when && <><dt>{t("evidence.sortLabel")}</dt><dd className="evi-when">{when}</dd></>}
          </dl>
        </div>
      )}
    </div>
  );
}

export function EvidenceChain({ deck, onOpenWorker }: {
  deck: DeckState;
  /** provenance jump: source worker → the worker detail panel. */
  onOpenWorker?: (id: string) => void;
}) {
  const t = useT();
  const { lang } = useLang();
  const zh = lang === "zh";
  // Live pheromone clock (5s decay refresh while live; frozen at finishedAt
  // for finished/replayed runs). Findings-less decks never start the timer.
  const nowSec = usePheromoneClock(deck);

  const [sortMode, setSortMode] = useState<SortMode>("newest");
  const [filter, setFilter] = useState<EvidenceFilter>("all");
  const [kindFilter, setKindFilter] = useState("");
  const [expOnly, setExpOnly] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    const v = readKey(SORT_KEY);
    if (v === "s") setSortMode("strength");
    else if (v != null) setSortMode(v === "1" ? "newest" : "oldest");
  }, []);
  const setSort = (next: SortMode) => {
    setSortMode(next);
    writeKey(SORT_KEY, next === "strength" ? "s" : next === "newest" ? "1" : "0");
  };

  const kinds = useMemo(() => findingKinds(deck.findings), [deck.findings]);

  // Facts arrive chronologically; newest-first = reverse. Keep source arrays
  // untouched (memo over a copy) so other panels reading deck stay stable.
  // Strength sort: linked-finding strength desc, N/A last, ts as the tiebreak.
  const order = useCallback((arr: BlackboardFact[]): BlackboardFact[] => {
    let out = arr;
    if (expOnly) out = out.filter((f) => findingForFactSeq(deck.findings, f.factSeq) != null);
    if (kindFilter) out = out.filter((f) => findingForFactSeq(deck.findings, f.factSeq)?.kind === kindFilter);
    if (sortMode === "strength") {
      out = [...out].sort((a, b) => {
        const sa = pheromoneStrength(findingForFactSeq(deck.findings, a.factSeq) ?? {}, nowSec);
        const sb = pheromoneStrength(findingForFactSeq(deck.findings, b.factSeq) ?? {}, nowSec);
        if (sa == null && sb == null) return tsMs(b.ts) - tsMs(a.ts);
        if (sa == null) return 1;
        if (sb == null) return -1;
        return sb - sa || tsMs(b.ts) - tsMs(a.ts);
      });
    } else if (sortMode === "newest") {
      out = [...out].reverse();
    }
    return out;
  }, [deck.findings, expOnly, kindFilter, nowSec, sortMode]);

  // A: review-retired facts (rejected/merged/superseded) are NOT evidence — they
  // failed review and must not appear in the proof chain.
  const verified = useMemo(
    () => order(deck.blackboard.facts.filter((f) => f.verified && !isFactRetired(f))),
    [deck.blackboard.facts, order],
  );
  const candidates = useMemo(
    () => order(deck.blackboard.facts.filter((f) => !f.verified && !isFactRetired(f))),
    [deck.blackboard.facts, order],
  );
  const deadEnds = useMemo(
    () => (sortMode === "oldest" ? deck.blackboard.deadEnds : [...deck.blackboard.deadEnds].reverse()),
    [deck.blackboard.deadEnds, sortMode],
  );
  const empty = verified.length === 0 && candidates.length === 0 && deadEnds.length === 0;
  const total = verified.length + candidates.length + deadEnds.length;
  const actorCount = useMemo(() => new Set([
    ...deck.blackboard.facts.map((f) => f.actor),
    ...deck.blackboard.deadEnds.map((d) => d.actor),
  ].filter(Boolean)).size, [deck.blackboard.facts, deck.blackboard.deadEnds]);
  const filterButtons: { key: EvidenceFilter; label: string; n: number }[] = [
    { key: "all", label: t("evidence.all"), n: total },
    { key: "verified", label: t("evidence.verifiedShort"), n: verified.length },
    { key: "candidates", label: t("evidence.candidatesShort"), n: candidates.length },
    { key: "dead", label: t("evidence.deadShort"), n: deadEnds.length },
  ];
  const toggleExpanded = (id: string) =>
    setExpandedIds((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="panel-scroll-wrap evidence-panel">
      <div className="evi-toolbar">
        <div className="evi-toolbar-title">
          <div className="panel-title">{t("evidence.title")}</div>
          <div className="evi-summary">
            <span>{t("evidence.total", { n: total })}</span>
            <span>{t("evidence.actors", { n: actorCount })}</span>
            <span>{sortMode === "strength" ? t("evidence.sortStrength") : sortMode === "newest" ? t("evidence.sortNewest") : t("evidence.sortOldest")}</span>
          </div>
        </div>
        {!empty && (
          <div className="evi-controls">
            <div className="evi-filter" role="tablist" aria-label={t("evidence.filterLabel")}>
              {filterButtons.map((b) => (
                <button
                  key={b.key}
                  type="button"
                  role="tab"
                  aria-selected={filter === b.key}
                  className={`evi-filter-btn ${filter === b.key ? "on" : ""}`.trim()}
                  onClick={() => setFilter(b.key)}
                >
                  <span>{b.label}</span>
                  <b>{b.n}</b>
                </button>
              ))}
            </div>
            <div className="evi-sort" role="group" aria-label={t("evidence.sortLabel")}>
              <button
                type="button"
                className={`evi-sort-btn ${sortMode === "newest" ? "on" : ""}`.trim()}
                aria-pressed={sortMode === "newest"}
                onClick={() => setSort("newest")}
              >
                {t("evidence.sortNewest")}
              </button>
              <button
                type="button"
                className={`evi-sort-btn ${sortMode === "oldest" ? "on" : ""}`.trim()}
                aria-pressed={sortMode === "oldest"}
                onClick={() => setSort("oldest")}
              >
                {t("evidence.sortOldest")}
              </button>
              <button
                type="button"
                className={`evi-sort-btn ${sortMode === "strength" ? "on" : ""}`.trim()}
                aria-pressed={sortMode === "strength"}
                title={t("pheromone.note")}
                onClick={() => setSort("strength")}
              >
                {t("evidence.sortStrength")}
              </button>
            </div>
          </div>
        )}
        {!empty && (
          <div className="evi-controls2">
            <label className="evi-kind-label">
              {t("evidence.filterKind")}
              <select
                className="evi-kind"
                value={kindFilter}
                onChange={(e) => setKindFilter(e.target.value)}
                aria-label={t("evidence.filterKind")}
              >
                <option value="">{t("evidence.allKinds")}</option>
                {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
            <button
              type="button"
              className={`evi-sort-btn evi-exp-toggle ${expOnly ? "on" : ""}`.trim()}
              aria-pressed={expOnly}
              title={t("pheromone.note")}
              onClick={() => setExpOnly((v) => !v)}
            >
              {t("evidence.experimentalOnly")}
            </button>
          </div>
        )}
      </div>
      <div className="panel-scroll evi-scroll">
      {!empty && (
        <div className="evi-density-note">{t("evidence.clickHint")}</div>
      )}
      {empty ? (
        <PanelEmpty icon="layers" title={t("evidence.empty")} hint={t("evidence.emptyHint")} />
      ) : (
        <>
          {(filter === "all" || filter === "verified") && verified.length > 0 && (
            <div className="evi-group verified">
              <div className="evi-group-h">{t("evidence.verified", { n: verified.length })}</div>
              {verified.map((f, i) => {
                const id = factKey(f, "v", i);
                return (
                  <FactItem
                    key={id} f={f} t={t} zh={zh}
                    expanded={expandedIds.has(id)}
                    onToggle={() => toggleExpanded(id)}
                    finding={findingForFactSeq(deck.findings, f.factSeq)}
                    nowSec={nowSec}
                    onOpenWorker={onOpenWorker}
                  />
                );
              })}
            </div>
          )}
          {(filter === "all" || filter === "candidates") && candidates.length > 0 && (
            <div className="evi-group candidates">
              <div className="evi-group-h">{t("evidence.candidates", { n: candidates.length })}</div>
              {candidates.map((f, i) => {
                const id = factKey(f, "c", i);
                return (
                  <FactItem
                    key={id} f={f} t={t} zh={zh}
                    expanded={expandedIds.has(id)}
                    onToggle={() => toggleExpanded(id)}
                    finding={findingForFactSeq(deck.findings, f.factSeq)}
                    nowSec={nowSec}
                    onOpenWorker={onOpenWorker}
                  />
                );
              })}
            </div>
          )}
          {(filter === "all" || filter === "dead") && deadEnds.length > 0 && (
            <div className="evi-group dead">
              <div className="evi-group-h">{t("evidence.dead", { n: deadEnds.length })}</div>
              {deadEnds.map((d, i) => (
                <DeadEndItem
                  key={`d${d.actor}-${d.ts}-${i}`}
                  d={d}
                  t={t}
                  zh={zh}
                  expanded={expandedIds.has(`d${d.actor}-${d.ts}-${i}`)}
                  onToggle={() => toggleExpanded(`d${d.actor}-${d.ts}-${i}`)}
                />
              ))}
            </div>
          )}
        </>
      )}
      </div>
    </div>
  );
}
