"use client";

import { useState } from "react";
import { useTargets, createTarget, deleteTarget } from "@/lib/useTargetsApi";
import { useEvents } from "@/lib/useEventsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

interface FormData {
  name: string;
  description: string;
  category: string;
  event_id: string;
}

export function TargetsList() {
  const t = useT();
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
        ? targets.filter((tgt) => tgt.is_solved)
        : targets.filter((tgt) => !tgt.is_solved);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.name.trim() || !formData.event_id) {
      setFormError(t("targets.nameRequired"));
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
      setFormError(result.error || t("targets.createFailed"));
    }
    setSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm(t("targets.deleteConfirm"))) return;

    const result = await deleteTarget(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || t("targets.deleteFailed"));
    }
  };

  return (
    <div className="ds-page">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("targets.title")}</h1>
        </div>
        <div className="ds-actions">
          <button type="button" onClick={() => setShowForm(!showForm)} className="ds-btn">
            {t("targets.new")}
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="ds-card ds-card--stack">
          <div className="ds-field-row">
            <div className="ds-field">
              <label htmlFor="target-event">{t("targets.event")} *</label>
              <select
                id="target-event"
                className="ds-select"
                value={formData.event_id}
                onChange={(e) => setFormData({ ...formData, event_id: e.target.value })}
                disabled={submitting}
              >
                <option value="">{t("targets.selectEvent")}</option>
                {events.map((event) => (
                  <option key={event.id} value={event.id}>
                    {event.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="ds-field">
              <label htmlFor="target-category">{t("targets.category")}</label>
              <input
                id="target-category"
                type="text"
                className="ds-input"
                value={formData.category}
                onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                placeholder={t("targets.categoryPh")}
                disabled={submitting}
              />
            </div>
          </div>

          <div className="ds-field">
            <label htmlFor="target-name">{t("targets.name")} *</label>
            <input
              id="target-name"
              type="text"
              className="ds-input"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              placeholder={t("targets.namePh")}
              disabled={submitting}
            />
          </div>

          <div className="ds-field">
            <label htmlFor="target-desc">{t("targets.desc")}</label>
            <textarea
              id="target-desc"
              className="ds-textarea"
              value={formData.description}
              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
              disabled={submitting}
              rows={3}
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
        <label className="ds-label" htmlFor="event-filter" style={{ marginRight: 4 }}>
          {t("targets.filterEvent")}
        </label>
        <select
          id="event-filter"
          className="ds-select"
          style={{ width: "auto", minWidth: 180 }}
          value={selectedEventId}
          onChange={(e) => setSelectedEventId(e.target.value)}
        >
          <option value="">{t("targets.allEvents")}</option>
          {events.map((event) => (
            <option key={event.id} value={event.id}>
              {event.name}
            </option>
          ))}
        </select>

        <span style={{ width: 10 }} />

        {(["all", "solved", "unsolved"] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setFilter(option)}
            className={"ds-chip" + (filter === option ? " ds-chip--on" : "")}
          >
            {option === "all"
              ? t("common.all")
              : option === "solved"
                ? t("targets.solved")
                : t("targets.unsolved")}
          </button>
        ))}
      </div>

      {loading && <div className="ds-loading">{t("common.loading")}</div>}
      {error && <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>}

      {!loading && filteredTargets.length === 0 && (
        <div className="ds-empty">{t("targets.empty")}</div>
      )}

      <div className="ds-grid">
        {filteredTargets.map((target) => (
          <article key={target.id} className="ds-card">
            <div className="ds-card-head">
              <h3 className="ds-card-title">{target.name}</h3>
              <span className={"ds-badge " + (target.is_solved ? "ds-badge--green" : "ds-badge--amber")}>
                {target.is_solved ? t("targets.solved") : t("targets.unsolved")}
              </span>
            </div>

            {target.description && (
              <p className="ds-muted" style={{ margin: "0 0 12px", fontSize: 12.5 }}>
                {target.description}
              </p>
            )}

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
              {target.category && <span className="ds-badge ds-badge--accent">{target.category}</span>}
              <span className="ds-badge ds-badge--muted">
                {t("targets.flagsN", { n: target.flags.length })}
              </span>
              <span className="ds-badge ds-badge--muted">
                {t("targets.factsN", { n: target.facts_count })}
              </span>
            </div>

            <div className="ds-actions" style={{ justifyContent: "space-between" }}>
              <Link href={`/targets/${target.id}`} className="ds-btn ds-btn--sm">
                {t("common.edit")}
              </Link>
              <button
                type="button"
                onClick={() => handleDelete(target.id)}
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
