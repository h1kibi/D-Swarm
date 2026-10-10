"use client";

import { useState } from "react";
import { useRuns, startRun, stopRun, deleteRun } from "@/lib/useRunsApi";
import { useEvents } from "@/lib/useEventsApi";
import { useTargets } from "@/lib/useTargetsApi";
import Link from "next/link";
import styles from "./RunsList.module.css";

export function RunsList() {
  const { events } = useEvents();
  const [selectedEventId, setSelectedEventId] = useState<string>("");
  const { targets } = useTargets(selectedEventId);
  const { runs, loading, error, refresh } = useRuns(selectedEventId);

  const [showLauncher, setShowLauncher] = useState(false);
  const [launchData, setLaunchData] = useState({ event_id: "", target_id: "" });
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "running" | "completed" | "failed">("all");

  const filteredRuns =
    filter === "all"
      ? runs
      : runs.filter((r) => {
          if (filter === "running") return r.status === "running" || r.status === "pending";
          if (filter === "completed") return r.status === "completed";
          if (filter === "failed") return r.status === "failed" || r.status === "stopped";
          return false;
        });

  const handleLaunch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!launchData.event_id || !launchData.target_id) {
      setLaunchError("Please select both event and target");
      return;
    }

    setLaunching(true);
    setLaunchError(null);

    const result = await startRun({
      event_id: launchData.event_id,
      target_id: launchData.target_id,
    });

    if (result.ok) {
      setLaunchData({ event_id: "", target_id: "" });
      setShowLauncher(false);
      void refresh();
    } else {
      setLaunchError(result.error || "Failed to start run");
    }
    setLaunching(false);
  };

  const handleStop = async (id: string) => {
    if (!window.confirm("Stop this run?")) return;

    const result = await stopRun(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to stop run");
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Delete this run?")) return;

    const result = await deleteRun(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to delete run");
    }
  };

  const filteredTargets = selectedEventId
    ? targets
    : [];

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Runs</h1>
        <button onClick={() => setShowLauncher(!showLauncher)} className={styles.launchBtn}>
          🚀 Launch Run
        </button>
      </div>

      <div className={styles.eventSelector}>
        <label htmlFor="event-select">Filter by Event:</label>
        <select
          id="event-select"
          value={selectedEventId}
          onChange={(e) => {
            setSelectedEventId(e.target.value);
            setLaunchData({ ...launchData, event_id: e.target.value });
          }}
        >
          <option value="">All Events</option>
          {events.map((event) => (
            <option key={event.id} value={event.id}>
              {event.name}
            </option>
          ))}
        </select>
      </div>

      {showLauncher && (
        <form onSubmit={handleLaunch} className={styles.launcher}>
          <h3>Launch New Run</h3>

          <div className={styles.formGroup}>
            <label htmlFor="launch-event">Event *</label>
            <select
              id="launch-event"
              value={launchData.event_id}
              onChange={(e) => {
                setLaunchData({ ...launchData, event_id: e.target.value });
              }}
              disabled={launching}
              required
            >
              <option value="">Select an event</option>
              {events.map((event) => (
                <option key={event.id} value={event.id}>
                  {event.name}
                </option>
              ))}
            </select>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="launch-target">Target *</label>
            <select
              id="launch-target"
              value={launchData.target_id}
              onChange={(e) => setLaunchData({ ...launchData, target_id: e.target.value })}
              disabled={launching || !launchData.event_id}
              required
            >
              <option value="">Select a target</option>
              {filteredTargets.map((target) => (
                <option key={target.id} value={target.id}>
                  {target.name} {target.solved ? "✓" : ""}
                </option>
              ))}
            </select>
          </div>

          {launchError && <div className={styles.error}>{launchError}</div>}

          <div className={styles.actions}>
            <button type="submit" disabled={launching}>
              {launching ? "Launching..." : "Launch"}
            </button>
            <button type="button" onClick={() => setShowLauncher(false)} disabled={launching}>
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className={styles.filters}>
        {(["all", "running", "completed", "failed"] as const).map((f) => (
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

      {!loading && filteredRuns.length === 0 && (
        <div className={styles.empty}>No runs found</div>
      )}

      <div className={styles.table}>
        <div className={styles.tableHeader}>
          <div className={styles.col_id}>ID</div>
          <div className={styles.col_target}>Target</div>
          <div className={styles.col_status}>Status</div>
          <div className={styles.col_flags}>Flags</div>
          <div className={styles.col_time}>Created</div>
          <div className={styles.col_actions}>Actions</div>
        </div>

        {filteredRuns.map((run) => (
          <div key={run.id} className={styles.tableRow}>
            <div className={styles.col_id}>
              <code>{run.id.substring(0, 8)}</code>
            </div>
            <div className={styles.col_target}>
              <span>{run.target_id.substring(0, 8)}</span>
            </div>
            <div className={styles.col_status}>
              <span className={styles.status + " " + styles[`status_${run.status}`]}>
                {run.status}
              </span>
            </div>
            <div className={styles.col_flags}>
              <span className={run.solved ? styles.solved : ""}>
                {run.flags_found.length}
              </span>
            </div>
            <div className={styles.col_time}>
              {new Date(run.created_at * 1000).toLocaleDateString()}
            </div>
            <div className={styles.col_actions}>
              <Link href={`/run/${run.id}`} className={styles.viewBtn}>
                View
              </Link>
              {(run.status === "running" || run.status === "pending") && (
                <button onClick={() => handleStop(run.id)} className={styles.stopBtn}>
                  Stop
                </button>
              )}
              <button onClick={() => handleDelete(run.id)} className={styles.deleteBtn}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
