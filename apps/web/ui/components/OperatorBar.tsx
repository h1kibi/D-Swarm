"use client";

import { useEffect, useRef, useState } from "react";
import { useLang, useT } from "@/lib/i18n";
import { Icon } from "@/components/Icon";
import type { SolverLane } from "@/lib/events";

type Action = "prompt" | "stop" | "resume" | "review";
const ACTIONS: Action[] = ["prompt", "stop", "resume", "review"];

type RecipientLane = SolverLane & { solverId: string };

function canonicalRecipient(value: string): string {
  return value.replace(/^solver:/, "").trim();
}

function roleFor(id: string, lane?: SolverLane): "reason" | "worker" | "review" {
  if (id === "reason" || id === "planner" || id === "coordinator") return "reason";
  if (lane?.role === "review" || (lane?.phase || "").toLowerCase().includes("review")) return "review";
  return "worker";
}

function roleLabel(role: "reason" | "worker" | "review", zh: boolean): string {
  if (role === "reason") return "Reason";
  if (role === "review") return zh ? "Reviewer" : "Reviewer";
  return zh ? "Worker" : "Worker";
}

function healthLabel(lane: SolverLane | undefined, zh: boolean): string {
  if (!lane) return zh ? "未观测" : "unobserved";
  const runtime = lane.runtime;
  if (runtime?.error) return zh ? "运行时错误" : "runtime error";
  if (runtime?.status) return String(runtime.status);
  if (lane.statusReason) return lane.statusReason;
  return lane.status || (zh ? "未知" : "unknown");
}

/** Persistent Run-scoped command bar. Review is a read-only Target workspace
 * entry point; it is intentionally not routed through HITL. */
