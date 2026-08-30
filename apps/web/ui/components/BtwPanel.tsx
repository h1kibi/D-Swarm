"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { useT } from "@/lib/i18n";
import { useBtwChat } from "@/lib/btwChat";
import { BtwMessageBody } from "@/components/btwMarkdown";

/**\n * BTW side-query worker — a right-side drawer for read-only Q&A over a run.\n *\n * The operator asks quick questions (summarize progress, which worker is on\n * which line, ...) and gets a bounded read-only answer. Normal turns do not\n * start a shell worker; explicit deep-audit turns may use the isolated one-shot\n * worker path. BTW never joins the swarm, consumes no max-worker slot, or writes\n * graph/cost state.\n *\n * Multi-turn: the transcript lives ONLY in this component's local state and is\n * sent with each request so the fixed observer model can retain conversational\n * context. Closing the drawer (Esc / backdrop / button) drops the whole\n * transcript — nothing is persisted server-side. Switching runs clears it\n * (different runs' contexts must not mix).\n *\n * Open/close is OWNED by page.tsx (same pattern as CommandPalette) so a single\n * global Esc handler arbitrates layering. This component is a pure modal: it\n * renders nothing when `open` is false.\n */

export interface BtwPanelProps {
  open: boolean;
  onClose: () => void;
  runId: string;
}

const QUICK_ASKS = [
  "总结当前进展",
  "当前有哪些 open intents?",
  "走过但失败的 dead-end 方向有哪些?",
  "目前有几条候选证据? 已验证几条?",
];

// Rough transcript cap (chars). Server also caps; this is the client line of
// defense so a long conversation doesn't balloon the request body.
export function BtwPanel({ open, onClose, runId }: BtwPanelProps) {
  const t = useT();
  const chat = useBtwChat(runId);
  const { turns, input, setInput, streaming, error, workerStatus, send } = chat;
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll the conversation to the bottom as deltas stream in.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [turns, streaming]);

  // Abort any in-flight stream when the drawer closes.
  useEffect(() => {
    if (!open) chat.abort();
  }, [open, chat]);

  // Esc closes (stopPropagation so it doesn't also close a panel beneath).
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
  };


  if (!open) return null;

  // rough client-side transcript cap so the request body stays bounded
  const overCap = chat.transcriptChars > chat.maxChars;

  return (
    <div className="modal-backdrop btw-backdrop" onClick={onClose} onKeyDown={onKey}>
      <div
        className="btw-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="BTW observer"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="btw-head">
          <span className="btw-title">
            <Icon name="eye" size={15} /> {t("btw.title")}
          </span>
          <span className="btw-sub" title={runId}>{runId}</span>
          <span className="btw-spacer" />
          <button className="btw-x" onClick={chat.clear} disabled={chat.streaming}
            title={t("btw.clear")} aria-label="clear">
            <Icon name="refresh" size={13} />
          </button>
          <button className="btw-x" onClick={onClose} aria-label="close" title="Esc">
            <Icon name="x" size={15} />
          </button>
        </div>

        <div className="btw-quick">
          {QUICK_ASKS.map((q) => (
            <button
              key={q}
              className="btw-quick-btn"
              disabled={streaming}
              onClick={() => send(q)}
              title={q}
            >
              {q}
            </button>
          ))}
        </div>

        <div className="btw-scroll" ref={scrollRef}>
          {turns.length === 0 && !streaming && (
            <div className="btw-empty">{t("btw.empty")}</div>
          )}
          {turns.map((turn, i) => (
            <div key={i} className={`btw-msg btw-${turn.role}`}>
              <div className="btw-msg-role">{turn.role === "user" ? "你" : "观察员"}</div>
              <div className="btw-msg-body">
                <BtwMessageBody
                  role={turn.role}
                  content={turn.content}
                  streaming={streaming}
                />
              </div>
            </div>
          ))}
          {workerStatus && <div className="btw-status" role="status">{workerStatus}</div>}
          {error && <div className="btw-error" role="alert">{error}</div>}
          {overCap && (
            <div className="btw-error">transcript 过长，建议关闭抽屉重新开始。</div>
          )}
        </div>

        <div className="btw-input-row">
          <textarea
            className="btw-input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t("btw.placeholder")}
            disabled={streaming}
            rows={2}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input);
              }
            }}
          />
          <button
            className="btw-send"
            disabled={streaming || !input.trim()}
            onClick={() => send(input)}
            title="Enter 发送 / Shift+Enter 换行"
          >
            {streaming ? "…" : t("btw.send")}
          </button>
        </div>
      </div>
    </div>
  );
}
