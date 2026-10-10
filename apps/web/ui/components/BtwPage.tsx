"use client";

/**
 * Read-only Ask Progress Observer workspace for one Run. Answers are grounded in the
 * evidence pack and never mutate the swarm, SharedGraph, or provenance gate.
 */

import { useEffect, useRef } from "react";
import { Icon } from "@/components/Icon";
import { useT } from "@/lib/i18n";
import { observerSourceLabel, useBtwChat } from "@/lib/btwChat";
import { detailUrlForRun } from "@/lib/runRoute";
import { BtwMessageBody } from "@/components/btwMarkdown";
import { useRunObservability } from "@/lib/useRun";
const QUICK_ASKS = [
  "总结当前 Run 的进展与证据缺口",
  "当前有哪些 open intents，分别由谁执行？",
  "哪些路线已失败或被压制？",
  "哪些候选证据仍需要验证？",
];

export function BtwPage({ runId }: { runId: string }) {
  const t = useT();
  const chat = useBtwChat(runId);
  const observability = useRunObservability(runId);
  const { turns, input, setInput, streaming, error, workerStatus, send } = chat;
  const { snapshot } = observability;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns, streaming]);

  const overCap = chat.transcriptChars > chat.maxChars;

  return (
    <div className="btw-page ask-progress-page">
      <section className="btw-page-chat" aria-label={t("btw.title")}>
        <header className="ask-progress-heading">
          <span className="eyebrow">READ-ONLY RUN Q&amp;A</span>
          <h2>{t("btw.title")}</h2>
          <p>基于当前 Run 的 Evidence、Intent、路线和事件回答；不会改变调度，也不会产生可接受的 Flag。</p>
        </header>
        {snapshot && (
          <section className="observer-progress-card" aria-label="Observer realtime progress">
            <div className="observer-progress-head"><span className="eyebrow">LIVE RUN SNAPSHOT</span><strong>{snapshot.health.status}</strong></div>
            <div className="observer-progress-grid">
              <span>Reason <b>{snapshot.binding.roles.find((row) => row.role === "reason")?.status || "unknown"}</b></span>
              <span>Worker <b>{snapshot.binding.roles.find((row) => row.role === "worker")?.status || "unknown"}</b></span>
              <span>Reviewer <b>{snapshot.binding.roles.find((row) => row.role === "review")?.status || "unknown"}</b></span>
              <span>Evidence gap <b>{snapshot.knowledge.inherited.length} inherited</b></span>
            </div>
            {snapshot.binding.roles.filter((row) => row.operator_attention || row.status === "fallback_active").map((row) => (
              <div className="observer-progress-alert" key={row.role}>
                <Icon name="alert" size={13} /><span>{row.role}: {row.status}{row.recovery?.at(-1)?.blocked_reason ? ` · ${row.recovery.at(-1)?.blocked_reason}` : ""}</span>
              </div>
            ))}
          </section>
        )}
        <div className="btw-quick" aria-label="Quick questions">
          {QUICK_ASKS.map((q) => (
            <button key={q} className="btw-quick-btn" disabled={streaming}
              onClick={() => send(q)} title={q}>{q}</button>
          ))}
        </div>
        <div className="btw-scroll" ref={scrollRef}>
          {turns.length === 0 && !streaming && (
            <div className="btw-empty">{t("btw.pageEmpty")}</div>
          )}
          {turns.map((turn, i) => (
            <div key={i} className={`btw-msg btw-${turn.role}`}>
              <div className="btw-msg-role">{turn.role === "user" ? "你" : "观察员"}</div>
              <div className="btw-msg-body">
                <BtwMessageBody role={turn.role} content={turn.content} streaming={streaming} />
              </div>
              {turn.role === "assistant" && (turn.observerSource || turn.evidenceRefs?.length || turn.uncertainties?.length) ? (
                <div className="btw-turn-meta">
                  {turn.observerSource && <span className="btw-source-label">{observerSourceLabel(turn.observerSource)}</span>}
                  {turn.fallbackUsed && <span className="btw-source-note">configured lightweight fallback</span>}
                  {turn.evidenceRefs?.length ? (
                    <div className="btw-refs">
                      <span className="btw-refs-label">{t("btw.evidenceRefs")}</span>
                      {turn.evidenceRefs.map((ref) => (
                        <a key={ref} className="evi-link" href={`${detailUrlForRun(runId, "evidence")}?fact=${encodeURIComponent(ref)}`}>{ref}</a>
                      ))}
                    </div>
                  ) : null}
                  {turn.uncertainties?.length ? (
                    <div className="btw-unknowns">
                      <span className="btw-refs-label">{t("btw.uncertainties")}</span>
                      <ul>{turn.uncertainties.map((u, j) => <li key={j}>{u}</li>)}</ul>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
          {workerStatus && <div className="btw-status" role="status">{workerStatus}</div>}
          {error && <div className="btw-error" role="alert">{error}</div>}
          {overCap && <div className="btw-error">{t("btw.overCap")}</div>}
        </div>
        <div className="btw-input-row">
          <textarea
            className="btw-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t("btw.placeholder")}
            disabled={streaming}
            rows={3}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
          />
          <button className="btw-send" disabled={streaming || !input.trim()}
            onClick={() => send(input)} title="Enter 发送 / Shift+Enter 换行">
            {streaming ? "…" : t("btw.send")}
          </button>
        </div>
      </section>

      <aside className="btw-page-context" aria-label={t("btw.contextTitle")}>
        <div className="btw-ctx-head"><Icon name="eye" size={14} /> {t("btw.contextTitle")}</div>
        <p className="btw-ctx-text">{t("btw.contextIntro")}</p>
        <ul className="btw-ctx-list">
          <li>{t("btw.ctxFacts")}</li>
          <li>{t("btw.ctxIntents")}</li>
          <li>{t("btw.ctxDeadEnds")}</li>
          <li>{t("btw.ctxEvents")}</li>
          <li>{t("btw.ctxArtifacts")}</li>
        </ul>
        <div className="ask-progress-boundary"><Icon name="lock" size={14} /><div><strong>只读 Observer 边界</strong><p>{t("btw.contextNote")}</p></div></div>
        <div className="btw-ctx-actions">
          <a className="ask-progress-evidence-link" href={detailUrlForRun(runId, "evidence")}><Icon name="check" size={13} />{t("panelbtn.evidence")}</a>
          <button className="btw-quick-btn" onClick={chat.clear} disabled={chat.streaming}>{t("btw.clear")}</button>
        </div>
        <p className="btw-ctx-persist">{t("btw.persistNote")}</p>
      </aside>
    </div>
  );
}
