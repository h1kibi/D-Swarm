"use client";

import { useState, useEffect } from "react";
import { useEvents, updateEvent } from "@/lib/useEventsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

interface EventDetailsProps {
  eventId: string;
}

/**
 * Events are console-only grouping metadata (the deck has no event concept):
 * name/description/status CRUD lives here. Targets and Runs are managed in the
 * Command Deck — this page only hands off.
 */
export function EventDetails({ eventId }: EventDetailsProps) {
  const t = useT();
  const { events, loading: eventsLoading } = useEvents();

  const event = events.find((e) => e.id === eventId);

  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState({ name: "", description: "" });
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    if (event) {
      setFormData({ name: event.name, description: event.description });
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
      setError(result.error || t("events.editFailed"));
    }

    setUpdating(false);
  };

  if (eventsLoading) {
    return (
      <div className="ds-page">
        <div className="ds-loading">{t("common.loading")}</div>
      </div>
    );
  }

  if (!event) {
    return (
      <div className="ds-page">
        <div className="ds-empty">{t("events.notFound")}</div>
      </div>
    );
  }

  return (
    <div className="ds-page ds-page--narrow">
      <p style={{ margin: "0 0 18px" }}>
        <Link href="/events" className="ds-link">{t("events.back")}</Link>
      </p>

      <div className="ds-card ds-card--stack">
        {editing ? (
          <form onSubmit={handleSave}>
            <div className="ds-field">
              <label htmlFor="event-name">{t("events.name")}</label>
              <input
                id="event-name"
                type="text"
                className="ds-input"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                disabled={updating}
              />
            </div>

            <div className="ds-field">
              <label htmlFor="event-desc">{t("events.desc")}</label>
              <textarea
                id="event-desc"
                className="ds-textarea"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                disabled={updating}
                rows={3}
              />
            </div>

            {error && <div className="ds-banner ds-banner--error">{error}</div>}
            {success && <div className="ds-banner ds-banner--ok">{t("events.saved")}</div>}

            <div className="ds-actions">
              <button type="submit" className="ds-btn ds-btn--primary" disabled={updating}>
                {updating ? t("common.saving") : t("common.save")}
              </button>
              <button
                type="button"
                className="ds-btn ds-btn--ghost"
                onClick={() => setEditing(false)}
                disabled={updating}
              >
                {t("common.cancel")}
              </button>
            </div>
          </form>
        ) : (
          <div className="ds-head" style={{ margin: 0 }}>
            <div>
              <h1 className="ds-title">{event.name}</h1>
              {event.description && (
                <p className="ds-sub" style={{ marginTop: 8 }}>{event.description}</p>
              )}
            </div>
            <div className="ds-actions">
              <span className={"ds-badge " + (event.status === "active" ? "ds-badge--green" : "ds-badge--muted")}>
                {event.status}
              </span>
              <button type="button" className="ds-btn" onClick={() => setEditing(true)}>
                {t("events.edit")}
              </button>
            </div>
          </div>
        )}
      </div>

      <section className="ds-card ds-card--stack">
        <h2 className="ds-card-title">{t("events.info")}</h2>
        <div className="ds-kv">
          <span className="ds-muted">{t("events.infoStatus")}</span>
          <span>{event.status}</span>
        </div>
        <div className="ds-kv">
          <span className="ds-muted">{t("events.infoCreated")}</span>
          <span>{new Date(event.created_at * 1000).toLocaleDateString()}</span>
        </div>
        <div className="ds-kv">
          <span className="ds-muted">{t("events.infoCreator")}</span>
          <span>{event.created_by}</span>
        </div>
      </section>

      <section className="ds-card">
        <h2 className="ds-card-title">{t("events.deckTitle")}</h2>
        <p className="ds-muted" style={{ margin: "0 0 14px", fontSize: 12.5 }}>
          {t("events.deckHint")}
        </p>
        <div className="ds-actions" style={{ justifyContent: "flex-start" }}>
          <Link href="/deck" className="ds-btn ds-btn--sm">{t("dash.openDeck")}</Link>
          <Link href="/runs" className="ds-btn ds-btn--sm ds-btn--ghost">{t("runs.title")}</Link>
        </div>
      </section>
    </div>
  );
}
