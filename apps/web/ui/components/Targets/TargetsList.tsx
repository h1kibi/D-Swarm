"use client";

import { useState } from "react";
import { useTargets, createTarget, deleteTarget } from "@/lib/useTargetsApi";
import { useEvents } from "@/lib/useEventsApi";
import Link from "next/link";
import styles from "./TargetsList.module.css";

interface FormData {
  name: string;
  description: string;
  category: string;
  event_id: string;
}

export function TargetsList() {
  const { events } = useEvents();
  const [selectedEventId, setSelectedEventId] = useState<string>("");
  const { targets, loading, error, refresh } = useTargets(selectedEventId);

  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState<FormData>({
    name: "",
    description: "",
    category: "",
    event_id: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "solved" | "unsolved">("all");

  const filteredTargets =
    filter === "all"
      ? targets
      : filter === "solved"
        ? targets.filter((t) => t.is_solved)
        : targets.filter((t) => !t.is_solved);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.name.trim() || !formData.event_id) {
      setFormError("Target name and event are required");
      return;
    }

    setSubmitting(true);
    setFormError(null);

    const result = await createTarget({
      name: formData.name,
      description: formData.description,
      category: formData.category || "misc",
      event_id: formData.event_id,
    });

    if (result.ok) {
      setFormData({ name: "", description: "", category: "", event_id: "" });
      setShowForm(false);
      void refresh();
    } else {
      setFormError(result.error || "Failed to create target");
    }
    setSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Are you sure you want to delete this target?")) return;

    const result = await deleteTarget(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to delete target");
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Targets</h1>
        <button onClick={() => setShowForm(!showForm)} className={styles.createBtn}>
          + New Target
        </button>
      </div>

      <div className={styles.eventSelector}>
        <label htmlFor="event-select">Filter by Event:</label>
        <select
          id="event-select"
          value={selectedEventId}
          onChange={(e) => setSelectedEventId(e.target.value)}
        >
          <option value="">All Events</option>
          {events.map((event) => (
            <option key={event.id} value={event.id}>
              {event.name}
            </option>
          ))}
        </select>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className={styles.form}>
          <div className={styles.row}>
            <div className={styles.formGroup}>
              <label htmlFor="event">Event *</label>
              <select
                id="event"
                value={formData.event_id}
                onChange={(e) =>
                  setFormData({ ...formData, event_id: e.target.value })
                }
                disabled={submitting}
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
              <label htmlFor="category">Category</label>
              <input
                id="category"
                type="text"
                value={formData.category}
                onChange={(e) =>
                  setFormData({ ...formData, category: e.target.value })
                }
                placeholder="e.g., web, crypto, pwn"
                disabled={submitting}
              />
            </div>
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="name">Target Name *</label>
            <input
              id="name"
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder="e.g., SQL Injection Challenge"
              disabled={submitting}
              required
            />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="description">Description</label>
            <textarea
              id="description"
              value={formData.description}
              onChange={(e) =>
                setFormData({ ...formData, description: e.target.value })
              }
              placeholder="Target description..."
              rows={4}
              disabled={submitting}
            />
          </div>

          {formError && <div className={styles.error}>{formError}</div>}

          <div className={styles.formActions}>
            <button type="submit" disabled={submitting}>
              {submitting ? "Creating..." : "Create"}
            </button>
            <button type="button" onClick={() => setShowForm(false)} disabled={submitting}>
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className={styles.filters}>
        {(["all", "solved", "unsolved"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={filter === f ? styles.filterActive : styles.filter}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>

      {loading && <div className={styles.loading}>Loading targets...</div>}
      {error && <div className={styles.error}>Error: {error}</div>}

      {!loading && filteredTargets.length === 0 && (
        <div className={styles.empty}>No targets found</div>
      )}

      <div className={styles.list}>
        {filteredTargets.map((target) => (
          <div key={target.id} className={styles.card}>
            <div className={styles.cardHeader}>
              <h3>{target.name}</h3>
              <span className={styles.status + " " + styles[target.is_solved ? "solved" : "unsolved"]}>
                {target.is_solved ? "✓ Solved" : "Unsolved"}
              </span>
            </div>

            <p className={styles.description}>{target.description}</p>

            <div className={styles.meta}>
              {target.category && <span className={styles.category}>{target.category}</span>}
              <span>{target.flags.length} flags</span>
              <span>{target.facts_count} facts</span>
            </div>

            <div className={styles.actions}>
              <Link href={`/targets/${target.id}`} className={styles.link}>
                Edit
              </Link>
              <button onClick={() => handleDelete(target.id)} className={styles.delete}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