export function OperatorBar({
  started,
  running,
  lanes,
  target,
  onTargetChange,
  focusNonce,
  onCommand,
  onReview,
  terminal = false,
  canReview = terminal,
}: {
  started: boolean;
  running: boolean;
  lanes: Record<string, SolverLane>;
  target: string;
  onTargetChange: (target: string) => void;
  /** bump to focus the input (e.g. a worker card's Redirect button). */
  focusNonce?: number;
  onCommand: (target: string, action: string, text: string, recipients?: string[]) => void;
  onReview: () => void;
  terminal?: boolean;
  canReview?: boolean;
}) {
  const t = useT();
  const { lang } = useLang();
  const zh = lang === "zh";
  const [action, setAction] = useState<Action>("prompt");
  const [text, setText] = useState("");
  const [selectedRecipients, setSelectedRecipients] = useState<string[]>(["all"]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusNonce) inputRef.current?.focus();
  }, [focusNonce]);

  // Worker detail actions seed the legacy `solver:<id>` spelling. Keep the
  // canonical recipient list in sync so the next Prompt cannot accidentally fan
  // out to all workers.
  useEffect(() => {
    const raw = target === "global" || target === "all"
      ? ["all"]
      : target.split(",").map(canonicalRecipient).filter(Boolean);
    setSelectedRecipients(raw.length ? raw : ["all"]);
  }, [target]);

  const toggleRecipient = (recipient: string) => {
    if (recipient === "all") {
      setSelectedRecipients(["all"]);
      onTargetChange("global");
      return;
    }
    const next = selectedRecipients.includes(recipient)
      ? selectedRecipients.filter((r) => r !== recipient)
      : [...selectedRecipients.filter((r) => r !== "all"), recipient];
    const finalRecipients = next.length === 0 ? ["all"] : next;
    setSelectedRecipients(finalRecipients);
    onTargetChange(finalRecipients.includes("all")
      ? "global"
      : finalRecipients.map((id) => `solver:${id}`).join(","));
  };

  const send = () => {
    const recipients = selectedRecipients.includes("all") ? ["all"] : selectedRecipients;
    if (action === "stop") {
      if (terminal) return;
      if (!window.confirm(t("op.stopConfirm"))) return;
      onCommand("global", "stop", "", ["all"]);
      return;
    }
    if (action === "resume") {
      onCommand("global", "resume", text.trim(), ["all"]);
      setText("");
      return;
    }
    if (action === "review") {
      if (canReview) onReview();
      return;
    }
    const value = text.trim();
    if (!value || !running) return;
    onCommand(recipients.join(","), "prompt", value, recipients);
    setText("");
  };

  if (!started) {
    return (
      <footer className="opbar opbar-disabled" aria-label={t("op.title")}>
        <span className="opbar-hint">{t("op.draftHint")}</span>
      </footer>
    );
  }

  const laneEntries: RecipientLane[] = Object.values(lanes)
    .filter((lane) => roleFor(lane.solverId, lane) !== "reason")
    .map((lane) => lane as RecipientLane);
  const reasonLane = lanes.reason || lanes.planner || lanes.coordinator;
  const entries: Array<{ id: string; lane?: RecipientLane; role: "reason" | "worker" | "review" }> = [
    { id: "reason", lane: reasonLane as RecipientLane | undefined, role: "reason" },
    ...laneEntries.map((lane) => ({ id: lane.solverId, lane, role: roleFor(lane.solverId, lane) })),
  ];
  const noText = action === "stop" || action === "review";
  const visibleActions: Action[] = terminal ? ["resume", "review"] : ACTIONS;
  const disabled = action === "review" ? !canReview : action === "stop" ? terminal : action === "prompt" ? !running || !text.trim() : false;
  return (
    <footer className="opbar" aria-label={t("op.title")}>
      <div className="opbar-row">
        <select
          className="opbar-action"
          value={visibleActions.includes(action) ? action : visibleActions[0]}
          onChange={(e) => setAction(e.target.value as Action)}
          aria-label={t("op.title")}
        >
          {visibleActions.map((item) => (
            <option key={item} value={item} disabled={item === "review" && !canReview}>
              {t(`op.action.${item}`)}{item === "review" && !canReview ? " · terminal Run only" : ""}
            </option>
          ))}
        </select>
        <div className="opbar-recipients" aria-label={t("op.targetTitle")}>
          <button
            type="button"
            className={`opbar-chip ${selectedRecipients.includes("all") ? "active" : ""}`}
            onClick={() => toggleRecipient("all")}
            aria-pressed={selectedRecipients.includes("all")}
          >
            {t("op.target.all")}
          </button>
          {entries.map(({ id, lane, role }) => {
            const active = selectedRecipients.includes(id);
            const online = lane ? lane.online !== false : false;
            const intent = lane?.intentId || t("swarm.noIntent");
            return (
              <button
                key={id}
                type="button"
                className={`opbar-chip ${active ? "active" : ""} ${online ? "online" : "offline"}`}
                onClick={() => toggleRecipient(id)}
                aria-pressed={active}
                title={`${roleLabel(role, zh)} · ${id} · ${intent} · ${healthLabel(lane, zh)}`}
              >
                <span className="opbar-chip-role">{roleLabel(role, zh)}</span>
                <strong>{id}</strong>
                <span className={`opbar-chip-online ${online ? "on" : "off"}`} aria-hidden="true" />
                <small>{intent}</small>
              </button>
            );
          })}
        </div>
        {!noText && (
          <input
            ref={inputRef}
            className="opbar-input"
            data-composer-input
            value={text}
            placeholder={action === "resume" ? t("op.action.resume") : t("op.placeholder")}
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
          disabled={disabled}
        >
          {action === "stop" || action === "review" ? t(`op.action.${action}`) : t("op.send")}
          {action !== "stop" && action !== "review" && <Icon name="send" size={13} />}
        </button>
      </div>
      <div className="opbar-meta">
        <span>{t("op.scope")}</span>
        <span className="opbar-note">{t("op.note")}</span>
        {terminal && <span className="opbar-terminal">{zh ? "终止 Run 可进入回顾" : "Review is available for terminated runs"}</span>}
      </div>
    </footer>
  );
}
