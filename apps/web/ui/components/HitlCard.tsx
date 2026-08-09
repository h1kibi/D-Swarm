"use client";

import { useEffect, useRef, useState } from "react";
import type { HitlRequest } from "@/lib/events";
import { useT } from "@/lib/i18n";
import { Icon } from "@/components/Icon";

/** A pending human-in-the-loop decision. Because a pending request PAUSES the
 *  whole swarm, the card is rendered high-priority (amber bar + alert icon +
 *  "needs your decision" heading). When the request carries `options`, each is a
 *  one-click answer button; the free-text input is always available for a custom
 *  answer (Enter submits). The FIRST pending card autofocuses its input so the
 *  operator can just type + Enter. `sending` disables controls until the request
 *  leaves deck.hitlRequests (a HITL_RESPONSE clears it). */
export function HitlCard({
  req, first, onAnswer, onDismiss,
}: {
  req: HitlRequest;
  first: boolean;
  onAnswer: (opt: string) => void;
  onDismiss?: () => void;
}) {
  const t = useT();
  const [free, setFree] = useState("");
  const [sending, setSending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const hasOptions = req.options.length > 0;
  // F: only an external_blocker actually freezes the swarm + needs an operator
  // answer; the other kinds are auto-handled (lane lock / route suppress / low-conf
  // candidate). An unclassified card (no needKind) defaults to blocking (back-compat).
  const pauses = req.pausesBehavior ?? true;
  const kindLabel = req.needKind ? t(`hitl.kind.${req.needKind}`) : t("hitl.title");
  // autofocus the topmost pending request's input — when the current first card
  // is answered and clears, the next one becomes `first` and grabs focus.
  useEffect(() => {
    if (first && pauses) inputRef.current?.focus();
  }, [first, pauses]);
  const submit = (value: string) => {
    const v = value.trim();
    if (!v || sending) return;
    setSending(true);
    onAnswer(v);
  };
  return (
    <div
      className={`hitl-card ${first ? "first" : ""} ${sending ? "sending" : ""} ${pauses ? "blocking" : "auto"}`}
      role="group"
      aria-label={t("hitl.region")}
    >
      <div className="hitl-head">
        <span className="hitl-ico" aria-hidden="true"><Icon name={pauses ? "alert" : "help"} size={16} /></span>
        <span className="hitl-title">{kindLabel}</span>
        {pauses
          ? <span className="hitl-blocking">{t("hitl.titleBlocking")}</span>
          : <span className="hitl-auto">{t("hitl.autoResolving")}</span>}
      </div>
      <div className="body">{req.promptZh || req.prompt}</div>
      {req.promptZh && req.promptZh !== req.prompt && (
        <details className="hitl-raw">
          <summary>{t("hitl.showOriginal")}</summary>
          <div>{req.prompt}</div>
        </details>
      )}
      {/* F: auto-resolving cards are informational — no input, the swarm handles it */}
      {pauses && <div className="hitl-opts">
        {req.options.map((o) => (
          <button key={o} type="button" disabled={sending} onClick={() => submit(o)}>{o}</button>
        ))}
        <input
          ref={inputRef}
          className="hitl-free"
          value={free}
          disabled={sending}
          aria-label={t("hitl.inputAria")}
          placeholder={hasOptions ? t("hitl.orType") : t("hitl.inputAria")}
          onChange={(e) => setFree(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(free); } }}
        />
        <button
          type="button"
          className="hitl-send"
          disabled={sending || !free.trim()}
          onClick={() => submit(free)}
        >
          {sending ? t("hitl.sending") : t("hitl.submit")}
        </button>
        {onDismiss && (
          <button
            type="button"
            className="hitl-dismiss"
            disabled={sending}
            title={t("hitl.dismiss.tip")}
            onClick={() => { setSending(true); onDismiss(); }}
          >
            {t("hitl.dismiss")}
          </button>
        )}
      </div>}
    </div>
  );
}
