"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useRuns, startRun, stopRun, deleteRun } from "@/lib/useRunsApi";
import Link from "next/link";
import styles from "./RunsList.module.css";

export function RunsList() {
  const router = useRouter();
  const { runs, loading, error, refresh } = useRuns();
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
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

  const handleLaunch = async () => {
    setLaunching(true);
    setLaunchError(null);
    const result = await startRun();
    if (result.ok && result.runId) {
      router.push(`/run/${encodeURIComponent(result.runId)}`);
      return;
    }
    setLaunchError(result.error || "Failed to create a run");
    setLaunching(false);
  };

  const handleStop = async (runId: string) => {
    if (!window.confirm("Stop this run?")) return;
    const result = await stopRun(runId);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to stop run");
    }
  };

  const handleDelete = async (runId: string) => {
    if (!window.confirm("Delete this run?")) return;
    const result = await deleteRun(runId);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to delete run");
    }
  };

  const flagCountFor = (run: { flag_count?: number; flags?: string[] }): number =>
    run.flag_count ?? run.flags?.length ?? 0;

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Runs</h1>
        <button onClick={handleLaunch} disabled={launching} className={styles.launchBtn}>
          {launching ? "Opening..." : "🚀 Launch Run"}
        </button>
      </div>

      <p className={styles.hint}>
        Launching mints a new run and opens it in the Command Deck, where you pick
        the Target and dispatch the prompt.
      </p>

      {launchError && <div className={styles.error}>{launchError}</div>}

      <div className={styles.filters}>
        {(["all", "running", "finished", "solved"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={filter === f ? styles.filterActive : styles.filter}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>

      {loading && <div className={styles.loading}>Loading runs...</div>}
      {error && <div className={styles.error}>Error: {error}</div>}

      {!loading && filteredRuns.length === 0 && <div className={styles.empty}>No runs found</div>}

      <div className={styles.table}>
        <div className={styles.tableHeader}>
          <div className={styles.col_id}>Run ID</div>
          <div className={styles.col_target}>Name</div>
          <div className={styles.col_status}>Status</div>
          <div className={styles.col_flags}>Flags</div>
          <div className={styles.col_time}>Solved</div>
          <div className={styles.col_actions}>Actions</div>
        </div>

        {filteredRuns.map((run) => (
          <div key={run.run_id} className={styles.tableRow}>
            <div className={styles.col_id}>
              <code>{run.run_id.substring(0, 8)}</code>
            </div>
            <div className={styles.col_target}>
              <span>{run.name || run.run_id}</span>
            </div>
            <div className={styles.col_status}>
              <span className={styles.status + " " + styles[`status_${run.status}`]}>
                {run.status}
              </span>
            </div>
            <div className={styles.col_flags}>
              <span className={run.solved ? styles.solved : ""}>{flagCountFor(run)}</span>
            </div>
            <div className={styles.col_time}>{run.solved ? "✓" : "—"}</div>
            <div className={styles.col_actions}>
              <Link href={`/run/${encodeURIComponent(run.run_id)}`} className={styles.viewBtn}>
                Open
              </Link>
              {run.status === "running" && (
                <button onClick={() => handleStop(run.run_id)} className={styles.stopBtn}>
                  Stop
                </button>
              )}
              <button onClick={() => handleDelete(run.run_id)} className={styles.deleteBtn}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
