"use client";

import { useEffect, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { Icon } from "@/components/Icon";

/**
 * Operator Command Bar (docs/07 §5.6) — the persistent bottom bar. Command
 * types: Hint / Redirect / Focus (text) and Pause / Resume / Stop (immediate).
 * Everything goes through the existing HITL entry point; after sending, the
 * directive lands in the Decision Timeline as a queued entry whose lifecycle
 * chip advances as `operator_directive_changed` deltas arrive.
 *
 * The caption spells out the semantics (§P1-4): hint/redirect never interrupt
 * a running worker; redirect applies to the next dispatch; pause/stop act
 * immediately.
 */

type Action = "hint" | "redirect" | "focus" | "pause" | "resume" | "stop";
const TEXT_ACTIONS: Action[] = ["hint", "redirect", "focus"];
const IMMEDIATE_ACTIONS: Action[] = ["pause", "resume", "stop"];

export function OperatorBar({
  started,
  running,
  solvers,
  target,
  onTargetChange,
  focusNonce,
  onCommand,
}: {
  started: boolean;
  running: boolean;
  solvers: string[];
  target: string;
  onTargetChange: (target: string) => void;
  /** bump to focus the input (e.g. a worker card's Redirect button). */
  focusNonce?: number;
  onCommand: (target: string, action: string, text: string) => void;
}) {
  const t = useT();
  const [action, setAction] = useState<Action>("hint");
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const immediate = IMMEDIATE_ACTIONS.includes(action);

  useEffect(() => {
    if (focusNonce) inputRef.current?.focus();
  }, [focusNonce]);

  const send = () => {
    if (action === "stop") {
      if (!window.confirm(t("op.stopConfirm"))) return;
      onCommand(target, "stop", "");
      return;
    }
    if (action === "pause" || action === "resume") {
      onCommand(target, action, "");
      return;
    }
    const v = text.trim();
    if (!v) return;
    onCommand(target, action, v);
    setText("");
  };

  if (!started) {
    return (
      <footer className="opbar opbar-disabled" aria-label={t("op.title")}>
        <span className="opbar-hint">{t("op.draftHint")}</span>
      </footer>
    );
  }

  return (
    <footer className="opbar" aria-label={t("op.title")}>
      <div className="opbar-row">
        <select
          className="opbar-action"
          value={action}
          onChange={(e) => setAction(e.target.value as Action)}
          aria-label={t("op.title")}
        >
          {TEXT_ACTIONS.map((a) => (
            <option key={a} value={a}>{t(`op.action.${a}`)}</option>
          ))}
          {IMMEDIATE_ACTIONS.map((a) => (
            <option key={a} value={a}>{t(`op.action.${a}`)}</option>
          ))}
        </select>
        <select
          className="opbar-target"
          value={target}
          onChange={(e) => onTargetChange(e.target.value)}
          title={t("op.targetTitle")}
          aria-label={t("op.targetTitle")}
        >
          <option value="global">{t("op.target.all")}</option>
          {solvers.map((s) => (
            <option key={s} value={`solver:${s}`}>{s}</option>
          ))}
        </select>
        {!immediate && (
          <input
            ref={inputRef}
            className="opbar-input"
            data-composer-input
            value={text}
            disabled={!running && action !== "hint"}
            placeholder={t("op.placeholder")}
            aria-label={t("op.placeholder")}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
        )}
        <button
          type="button"
          className={`opbar-send ${action === "stop" ? "danger" : "primary"}`}
          onClick={send}
          disabled={!immediate && !text.trim()}
        >
          {immediate ? t(`op.action.${action}`) : t("op.send")}
          {!immediate && <Icon name="send" size={13} />}
        </button>
      </div>
      <div className="opbar-meta">
        <span>{t("op.scope")}</span>
        <span className="opbar-note">{t("op.note")}</span>
      </div>
    </footer>
  );
}
