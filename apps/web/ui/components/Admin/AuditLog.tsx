"use client";

import { useState } from "react";
import { useAudit, exportAuditLogs, type AuditFilters } from "@/lib/useAuditApi";
import { useT } from "@/lib/i18n";

const ACTION_TONE: Record<string, string> = {
  create: "ds-badge--green",
  update: "ds-badge--accent",
  delete: "ds-badge--red",
  login: "ds-badge--amber",
};

export function AuditLog() {
  const t = useT();
  const [filters, setFilters] = useState<AuditFilters>({
    limit: 50,
    skip: 0,
  });
  const { logs, total, loading, error, refresh } = useAudit(filters);

  const [actionFilter, setActionFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<"" | "success" | "failure">("");
  const [exporting, setExporting] = useState(false);

  const handleFilterApply = () => {
    const newFilters: AuditFilters = { limit: 50, skip: 0 };
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
      alert(result.error || t("audit.exportFailed"));
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
    <div className="ds-page ds-page--wide">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("audit.title")}</h1>
        </div>
        <div className="ds-actions">
          <button type="button" onClick={handleExport} disabled={exporting} className="ds-btn">
            {t("audit.export")}
          </button>
        </div>
      </div>

      <div className="ds-toolbar">
        <label className="ds-label" htmlFor="audit-action" style={{ marginRight: 4 }}>
          {t("audit.filterAction")}
        </label>
        <select
          id="audit-action"
          className="ds-select"
          style={{ width: "auto", minWidth: 130 }}
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
        >
          <option value="">{t("common.all")}</option>
          <option value="create">create</option>
          <option value="update">update</option>
          <option value="delete">delete</option>
          <option value="login">login</option>
        </select>

        <span style={{ width: 8 }} />

        <label className="ds-label" htmlFor="audit-status" style={{ marginRight: 4 }}>
          {t("audit.filterStatus")}
        </label>
        <select
          id="audit-status"
          className="ds-select"
          style={{ width: "auto", minWidth: 120 }}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as "" | "success" | "failure")}
        >
          <option value="">{t("common.all")}</option>
          <option value="success">{t("audit.filterSuccess")}</option>
          <option value="failure">{t("audit.filterFailure")}</option>
        </select>

        <span style={{ flex: 1 }} />

        <button type="button" onClick={handleFilterApply} className="ds-btn ds-btn--sm">
          {t("common.apply")}
        </button>
        <button type="button" onClick={handleClearFilters} className="ds-btn ds-btn--sm ds-btn--ghost">
          {t("common.clear")}
        </button>
      </div>

      {loading && <div className="ds-loading">{t("common.loading")}</div>}
      {error && <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>}

      {!loading && logs.length === 0 && <div className="ds-empty">{t("audit.empty")}</div>}

      {logs.length > 0 && (
        <>
          <div
            className="ds-table"
            style={{ "--cols": "170px 110px 140px 1fr 100px 130px" } as React.CSSProperties}
          >
            <div className="ds-table-head">
              <span>{t("audit.colTime")}</span>
              <span>{t("audit.filterAction")}</span>
              <span>{t("audit.colActor")}</span>
              <span>{t("audit.colResource")}</span>
              <span>{t("audit.colStatus")}</span>
              <span>{t("audit.colIp")}</span>
            </div>

            {logs.map((log) => (
              <div key={log.id} className="ds-row" title={JSON.stringify(log.details, null, 2)}>
                <span className="ds-dim" style={{ fontSize: 11.5 }}>
                  {new Date(log.timestamp * 1000).toLocaleString()}
                </span>
                <span>
                  <span className={"ds-badge " + (ACTION_TONE[log.action] || "ds-badge--muted")}>
                    {log.action}
                  </span>
                </span>
                <span className="ds-code">{log.actor}</span>
                <span className="ds-muted" style={{ fontSize: 11.5, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {log.resource_type} · {log.resource_id.substring(0, 12)}
                </span>
                <span>
                  <span className={"ds-badge " + (log.status === "success" ? "ds-badge--green" : "ds-badge--red")}>
                    {log.status}
                  </span>
                </span>
                <span className="ds-dim" style={{ fontSize: 11.5 }}>{log.ip_address}</span>
              </div>
            ))}
          </div>

          <div className="ds-pager">
            <button
              type="button"
              onClick={handlePreviousPage}
              disabled={currentPage === 1}
              className="ds-btn ds-btn--sm ds-btn--ghost"
            >
              {t("audit.prev")}
            </button>
            <span>{t("audit.pager", { page: currentPage, pages: totalPages, total })}</span>
            <button
              type="button"
              onClick={handleNextPage}
              disabled={currentPage >= totalPages}
              className="ds-btn ds-btn--sm ds-btn--ghost"
            >
              {t("audit.next")}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
