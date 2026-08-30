"use client";

import { STAGES, type Stage } from "@/lib/normalize";
import { stageRailStates, type StageInfo } from "@/lib/timeline";
import { useT } from "@/lib/i18n";

/**
 * Stage Rail (docs/07 §5.3): QUEUE─PREPARE─RECON─REASON─DISPATCH─EXECUTE─
 * REVIEW─FINALIZE. Visual conventions: current = solid deep green, completed =
 * green check, pending = grey dot, waiting/degraded = amber, failed = red.
 * A derived (legacy-replay) stage is marked approximate. Clicking a stage asks
 * the Decision Timeline to scroll to that stage's first item.
 */
export function StageRail({
  info,
  onJump,
}: {
  info: StageInfo;
  onJump?: (stage: Stage) => void;
}) {
  const t = useT();
  return (
    <ol className="stage-rail" aria-label={t("stage.railAria")}>
      {stageRailStates(info).map(({ stage, state }, i) => {
        const active = state === "active";
        const cls = [
          "stage-node",
          `st-${state}`,
          active && info.status === "degraded" ? "is-waiting" : "",
          active && info.waiting ? "is-waiting" : "",
          active && info.failed ? "is-failed" : "",
        ].filter(Boolean).join(" ");
        const approx = active && info.derived ? ` · ${t("stage.approx")}` : "";
        const stateNote = active && info.failed
          ? ` · ${t("stage.failed")}`
          : active && info.status === "degraded"
            ? ` · ${t("stage.degraded")}`
            : active && info.waiting
              ? ` · ${t("stage.waiting")}`
              : "";
        return (
          <li key={stage} className="stage-cell">
            {i > 0 && (
              <span className={`stage-link ${state !== "pending" ? "done" : ""}`} aria-hidden="true" />
            )}
            <button
              type="button"
              className={cls}
              aria-current={active ? "step" : undefined}
              title={`${t(`stage.${stage}`)}${stateNote}${approx} — ${t("stage.jump")}`}
              onClick={() => onJump?.(stage)}
            >
              <span className="stage-dot" aria-hidden="true">{state === "completed" ? "✓" : ""}</span>
              <span className="stage-label">
                {t(`stage.${stage}`)}
                {active && info.derived && <span className="stage-approx" title={t("stage.approx")}>≈</span>}
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
