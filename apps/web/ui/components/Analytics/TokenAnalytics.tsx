"use client";

import { useTokenAnalytics } from "@/lib/useTokenAnalytics";
import styles from "./TokenAnalytics.module.css";

export function TokenAnalytics() {
  const { stats, totalTokens, totalCost, loading, error } = useTokenAnalytics();

  if (loading) {
    return <div className={styles.loading}>Loading analytics...</div>;
  }

  if (error) {
    return <div className={styles.error}>Error: {error}</div>;
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Token Analytics</h1>
        <p>Monitor your token usage and costs</p>
      </div>

      <div className={styles.summary}>
        <div className={styles.summaryCard}>
          <div className={styles.label}>Total Tokens Used</div>
          <div className={styles.value}>{totalTokens.toLocaleString()}</div>
        </div>
        <div className={styles.summaryCard}>
          <div className={styles.label}>Estimated Cost</div>
          <div className={styles.value}>${totalCost.toFixed(2)}</div>
        </div>
        <div className={styles.summaryCard}>
          <div className={styles.label}>Providers</div>
          <div className={styles.value}>{stats.length}</div>
        </div>
        <div className={styles.summaryCard}>
          <div className={styles.label}>Avg per Run</div>
          <div className={styles.value}>
            {stats.length > 0
              ? Math.round(
                  stats.reduce((sum, s) => sum + s.avg_per_run, 0) / stats.length
                )
              : 0}
          </div>
        </div>
      </div>

      {stats.length === 0 ? (
        <div className={styles.empty}>No token usage data available</div>
      ) : (
        <div className={styles.table}>
          <div className={styles.tableHeader}>
            <div className={styles.col_provider}>Provider</div>
            <div className={styles.col_tokens}>Tokens Used</div>
            <div className={styles.col_runs}>Runs</div>
            <div className={styles.col_avg}>Avg per Run</div>
            <div className={styles.col_cost}>Estimated Cost</div>
            <div className={styles.col_trend}>Trend</div>
          </div>

          {stats.map((stat) => (
            <div key={stat.provider} className={styles.tableRow}>
              <div className={styles.col_provider}>
                <strong>{stat.provider}</strong>
              </div>
              <div className={styles.col_tokens}>{stat.total_tokens.toLocaleString()}</div>
              <div className={styles.col_runs}>{stat.runs_count}</div>
              <div className={styles.col_avg}>{Math.round(stat.avg_per_run)}</div>
              <div className={styles.col_cost}>${stat.total_cost.toFixed(4)}</div>
              <div className={styles.col_trend}>
                <span
                  className={
                    stat.trend > 0
                      ? styles.trendUp
                      : stat.trend < 0
                        ? styles.trendDown
                        : styles.trendNeutral
                  }
                >
                  {stat.trend > 0 ? "↑" : stat.trend < 0 ? "↓" : "→"}
                  {Math.abs(stat.trend).toFixed(1)}%
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
