"use client";

import { useState, useEffect } from "react";
import { useEvents, updateEvent } from "@/lib/useEventsApi";
import { useTargets } from "@/lib/useTargetsApi";
import { useRuns } from "@/lib/useRunsApi";
import Link from "next/link";
import styles from "./EventDetails.module.css";

interface EventDetailsProps {
  eventId: string;
}

export function EventDetails({ eventId }: EventDetailsProps) {
  const { events, loading: eventsLoading } = useEvents();
  const { targets } = useTargets(eventId);
  const { runs } = useRuns();

  const event = events.find((e) => e.id === eventId);

  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    description: "",
  });
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (event) {
      setFormData({
        name: event.name,
        description: event.description,
      });
    }
  }, [event]);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!event) return;

    setUpdating(true);
    setError(null);
    setSuccess(false);

    const result = await updateEvent(event.id, formData);

    if (result.ok) {
      setSuccess(true);
      setEditing(false);
      setTimeout(() => setSuccess(false), 3000);
    } else {
      setError(result.error || "Failed to update event");
    }

    setUpdating(false);
  };

  if (eventsLoading) {
    return <div className={styles.loading}>Loading event...</div>;
  }

  if (!event) {
    return <div className={styles.error}>Event not found</div>;
  }

  const eventTargetIds = new Set(targets.map((t) => t.id));
  const eventRuns = runs.filter((r) => r.target_id && eventTargetIds.has(r.target_id));
  const solvedTargets = targets.filter((t) => t.is_solved).length;
  const totalTargets = targets.length;
  const activeRuns = eventRuns.filter((r) => r.status === "running").length;
  const completedRuns = eventRuns.filter((r) => r.solved === true || r.finished === true).length;

  return (
    <div className={styles.container}>
      <Link href="/events" className={styles.backLink}>
        ← Back to Events
      </Link>

      <div className={styles.header}>
        {editing ? (
          <form onSubmit={handleSave} className={styles.editForm}>
            <div className={styles.formGroup}>
              <label htmlFor="name">Event Name</label>
              <input
                id="name"
                type="text"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                disabled={updating}
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="description">Description</label>
              <textarea
                id="description"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                disabled={updating}
                rows={3}
              />
            </div>

            {error && <div className={styles.error}>{error}</div>}
            {success && <div className={styles.success}>Event updated successfully!</div>}

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
            <h1>{event.name}</h1>
            <p className={styles.description}>{event.description}</p>
            <div className={styles.meta}>
              <span>Created by {event.created_by}</span>
              <span>Status: {event.status}</span>
            </div>
            <button onClick={() => setEditing(true)} className={styles.editBtn}>
              Edit Event
            </button>
          </div>
        )}
      </div>

      <div className={styles.grid}>
        <div className={styles.card}>
          <h2>📋 Targets</h2>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{totalTargets}</div>
              <div className={styles.statLabel}>Total</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{solvedTargets}</div>
              <div className={styles.statLabel}>Solved</div>
            </div>
          </div>
          <Link href="/targets" className={styles.link}>
            View Targets
          </Link>
        </div>

        <div className={styles.card}>
          <h2>⚙️ Runs</h2>
          <div className={styles.stats}>
            <div className={styles.stat}>
              <div className={styles.statValue}>{activeRuns}</div>
              <div className={styles.statLabel}>Active</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statValue}>{completedRuns}</div>
              <div className={styles.statLabel}>Completed</div>
            </div>
          </div>
          <Link href="/runs" className={styles.link}>
            View Runs
          </Link>
        </div>

        <div className={styles.card}>
          <h2>📊 Event Info</h2>
          <div className={styles.infoList}>
            <div className={styles.infoItem}>
              <span className={styles.label}>Status</span>
              <span className={styles.value}>{event.status}</span>
            </div>
            <div className={styles.infoItem}>
              <span className={styles.label}>Created</span>
              <span className={styles.value}>
                {new Date(event.created_at * 1000).toLocaleDateString()}
              </span>
            </div>
            <div className={styles.infoItem}>
              <span className={styles.label}>Created By</span>
              <span className={styles.value}>{event.created_by}</span>
            </div>
          </div>
        </div>
      </div>

      {targets.length > 0 && (
        <div className={styles.section}>
          <h2>Targets in this Event</h2>
          <div className={styles.targetList}>
            {targets.map((target) => (
              <div key={target.id} className={styles.targetItem}>
                <div className={styles.targetName}>{target.name}</div>
                <div className={styles.targetCategory}>{target.category}</div>
                <div className={styles.targetStatus}>
                  {target.is_solved ? "✓ Solved" : "Unsolved"}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
