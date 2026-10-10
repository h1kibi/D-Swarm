"use client";

import { useState } from "react";
import { useEvents, createEvent, updateEvent, deleteEvent } from "@/lib/useEventsApi";
import Link from "next/link";
import styles from "./EventsList.module.css";

interface FormData {
  name: string;
  description: string;
}

export function EventsList() {
  const { events, loading, error, refresh } = useEvents();
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState<FormData>({ name: "", description: "" });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "draft" | "archived">("all");

  const filteredEvents =
    filter === "all" ? events : events.filter((e) => e.status === filter);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      setFormError("Event name is required");
      return;
    }

    setSubmitting(true);
    setFormError(null);

    const result = await createEvent({
      name: formData.name,
      description: formData.description,
    });

    if (result.ok) {
      setFormData({ name: "", description: "" });
      setShowForm(false);
      void refresh();
    } else {
      setFormError(result.error || "Failed to create event");
    }
    setSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Are you sure you want to delete this event?")) return;

    const result = await deleteEvent(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to delete event");
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Events</h1>
        <button onClick={() => setShowForm(!showForm)} className={styles.createBtn}>
          + New Event
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className={styles.form}>
          <div className={styles.formGroup}>
            <label htmlFor="name">Event Name</label>
            <input
              id="name"
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder="e.g., CTF 2026"
              disabled={submitting}
            />
          </div>

          <div className={styles.formGroup}>
            <label htmlFor="description">Description</label>
            <textarea
              id="description"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              placeholder="Event description..."
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
        {(["all", "active", "draft", "archived"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={filter === f ? styles.filterActive : styles.filter}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>

      {loading && <div className={styles.loading}>Loading events...</div>}
      {error && <div className={styles.error}>Error: {error}</div>}

      {!loading && filteredEvents.length === 0 && (
        <div className={styles.empty}>No events found</div>
      )}

      <div className={styles.list}>
        {filteredEvents.map((event) => (
          <div key={event.id} className={styles.card}>
            <div className={styles.cardHeader}>
              <h3>{event.name}</h3>
              <span className={styles.status + " " + styles[`status_${event.status}`]}>
                {event.status}
              </span>
            </div>

            <p className={styles.description}>{event.description}</p>

            <div className={styles.meta}>
              <span>{event.target_count} targets</span>
              <span>by {event.created_by}</span>
            </div>

            <div className={styles.actions}>
              <Link href={`/events/${event.id}`} className={styles.link}>
                View
              </Link>
              <button onClick={() => handleDelete(event.id)} className={styles.delete}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
