"use client";

/**
 * Full-page BTW observer chat (/run/<id>/btw): the sidebar quick-ask drawer's
 * big sibling. Same shared chat state (lib/btwChat), so the conversation is
 * identical on both surfaces and survives reloads; this page adds room to
 * breathe — chat left, observer-context right (what the evidence pack covers,
 * per-answer evidence links into the evidence chain, flagged uncertainties).
 */

import { useEffect, useRef } from "react";
import { Icon } from "@/components/Icon";
import { useT } from "@/lib/i18n";
import { useBtwChat } from "@/lib/btwChat";
import { detailUrlForRun } from "@/lib/runRoute";
import { BtwMessageBody } from "@/components/btwMarkdown";

const QUICK_ASKS = [
  "总结当前进展",
  "当前有哪些 open intents?",
  "走过但失败的 dead-end 方向有哪些?",
  "目前有几条候选证据? 已验证几条?",
];

export function BtwPage({ runId }: { runId: string }) {
  const t = useT();
  const chat = useBtwChat(runId);
  const { turns, input, setInput, streaming, error, workerStatus, send } = chat;
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns, streaming]);

  const overCap = chat.transcriptChars > chat.maxChars;

  return (
    <div className="btw-page">
      <section className="btw-page-chat" aria-label={t("btw.title")}>
        <div className="btw-quick">
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
              {turn.role === "assistant" && (turn.evidenceRefs?.length || turn.uncertainties?.length) ? (
                <div className="btw-turn-meta">
                  {turn.evidenceRefs?.length ? (
                    <div className="btw-refs">
                      <span className="btw-refs-label">{t("btw.evidenceRefs")}</span>
                      {turn.evidenceRefs.map((ref) => (
                        <a key={ref} className="evi-link"
                          href={`${detailUrlForRun(runId, "evidence")}?fact=${encodeURIComponent(ref)}`}>
                          {ref}
                        </a>
                      ))}
                    </div>
                  ) : null}
                  {turn.uncertainties?.length ? (
                    <div className="btw-unknowns">
                      <span className="btw-refs-label">{t("btw.uncertainties")}</span>
                      <ul>
                        {turn.uncertainties.map((u, j) => <li key={j}>{u}</li>)}
                      </ul>
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
        <div className="btw-ctx-head">
          <Icon name="eye" size={14} /> {t("btw.contextTitle")}
        </div>
        <p className="btw-ctx-text">{t("btw.contextIntro")}</p>
        <ul className="btw-ctx-list">
          <li>{t("btw.ctxFacts")}</li>
          <li>{t("btw.ctxIntents")}</li>
          <li>{t("btw.ctxDeadEnds")}</li>
          <li>{t("btw.ctxEvents")}</li>
          <li>{t("btw.ctxArtifacts")}</li>
        </ul>
        <p className="btw-ctx-note">{t("btw.contextNote")}</p>
        <div className="btw-ctx-actions">
          <a className="detail-tab" href={detailUrlForRun(runId, "evidence")}>{t("panelbtn.evidence")}</a>
          <button className="btw-quick-btn" onClick={chat.clear} disabled={chat.streaming}>
            {t("btw.clear")}
          </button>
        </div>
        <p className="btw-ctx-persist">{t("btw.persistNote")}</p>
      </aside>
    </div>
  );
}
