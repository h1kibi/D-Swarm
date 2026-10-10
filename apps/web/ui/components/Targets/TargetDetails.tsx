"use client";

import { useCallback, useEffect, useState } from "react";
import { useTargets, updateTarget } from "@/lib/useTargetsApi";
import { useRuns } from "@/lib/useRunsApi";
import Link from "next/link";
import styles from "./TargetDetails.module.css";

interface TargetDetailsProps {
  targetId: string;
}

export function TargetDetails({ targetId }: TargetDetailsProps) {
  const { targets, loading: targetsLoading } = useTargets();
  const target = targets.find((t) => t.id === targetId);
  const { runs } = useRuns();
  const targetRuns = runs.filter((r) => r.target_id === targetId);

  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    category: "",
    solved: false,
  });
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (target) {
      setFormData({
        name: target.name,
        description: target.description,
        category: target.category,
        solved: target.solved,
      });
    }
  }, [target]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!target) return;

    setUpdating(true);
    setError(null);
    setSuccess(false);

    const result = await updateTarget(target.id, formData);

    if (result.ok) {
      setSuccess(true);
      setEditing(false);
      setTimeout(() => setSuccess(false), 3000);
    } else {
      setError(result.error || "Failed to update target");
    }

    setUpdating(false);
  };

  if (targetsLoading) {
    return <div className={styles.loading}>Loading target...</div>;
  }

  if (!target) {
    return <div className={styles.error}>Target not found</div>;
  }

  const solvedRuns = targetRuns.filter((r) => r.solved).length;
  const totalFlags = target.flag_count;
  const totalFacts = target.fact_count;

  return (
    <div className={styles.container}>
      <Link href="/targets" className={styles.backLink}>
        ← Back to Targets
      </Link>

      <div className={styles.header}>
        {editing ? (
          <form onSubmit={handleSave} className={styles.editForm}>
            <div className={styles.row}>
              <div className={styles.formGroup}>
                <label htmlFor="name">Target Name</label>
                <input
                  id="name"
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  disabled={updating}
                />
              </div>

              <div className={styles.formGroup}>
                <label htmlFor="category">Category</label>
                <input
                  id="category"
                  type="text"
                  value={formData.category}
                  onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                  disabled={updating}
                />
              </div>
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="description">Description</label>
              <textarea
                id="description"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                disabled={updating}
                rows={4}
              />
            </div>

            <div className={styles.checkbox}>
              <input
                id="solved"
                type="checkbox"
                checked={formData.solved}
                onChange={(e) => setFormData({ ...formData, solved: e.target.checked })}
                disabled={updating}
              />
              <label htmlFor="solved">Mark as solved</label>
            </div>

            {error && <div className={styles.error}>{error}</div>}
            {success && <div className={styles.success}>Target updated successfully!</div>}

            <div className={styles.formActions}>
              <button type="submit" disabled={updating}>
                {updating ? "Saving..." : "Save"}
              </button>
              <button type="button" onClick={() => setEditing(false)} disabled={updating}>
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <div>
            <h1>{target.name}</h1>
            <div className={styles.meta}>
              <span className={styles.category}>{target.category}</span>
              <span>
                {target.solved ? (
                  <span className={styles.solved}>✓ Solved</span>
                ) : (
                  <span className={styles.unsolved}>○ Unsolved</span>
                )}
              </span>
            </div>
            <p className={styles.description}>{target.description}</p>
            <button onClick={() => setEditing(true)} className={styles.editBtn}>
              Edit Target
            </button>
          </div>
        )}
      </div>

      <div className={styles.grid}>
        <div className={styles.card}>
          <h2>🚩 Flags</h2>
          <div className={styles.bigStat}>
            <div className={styles.statValue}>{totalFlags}</div>
            <div className={styles.statLabel}>Total Flags</div>
          </div>
        </div>

        <div className={styles.card}>
          <h2>💡 Facts</h2>
          <div className={styles.bigStat}>
            <div className={styles.statValue}>{totalFacts}</div>
            <div className={styles.statLabel}>Total Facts</div>
          </div>
        </div>

        <div className={styles.card}>
          <h2>⚙️ Runs</h2>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{targetRuns.length}</div>
              <div className={styles.statLabel}>Total</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{solvedRuns}</div>
              <div className={styles.statLabel}>Solved</div>
            </div>
          </div>
          <Link href="/runs" className={styles.link}>
            View Runs
          </Link>
        </div>
      </div>

      {targetRuns.length > 0 && (
        <div className={styles.section}>
          <h2>Recent Runs</h2>
          <div className={styles.runsList}>
            {targetRuns.slice(0, 5).map((run) => (
              <Link key={run.id} href={`/run/${run.id}`} className={styles.runItem}>
                <div className={styles.runStatus + " " + styles[`status_${run.status}`]}>
                  {run.status}
                </div>
                <div className={styles.runDate}>
                  {new Date(run.created_at * 1000).toLocaleDateString()}
                </div>
                <div className={styles.runFlags}>
                  {run.flags_found.length} flag{run.flags_found.length !== 1 ? "s" : ""}
                </div>
              </Link>
            ))}
          </div>
          {targetRuns.length > 5 && (
            <Link href="/runs" className={styles.seeMore}>
              See all {targetRuns.length} runs →
            </Link>
          )}
        </div>
      )}
    </div>
  );
}
