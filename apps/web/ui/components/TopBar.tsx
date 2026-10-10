"use client";

import { swarmDigest, type DeckState } from "@/lib/events";
import { useLang, useT } from "@/lib/i18n";
import { type StageInfo } from "@/lib/timeline";
import type { Stage } from "@/lib/normalize";
import { Icon } from "@/components/Icon";
import { StageRail } from "@/components/StageRail";

/**
 * Top status bar (docs/07 §5.1) — the Command-center chrome: D-Swarm brand,
 * current run/target, the Stage Rail, flag progress, cost, connection state,
 * and the high-frequency controls (Pause / Resume / Stop / Resolve). Retired
 * run-mode and legacy-engine vocabulary never appears here (docs/07 §5.1).
 */
export function TopBar({
  deck,
  connected,
  running,
  stageInfo,
  inspectorOpen,
  railCollapsed,
  onToggleRail,
  onToggleInspector,
  onJumpStage,
  onCommand,
  onResolve,
  resolveBlocked = false,
  onNewRun,
  onOpenBtw,
  theme,
  onToggleTheme,
}: {
  deck: DeckState;
  connected: boolean;
  running: boolean;
  stageInfo: StageInfo;
  inspectorOpen: boolean;
  railCollapsed: boolean;
  onToggleRail: () => void;
  onToggleInspector: () => void;
  onJumpStage: (stage: Stage) => void;
  onCommand: (target: string, action: string, text: string) => void;
  onResolve: (text?: string) => void;
  resolveBlocked?: boolean;
  onNewRun?: () => void;
  onOpenBtw?: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}) {
  const t = useT();
  const { lang, setLang } = useLang();
  const digest = swarmDigest(deck);
  const started = deck.started;
  const paused = digest.phase === "paused";
  const need = Math.max(1, deck.expectedFlags || 1);
  const connLabel = connected
    ? t("topbar.live")
    : deck.finished
      ? t("topbar.replay")
      : !started
        ? t("convo.idle")
        : t("convo.disconnected");
  const connClass = connected ? "live" : deck.finished || !started ? "idle" : "off";

  const stop = () => {
    if (window.confirm(t("op.stopConfirm"))) onCommand("global", "stop", "");
  };
  return (
    <header className="topbar" aria-label={t("topbar.aria")}>
      <button
        className={`icon-btn topbar-rail-toggle ${railCollapsed ? "is-collapsed" : ""}`}
        onClick={onToggleRail}
        title={railCollapsed ? (lang === "zh" ? "显示目标列表" : "Show targets") : (lang === "zh" ? "隐藏目标列表" : "Hide targets")}
        aria-label={railCollapsed ? (lang === "zh" ? "显示目标列表" : "Show targets") : (lang === "zh" ? "隐藏目标列表" : "Hide targets")}
        aria-pressed={!railCollapsed}
      ><Icon name={railCollapsed ? "panel" : "menu"} /></button>
      <span className="brand topbar-brand"><span>D-Swarm</span></span>

      {started && (
        <span className="topbar-run">
          <span className="topbar-run-name">{deck.challengeName || deck.runId}</span>
          {deck.target && (
            <span className="topbar-target" title={deck.target}>
              {t("topbar.target")}: {deck.target}
            </span>
          )}
        </span>
      )}

      {started && (
        <div className="topbar-stage">
          <StageRail info={stageInfo} onJump={onJumpStage} />
          <span className={`run-status-chip rs-${stageInfo.status}`}
            title={t("topbar.statusTitle")}>{t(`runStatus.${stageInfo.status}`)}</span>
        </div>
      )}

      <span className="spacer" />

      {started && (
        <>
          <span className="topbar-flags" title={t("topbar.flagsTitle")}>
            <Icon name="flag" size={13} /> {deck.flags.length}/{need}
          </span>
          <span className="topbar-cost" title={t("topbar.costTitle")}>${deck.usd.toFixed(2)}</span>
        </>
      )}
      <span className={`topbar-conn ${connClass}`} role="status">{connLabel}</span>

      {started && running && (
        paused ? (
          <button className="topbar-ctl" onClick={() => onCommand("global", "resume", "")}>
            {t("topbar.resume")}
          </button>
        ) : (
          <button className="topbar-ctl" onClick={() => onCommand("global", "pause", "")}>
            {t("topbar.pause")}
          </button>
        )
      )}
      {started && running && (
        <button className="topbar-ctl danger" onClick={stop} title={t("topbar.stopTitle")}>
          {t("topbar.stop")}
        </button>
      )}
      {started && deck.finished && (resolveBlocked ? (
        <>
          <button
            className="topbar-ctl danger"
            disabled
            title={lang === "zh" ? "该 Run 的历史绑定与当前 Target 不一致，不能继续恢复" : "This Run does not match its Target and cannot be resumed"}
          >
            {lang === "zh" ? "无法继续" : "Cannot continue"}
          </button>
          {onNewRun && (
            <button className="topbar-ctl" onClick={onNewRun}>
              {lang === "zh" ? "新建 Run" : "New Run"}
            </button>
          )}
        </>
      ) : (
        <button className="topbar-ctl" onClick={() => onResolve()}>
          {t("topbar.resolve")}
        </button>
      ))}
      {started && (
        <button
          className={`icon-btn ${inspectorOpen ? "on" : ""}`}
          onClick={onToggleInspector}
          title={t("topbar.toggleInspector")}
          aria-label={t("topbar.toggleInspector")}
          aria-pressed={inspectorOpen}
        ><Icon name="panel" /></button>
      )}
      {/* BTW observes a RUN: hide on drafts so the button can never lead to
          the legacy sidebar path (it now opens the dedicated /run/<id>/btw page
          for real runs). */}
      {onOpenBtw && deck.started && (
        <button className="btw-btn" onClick={onOpenBtw} title={t("btw.btnTitle")} aria-label={t("btw.btnTitle")}>
          {t("btw.btn")}
        </button>
      )}
      <button
        className="icon-btn"
        onClick={onToggleTheme}
        title={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
        aria-label={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
      ><Icon name={theme === "dark" ? "sun" : "moon"} /></button>
      <button
        className="lang-btn"
        onClick={() => setLang(lang === "zh" ? "en" : "zh")}
        title={t("lang.toggleTitle")}
        aria-label={t("lang.toggleTitle")}
      >{t("lang.toggle")}</button>
    </header>
  );
}
