"use client";

import { useState } from "react";
import { useAccounts, createAccount, toggleAccountActive, deleteAccount } from "@/lib/useAccountsApi";
import { useT } from "@/lib/i18n";

interface FormData {
  username: string;
  display_name: string;
  password: string;
  role: "admin" | "member";
}

export function AccountsList() {
  const t = useT();
  const { accounts, loading, error, refresh } = useAccounts();

  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState<FormData>({
    username: "",
    display_name: "",
    password: "",
    role: "member",
  });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "inactive">("all");

  const filteredAccounts =
    filter === "all"
      ? accounts
      : filter === "active"
        ? accounts.filter((account) => account.is_active)
        : accounts.filter((account) => !account.is_active);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.username.trim() || !formData.password.trim()) {
      setFormError(t("acct.usernamePwRequired"));
      return;
    }

    if (formData.password.length < 8) {
      setFormError(t("settings.pwShort"));
      return;
    }

    setSubmitting(true);
    setFormError(null);

    const result = await createAccount(formData);

    if (result.ok) {
      setFormData({ username: "", display_name: "", password: "", role: "member" });
      setShowForm(false);
      void refresh();
    } else {
      setFormError(result.error || t("acct.createFailed"));
    }

    setSubmitting(false);
  };

  const handleToggle = async (id: string, isActive: boolean) => {
    const result = await toggleAccountActive(id, !isActive);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || t("acct.toggleFailed"));
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm(t("acct.deleteConfirm"))) return;

    const result = await deleteAccount(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || t("acct.deleteFailed"));
    }
  };

  return (
    <div className="ds-page ds-page--wide">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("acct.title")}</h1>
        </div>
        <div className="ds-actions">
          <button type="button" onClick={() => setShowForm(!showForm)} className="ds-btn">
            {t("acct.new")}
          </button>
        </div>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="ds-card ds-card--stack">
          <div className="ds-field-row">
            <div className="ds-field">
              <label htmlFor="acct-username">{t("settings.username")} *</label>
              <input
                id="acct-username"
                type="text"
                className="ds-input"
                value={formData.username}
                onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                placeholder={t("acct.usernamePh")}
                disabled={submitting}
              />
            </div>

            <div className="ds-field">
              <label htmlFor="acct-password">{t("acct.passwordPh")} *</label>
              <input
                id="acct-password"
                type="password"
                className="ds-input"
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                placeholder={t("acct.passwordPh")}
                disabled={submitting}
              />
            </div>
          </div>

          <div className="ds-field-row">
            <div className="ds-field">
              <label htmlFor="acct-display">{t("settings.displayName")}</label>
              <input
                id="acct-display"
                type="text"
                className="ds-input"
                value={formData.display_name}
                onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                placeholder={t("acct.displayNamePh")}
                disabled={submitting}
              />
            </div>

            <div className="ds-field">
              <label htmlFor="acct-role">{t("settings.role")}</label>
              <select
                id="acct-role"
                className="ds-select"
                value={formData.role}
                onChange={(e) =>
                  setFormData({ ...formData, role: e.target.value as "admin" | "member" })
                }
                disabled={submitting}
              >
                <option value="member">{t("role.member")}</option>
                <option value="admin">{t("role.admin")}</option>
              </select>
            </div>
          </div>

          {formError && <div className="ds-banner ds-banner--error">{formError}</div>}

          <div className="ds-actions">
            <button type="submit" className="ds-btn ds-btn--primary" disabled={submitting}>
              {submitting ? t("common.creating") : t("acct.create")}
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
        {(["all", "active", "inactive"] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => setFilter(option)}
            className={"ds-chip" + (filter === option ? " ds-chip--on" : "")}
          >
            {option === "all"
              ? t("common.all")
              : option === "active"
                ? t("acct.active")
                : t("acct.inactive")}
          </button>
        ))}
      </div>

      {loading && <div className="ds-loading">{t("common.loading")}</div>}
      {error && <div className="ds-banner ds-banner--error">{t("common.error")}: {error}</div>}

      {!loading && filteredAccounts.length === 0 && (
        <div className="ds-empty">{t("acct.empty")}</div>
      )}

      {filteredAccounts.length > 0 && (
        <div
          className="ds-table"
          style={{ "--cols": "170px 1fr 110px 100px 130px 200px" } as React.CSSProperties}
        >
          <div className="ds-table-head">
            <span>{t("acct.colUsername")}</span>
            <span>{t("acct.colName")}</span>
            <span>{t("acct.colRole")}</span>
            <span>{t("acct.colStatus")}</span>
            <span className="ds-cell-right">{t("acct.colLogin")}</span>
            <span className="ds-cell-right">{t("acct.colActions")}</span>
          </div>

          {filteredAccounts.map((account) => (
            <div key={account.id} className="ds-row">
              <span className="ds-code">{account.username}</span>
              <span className="ds-muted">{account.display_name}</span>
              <span>
                <span className={"ds-badge " + (account.role === "admin" ? "ds-badge--red" : "ds-badge--accent")}>
                  {account.role === "admin" ? t("role.admin") : t("role.member")}
                </span>
              </span>
              <span>
                <span className={"ds-badge " + (account.is_active ? "ds-badge--green" : "ds-badge--muted")}>
                  {account.is_active ? t("acct.active") : t("acct.inactive")}
                </span>
              </span>
              <span className="ds-cell-right ds-dim" style={{ fontSize: 11.5 }}>
                {account.last_login
                  ? new Date(account.last_login * 1000).toLocaleDateString()
                  : t("acct.never")}
              </span>
              <span className="ds-row-actions">
                <button
                  type="button"
                  onClick={() => handleToggle(account.id, account.is_active)}
                  className="ds-btn ds-btn--sm"
                >
                  {account.is_active ? t("acct.disable") : t("acct.enable")}
                </button>
                <button
                  type="button"
                  onClick={() => handleDelete(account.id)}
                  className="ds-btn ds-btn--sm ds-btn--danger"
                >
                  {t("common.delete")}
                </button>
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
