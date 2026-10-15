"use client";

import { useState } from "react";
import { useAudit, exportAuditLogs, type AuditFilters } from "@/lib/useAuditApi";
import styles from "./AuditLog.module.css";

export function AuditLog() {
  const [filters, setFilters] = useState<AuditFilters>({
    limit: 50,
    skip: 0,
  });
  const { logs, total, loading, error, refresh } = useAudit(filters);

  const [actionFilter, setActionFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"" | "success" | "failure">("");
  const [exporting, setExporting] = useState(false);

  const handleFilterApply = () => {
    const newFilters: AuditFilters = {
      limit: 50,
      skip: 0,
    };

    if (actionFilter) newFilters.action = actionFilter;
    if (statusFilter) newFilters.status_filter = statusFilter;

    setFilters(newFilters);
    void refresh(newFilters);
  };

  const handleClearFilters = () => {
    setActionFilter("");
    setStatusFilter("");
    setFilters({ limit: 50, skip: 0 });
    void refresh({ limit: 50, skip: 0 });
  };

  const handleExport = async () => {
    setExporting(true);
    const result = await exportAuditLogs(filters);
    if (result.ok && result.data) {
      const url = URL.createObjectURL(result.data);
      const link = document.createElement("a");
      link.href = url;
      link.download = `audit-logs-${new Date().toISOString().split("T")[0]}.csv`;
      link.click();
      URL.revokeObjectURL(url);
    } else {
      alert(result.error || "Failed to export logs");
    }
    setExporting(false);
  };

  const handlePreviousPage = () => {
    const next: AuditFilters = {
      ...filters,
      skip: Math.max(0, (filters.skip || 0) - (filters.limit || 50)),
    };
    setFilters(next);
    void refresh(next);
  };

  const handleNextPage = () => {
    const next: AuditFilters = {
      ...filters,
      skip: (filters.skip || 0) + (filters.limit || 50),
    };
    setFilters(next);
    void refresh(next);
  };

  const currentPage = Math.floor((filters.skip || 0) / (filters.limit || 50)) + 1;
  const totalPages = Math.max(1, Math.ceil(total / (filters.limit || 50)));

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Audit Log</h1>
        <button onClick={handleExport} disabled={exporting} className={styles.exportBtn}>
          📥 Export
        </button>
      </div>

      <div className={styles.filters}>
        <div className={styles.filterGroup}>
          <label htmlFor="action">Action</label>
          <select value={actionFilter} onChange={(e) => setActionFilter(e.target.value)}>
            <option value="">All</option>
            <option value="create">Create</option>
            <option value="update">Update</option>
            <option value="delete">Delete</option>
            <option value="login">Login</option>
          </select>
        </div>

        <div className={styles.filterGroup}>
          <label htmlFor="status">Status</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as "" | "success" | "failure")}
          >
            <option value="">All</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </select>
        </div>

        <div className={styles.filterActions}>
          <button onClick={handleFilterApply} className={styles.applyBtn}>
            Apply
          </button>
          <button onClick={handleClearFilters} className={styles.clearBtn}>
            Clear
          </button>
        </div>
      </div>

      {loading && <div className={styles.loading}>Loading audit logs...</div>}
      {error && <div className={styles.error}>Error: {error}</div>}

      {!loading && logs.length === 0 && <div className={styles.empty}>No audit logs found</div>}

      <div className={styles.table}>
        <div className={styles.tableHeader}>
          <div className={styles.col_time}>Timestamp</div>
          <div className={styles.col_action}>Action</div>
          <div className={styles.col_actor}>Actor</div>
          <div className={styles.col_resource}>Resource</div>
          <div className={styles.col_status}>Status</div>
          <div className={styles.col_ip}>IP Address</div>
        </div>

        {logs.map((log) => (
          <div key={log.id} className={styles.tableRow} title={JSON.stringify(log.details, null, 2)}>
            <div className={styles.col_time}>
              {new Date(log.timestamp * 1000).toLocaleString()}
            </div>
            <div className={styles.col_action}>
              <span className={styles.action + " " + styles[`action_${log.action}`]}>
                {log.action}
              </span>
            </div>
            <div className={styles.col_actor}>
              <code>{log.actor}</code>
            </div>
            <div className={styles.col_resource}>
              <span className={styles.resource}>
                {log.resource_type}
                <br />
                <code>{log.resource_id.substring(0, 12)}</code>
              </span>
            </div>
            <div className={styles.col_status}>
              <span className={styles.status + " " + styles[`status_${log.status}`]}>
                {log.status}
              </span>
            </div>
            <div className={styles.col_ip}>{log.ip_address}</div>
          </div>
        ))}
      </div>

      <div className={styles.pagination}>
        <button onClick={handlePreviousPage} disabled={currentPage === 1}>
          ← Previous
        </button>
        <span>
          Page {currentPage} of {totalPages} ({total} total)
        </span>
        <button onClick={handleNextPage} disabled={currentPage >= totalPages}>
          Next →
        </button>
      </div>
    </div>
  );
}
