"use client";

import { useState } from "react";
import { useRuns } from "@/lib/useRunsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

/** Read-only run overview: creation and management live in the deck's rail. */

function statusTone(run: { status: string; solved?: boolean; finished?: boolean }): string {
  if (run.status === "running") return "ds-badge--cyan";
  if (run.solved) return "ds-badge--green";
  if (run.finished) return "ds-badge--muted";
  return "ds-badge--muted";
}

function statusLed(run: { status: string; solved?: boolean; finished?: boolean }): string {
  if (run.status === "running") return "ds-led--cyan";
  if (run.solved) return "ds-led--green";
  if (run.finished) return "ds-led";
  return "ds-led--amber";
}

export function RunsList() {
  const t = useT();
  const { runs, loading, error } = useRuns();
  const [filter, setFilter] = useState<"all" | "running" | "finished" | "solved">("all");

  const filteredRuns =
    filter === "all"
      ? runs
      : runs.filter((run) => {
          if (filter === "running") return run.status === "running";
          if (filter === "finished") return run.finished || run.solved;
          if (filter === "solved") return run.solved === true;
          return false;
        });

  const flagCountFor = (run: { flag_count?: number; flags?: string[] }): number =>
    run.flag_count ?? run.flags?.length ?? 0;

  return (
    <div className="ds-page ds-page--wide">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("runs.title")}</h1>
        </div>
        <div className="ds-actions">
          <Link href="/deck" className="ds-btn ds-btn--primary">{t("dash.openDeck")}</Link>
        </div>
      </div>

      <p className="ds-hint">{t("runs.manageHint")}</p>

      <div className="ds-toolbar">
        {(["all", "running", "finished", "solved"] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setFilter(option)}
            className={"ds-chip" + (filter === option ? " ds-chip--on" : "")}
          >
            {option === "all"
              ? t("common.all")
              : option === "running"
                ? t("runs.filterRunning")
                : option === "finished"
                  ? t("runs.filterFinished")
                  : t("runs.filterSolved")}
          </button>
        ))}
      </div>

      {loading && <div className="ds-loading">{t("common.loading")}</div>}
      {error && <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>}

      {!loading && filteredRuns.length === 0 && <div className="ds-empty">{t("runs.empty")}</div>}

      {filteredRuns.length > 0 && (
        <div className="ds-table" style={{ "--cols": "110px 1fr 130px 80px 70px 100px" } as React.CSSProperties}>
          <div className="ds-table-head">
            <span>{t("runs.colRun")}</span>
            <span>{t("runs.colName")}</span>
            <span>{t("runs.colStatus")}</span>
            <span className="ds-cell-right">{t("runs.colFlags")}</span>
            <span className="ds-cell-center">{t("runs.colSolved")}</span>
            <span className="ds-cell-right">{t("runs.colActions")}</span>
          </div>

          {filteredRuns.map((run) => (
            <div key={run.run_id} className="ds-row">
              <span className="ds-code">{run.run_id.substring(0, 8)}</span>
              <span className="ds-bright" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                {run.name || run.run_id}
              </span>
              <span>
                <span className={"ds-led " + statusLed(run)} aria-hidden />{" "}
                <span className={"ds-badge " + statusTone(run)}>{run.status}</span>
              </span>
              <span className="ds-cell-right ds-muted">{flagCountFor(run)}</span>
              <span className="ds-cell-center">{run.solved ? "✓" : "—"}</span>
              <span className="ds-row-actions">
                <Link href={`/run/${encodeURIComponent(run.run_id)}`} className="ds-btn ds-btn--sm">
                  {t("common.open")}
                </Link>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
