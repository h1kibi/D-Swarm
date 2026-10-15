"use client";

import { useTokenAnalytics } from "@/lib/useTokenAnalytics";
import { useT } from "@/lib/i18n";

export function TokenAnalytics() {
  const t = useT();
  const { stats, totalTokens, totalCost, loading, error } = useTokenAnalytics();

  if (loading) {
    return (
      <div className="ds-page">
        <div className="ds-loading">{t("common.loading")}</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="ds-page">
        <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>
      </div>
    );
  }

  const avgPerRun =
    stats.length > 0
      ? Math.round(stats.reduce((sum, stat) => sum + stat.avg_per_run, 0) / stats.length)
      : 0;

  const trendClass = (trend: number): string => {
    if (trend > 0) return "ds-badge--red";
    if (trend < 0) return "ds-badge--green";
    return "ds-badge--muted";
  };

  const trendArrow = (trend: number): string => (trend > 0 ? "↑" : trend < 0 ? "↓" : "→");

  return (
    <div className="ds-page">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("analytics.title")}</h1>
          <p className="ds-sub">{t("analytics.sub")}</p>
        </div>
      </div>

      <div className="ds-grid--stats">
        <div className="ds-stat">
          <div className="ds-stat-num">{totalTokens.toLocaleString()}</div>
          <span className="ds-stat-label">{t("analytics.total")}</span>
        </div>
        <div className="ds-stat">
          <div className="ds-stat-num">${totalCost.toFixed(2)}</div>
          <span className="ds-stat-label">{t("analytics.cost")}</span>
        </div>
        <div className="ds-stat">
          <div className="ds-stat-num">{stats.length}</div>
          <span className="ds-stat-label">{t("analytics.providers")}</span>
        </div>
        <div className="ds-stat">
          <div className="ds-stat-num">{avgPerRun.toLocaleString()}</div>
          <span className="ds-stat-label">{t("analytics.avgAll")}</span>
        </div>
      </div>

      {stats.length === 0 ? (
        <div className="ds-empty">{t("analytics.empty")}</div>
      ) : (
        <div
          className="ds-table"
          style={{ "--cols": "1fr 140px 80px 110px 120px 100px" } as React.CSSProperties}
        >
          <div className="ds-table-head">
            <span>{t("analytics.colProvider")}</span>
            <span className="ds-cell-right">{t("analytics.colUsed")}</span>
            <span className="ds-cell-right">{t("analytics.colRuns")}</span>
            <span className="ds-cell-right">{t("analytics.colAvg")}</span>
            <span className="ds-cell-right">{t("analytics.colCost")}</span>
            <span className="ds-cell-center">{t("analytics.colTrend")}</span>
          </div>

          {stats.map((stat) => (
            <div key={stat.provider} className="ds-row">
              <span className="ds-bright">{stat.provider}</span>
              <span className="ds-cell-right ds-muted">{stat.total_tokens.toLocaleString()}</span>
              <span className="ds-cell-right ds-muted">{stat.runs_count}</span>
              <span className="ds-cell-right ds-muted">{Math.round(stat.avg_per_run).toLocaleString()}</span>
              <span className="ds-cell-right ds-muted">${stat.total_cost.toFixed(4)}</span>
              <span className="ds-cell-center">
                <span className={"ds-badge " + trendClass(stat.trend)}>
                  {trendArrow(stat.trend)}{Math.abs(stat.trend).toFixed(1)}%
                </span>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
