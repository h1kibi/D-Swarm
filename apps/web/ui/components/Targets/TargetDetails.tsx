"use client";

import { useEffect, useState } from "react";
import { useTargets, updateTarget } from "@/lib/useTargetsApi";
import { useRuns } from "@/lib/useRunsApi";
import { useT } from "@/lib/i18n";
import Link from "next/link";

interface TargetDetailsProps {
  targetId: string;
}

export function TargetDetails({ targetId }: TargetDetailsProps) {
  const t = useT();
  const { targets, loading: targetsLoading } = useTargets();
  const target = targets.find((tgt) => tgt.id === targetId);
  const { runs } = useRuns();
  const targetRuns = runs.filter((run) => run.target_id === targetId);

  const [editing, setEditing] = useState(false);
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    category: "",
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
      setError(result.error || t("targets.editFailed"));
    }

    setUpdating(false);
  };

  if (targetsLoading) {
    return (
      <div className="ds-page">
        <div className="ds-loading">{t("common.loading")}</div>
      </div>
    );
  }

  if (!target) {
    return (
      <div className="ds-page">
        <div className="ds-empty">{t("targets.notFound")}</div>
      </div>
    );
  }

  const solvedRuns = targetRuns.filter((run) => run.solved).length;
  const totalFlags = target.flags.length;
  const totalFacts = target.facts_count;
  const flagCountFor = (run: { flag_count?: number; flags?: string[] }): number =>
    run.flag_count ?? run.flags?.length ?? 0;

  return (
    <div className="ds-page">
      <p style={{ margin: "0 0 18px" }}>
        <Link href="/targets" className="ds-link">{t("targets.back")}</Link>
      </p>

      <div className="ds-card ds-card--stack">
        {editing ? (
          <form onSubmit={handleSave}>
            <div className="ds-field-row">
              <div className="ds-field">
                <label htmlFor="target-name">{t("targets.name")}</label>
                <input
                  id="target-name"
                  type="text"
                  className="ds-input"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  disabled={updating}
                />
              </div>

              <div className="ds-field">
                <label htmlFor="target-category">{t("targets.category")}</label>
                <input
                  id="target-category"
                  type="text"
                  className="ds-input"
                  value={formData.category}
                  onChange={(e) => setFormData({ ...formData, category: e.target.value })}
                  disabled={updating}
                />
              </div>
            </div>

            <div className="ds-field">
              <label htmlFor="target-desc">{t("targets.desc")}</label>
              <textarea
                id="target-desc"
                className="ds-textarea"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                disabled={updating}
                rows={4}
              />
            </div>

            {error && <div className="ds-banner ds-banner--error">{error}</div>}
            {success && <div className="ds-banner ds-banner--ok">{t("targets.saved")}</div>}

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
              <h1 className="ds-title">{target.name}</h1>
              {target.description && (
                <p className="ds-sub" style={{ marginTop: 8 }}>{target.description}</p>
              )}
            </div>
            <div className="ds-actions">
              {target.category && <span className="ds-badge ds-badge--accent">{target.category}</span>}
              <span className={"ds-badge " + (target.is_solved ? "ds-badge--green" : "ds-badge--amber")}>
                {target.is_solved ? t("targets.solved") : t("targets.unsolved")}
              </span>
              <button type="button" className="ds-btn" onClick={() => setEditing(true)}>
                {t("common.edit")}
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="ds-grid">
        <section className="ds-card">
          <h2 className="ds-card-title">{t("targets.flagCount")}</h2>
          <div className="ds-stat">
            <div className="ds-stat-num">{totalFlags}</div>
            <span className="ds-stat-label">flags</span>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("targets.factCount")}</h2>
          <div className="ds-stat">
            <div className="ds-stat-num">{totalFacts}</div>
            <span className="ds-stat-label">facts</span>
          </div>
        </section>

        <section className="ds-card">
          <h2 className="ds-card-title">{t("targets.totalRuns")}</h2>
          <div className="ds-grid--stats" style={{ marginBottom: 0 }}>
            <div className="ds-stat">
              <div className="ds-stat-num">{targetRuns.length}</div>
              <span className="ds-stat-label">{t("events.total")}</span>
            </div>
            <div className="ds-stat">
              <div className="ds-stat-num">{solvedRuns}</div>
              <span className="ds-stat-label">{t("targets.solvedRuns")}</span>
            </div>
          </div>
          <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 14 }}>
            <Link href="/runs" className="ds-btn ds-btn--sm">{t("events.viewRuns")}</Link>
          </div>
        </section>
      </div>

      {targetRuns.length > 0 && (
        <section className="ds-card">
          <h2 className="ds-card-title">{t("targets.recentRuns")}</h2>
          <div className="ds-table" style={{ "--cols": "110px 1fr 120px 90px" } as React.CSSProperties}>
            {targetRuns.slice(0, 5).map((run) => (
              <Link
                key={run.run_id}
                href={`/run/${encodeURIComponent(run.run_id)}`}
                className="ds-row"
                style={{ textDecoration: "none", color: "inherit" }}
              >
                <span className="ds-code">{run.run_id.substring(0, 8)}</span>
                <span className="ds-muted">{run.name || "—"}</span>
                <span className="ds-dim" style={{ fontSize: 11.5 }}>
                  {run.updated_at ? new Date(run.updated_at * 1000).toLocaleDateString() : "—"}
                </span>
                <span className="ds-muted" style={{ fontSize: 11.5 }}>
                  {flagCountFor(run)} {t("targets.flagsWord")}
                </span>
              </Link>
            ))}
          </div>
          {targetRuns.length > 5 && (
            <div className="ds-actions" style={{ justifyContent: "flex-start", marginTop: 12 }}>
              <Link href="/runs" className="ds-link" style={{ fontSize: 12 }}>
                {t("targets.seeAllRuns", { n: targetRuns.length })}
              </Link>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
