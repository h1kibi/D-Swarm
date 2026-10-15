"use client";

import { useState, useEffect } from "react";
import { useEvents, updateEvent } from "@/lib/useEventsApi";
import { useTargets } from "@/lib/useTargetsApi";
import { useRuns } from "@/lib/useRunsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

interface EventDetailsProps {
  eventId: string;
}

export function EventDetails({ eventId }: EventDetailsProps) {
  const t = useT();
  const { events, loading: eventsLoading } = useEvents();
  const { targets } = useTargets(eventId);
  const { runs } = useRuns();

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

  const eventTargetIds = new Set(targets.map((tgt) => tgt.id));
  const eventRuns = runs.filter((run) => run.target_id && eventTargetIds.has(run.target_id));
  const solvedTargets = targets.filter((tgt) => tgt.is_solved).length;
  const activeRuns = eventRuns.filter((run) => run.status === "running").length;
  const completedRuns = eventRuns.filter(
    (run) => run.solved === true || run.finished === true,
  ).length;

  return (
    <div className="ds-page">
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
          <>
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
          </>
        )}
      </div>

      <div className="ds-grid">
        <section className="ds-card">
          <h2 className="ds-card-title">{t("events.targetsCount")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{targets.length}</div>
              <span className="ds-stat-label">{t("events.total")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{solvedTargets}</div>
              <span className="ds-stat-label">{t("events.solvedCount")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/targets" className="ds-btn ds-btn--sm">{t("events.viewTargets")}</Link>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("events.runsCount")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{activeRuns}</div>
              <span className="ds-stat-label">{t("events.runsActive")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{completedRuns}</div>
              <span className="ds-stat-label">{t("events.runsDone")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/runs" className="ds-btn ds-btn--sm">{t("events.viewRuns")}</Link>
          </div>
        </section>

        <section className="ds-card">
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
      </div>

      {targets.length > 0 && (
        <section className="ds-card">
          <h2 className="ds-card-title">{t("events.targetsIn")}</h2>
          <div className="ds-table" style={{ "--cols": "1fr 120px 130px" } as React.CSSProperties}>
            {targets.map((target) => (
              <div key={target.id} className="ds-row">
                <span className="ds-bright">{target.name}</span>
                <span className="ds-accent" style={{ fontSize: 11.5 }}>{target.category}</span>
                <span>
                  <span className={"ds-badge " + (target.is_solved ? "ds-badge--green" : "ds-badge--amber")}>
                    {target.is_solved ? t("events.solvedTag") : t("events.unsolvedTag")}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
