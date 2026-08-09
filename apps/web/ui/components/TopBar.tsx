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
  onToggleRail,
  onToggleInspector,
  onJumpStage,
  onCommand,
  onResolve,
  onOpenBtw,
  theme,
  onToggleTheme,
}: {
  deck: DeckState;
  connected: boolean;
  running: boolean;
  stageInfo: StageInfo;
  inspectorOpen: boolean;
  onToggleRail: () => void;
  onToggleInspector: () => void;
  onJumpStage: (stage: Stage) => void;
  onCommand: (target: string, action: string, text: string) => void;
  onResolve: (text?: string) => void;
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
    <header className="topbar motion-shell-piece" aria-label={t("topbar.aria")}>
      <button
        className="icon-btn"
        onClick={onToggleRail}
        title={t("convo.toggleRuns")}
        aria-label={t("convo.toggleRuns")}
      ><Icon name="menu" /></button>
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
      {started && deck.finished && (
        <button className="topbar-ctl" onClick={() => onResolve()}>
          {t("topbar.resolve")}
        </button>
      )}
      {started && (
        <button
          className={`icon-btn ${inspectorOpen ? "on" : ""}`}
          onClick={onToggleInspector}
          title={t("topbar.toggleInspector")}
          aria-label={t("topbar.toggleInspector")}
          aria-pressed={inspectorOpen}
        ><Icon name="panel" /></button>
      )}
      {onOpenBtw && (
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
