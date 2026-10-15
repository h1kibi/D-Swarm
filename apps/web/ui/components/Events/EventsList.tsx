"use client";

import { useState } from "react";
import { useEvents, createEvent, deleteEvent } from "@/lib/useEventsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

interface FormData {
  name: string;
  description: string;
}

const STATUS_CLASS: Record<string, string> = {
  active: "ds-badge--green",
  draft: "ds-badge--amber",
  archived: "ds-badge--muted",
};

export function EventsList() {
  const t = useT();
  const { events, loading, error, refresh } = useEvents();
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState<FormData>({ name: "", description: "" });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "draft" | "archived">("all");

  const filteredEvents =
    filter === "all" ? events : events.filter((event) => event.status === filter);

  const statusLabel = (status: string): string => {
    if (status === "active") return t("events.statusActive");
    if (status === "draft") return t("events.statusDraft");
    if (status === "archived") return t("events.statusArchived");
    return status;
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      setFormError(t("events.nameRequired"));
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
      setFormError(result.error || t("events.createFailed"));
    }
    setSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm(t("events.deleteConfirm"))) return;

    const result = await deleteEvent(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || t("events.deleteFailed"));
    }
  };

  return (
    <div className="ds-page">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("events.title")}</h1>
        </div>
        <div className="ds-actions">
          <button type="button" onClick={() => setShowForm(!showForm)} className="ds-btn">
            {t("events.new")}
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="ds-card ds-card--stack">
          <div className="ds-field">
            <label htmlFor="event-name">{t("events.name")}</label>
            <input
              id="event-name"
              type="text"
              className="ds-input"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder={t("events.namePh")}
              disabled={submitting}
            />
          </div>

          <div className="ds-field">
            <label htmlFor="event-desc">{t("events.desc")}</label>
            <textarea
              id="event-desc"
              className="ds-textarea"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              placeholder={t("events.descPh")}
              rows={3}
              disabled={submitting}
            />
          </div>

          {formError && <div className="ds-banner ds-banner--error">{formError}</div>}

          <div className="ds-actions">
            <button type="submit" className="ds-btn ds-btn--primary" disabled={submitting}>
              {submitting ? t("common.creating") : t("common.create")}
            </button>
            <button
              type="button"
              className="ds-btn ds-btn--ghost"
              onClick={() => setShowForm(false)}
              disabled={submitting}
            >
              {t("common.cancel")}
            </button>
          </div>
        </form>
      )}

      <div className="ds-toolbar">
        {(["all", "active", "draft", "archived"] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setFilter(option)}
            className={"ds-chip" + (filter === option ? " ds-chip--on" : "")}
          >
            {option === "all" ? t("common.all") : statusLabel(option)}
          </button>
        ))}
      </div>

      {loading && <div className="ds-loading">{t("common.loading")}</div>}
      {error && <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>}

      {!loading && filteredEvents.length === 0 && (
        <div className="ds-empty">{t("events.empty")}</div>
      )}

      <div className="ds-grid">
        {filteredEvents.map((event) => (
          <article key={event.id} className="ds-card">
            <div className="ds-card-head">
              <h3 className="ds-card-title">{event.name}</h3>
              <span className={"ds-badge " + (STATUS_CLASS[event.status] || "ds-badge--muted")}>
                {statusLabel(event.status)}
              </span>
            </div>

            {event.description && (
              <p className="ds-muted" style={{ margin: "0 0 12px", fontSize: 12.5 }}>
                {event.description}
              </p>
            )}

            <p className="ds-dim" style={{ margin: "0 0 14px", fontSize: 11.5 }}>
              {t("events.by", { name: event.created_by })}
            </p>

            <div className="ds-actions" style={{ justifyContent: "space-between" }}>
              <Link href={`/events/${event.id}`} className="ds-btn ds-btn--sm">
                {t("common.view")}
              </Link>
              <button
                type="button"
                onClick={() => handleDelete(event.id)}
                className="ds-btn ds-btn--sm ds-btn--danger"
              >
                {t("common.delete")}
              </button>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
