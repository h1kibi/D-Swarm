"use client";

import { useState } from "react";
import { useUser, changePassword, logout } from "@/lib/useUser";
import { useT } from "@/lib/i18n";

export function Settings() {
  const t = useT();
  const { user, isAdmin } = useUser();

  const [passwordForm, setPasswordForm] = useState({
    oldPassword: "",
    newPassword: "",
    confirmPassword: "",
  });
  const [passwordLoading, setPasswordLoading] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordSuccess, setPasswordSuccess] = useState(false);

  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault();

    if (passwordForm.newPassword !== passwordForm.confirmPassword) {
      setPasswordError(t("settings.pwMismatch"));
      return;
    }

    if (passwordForm.newPassword.length < 8) {
      setPasswordError(t("settings.pwShort"));
      return;
    }

    setPasswordLoading(true);
    setPasswordError(null);
    setPasswordSuccess(false);

    const result = await changePassword(passwordForm.oldPassword, passwordForm.newPassword);

    if (result.ok) {
      setPasswordSuccess(true);
      setPasswordForm({ oldPassword: "", newPassword: "", confirmPassword: "" });
      setTimeout(() => setPasswordSuccess(false), 3000);
    } else {
      setPasswordError(result.error || t("settings.pwFailed"));
    }

    setPasswordLoading(false);
  };

  const handleLogout = async () => {
    if (!window.confirm(t("settings.logoutConfirm"))) return;
    await logout();
    window.location.href = "/";
  };

  return (
    <div className="ds-page ds-page--narrow">
      <div className="ds-head">
        <div>
          <h1 className="ds-title">{t("settings.pageTitle")}</h1>
        </div>
      </div>

      {user && (
        <section className="ds-card ds-card--stack">
          <h2 className="ds-card-title">{t("settings.username")}</h2>
          <div className="ds-kv">
            <span className="ds-muted">{t("settings.username")}</span>
            <span className="ds-code">{user.username}</span>
          </div>
          <div className="ds-kv">
            <span className="ds-muted">{t("settings.displayName")}</span>
            <span>{user.display_name || "—"}</span>
          </div>
          <div className="ds-kv">
            <span className="ds-muted">{t("settings.role")}</span>
            <span className={"ds-badge " + (isAdmin ? "ds-badge--red" : "ds-badge--accent")}>
              {isAdmin ? t("role.admin") : t("role.member")}
            </span>
          </div>
        </section>
      )}

      <section className="ds-card ds-card--stack">
        <h2 className="ds-card-title">{t("settings.pwTitle")}</h2>

        <form onSubmit={handlePasswordChange}>
          <div className="ds-field">
            <label htmlFor="old-password">{t("settings.currentPw")}</label>
            <input
              id="old-password"
              type="password"
              className="ds-input"
              value={passwordForm.oldPassword}
              onChange={(e) => setPasswordForm({ ...passwordForm, oldPassword: e.target.value })}
              disabled={passwordLoading}
              required
            />
          </div>

          <div className="ds-field">
            <label htmlFor="new-password">{t("settings.newPw")}</label>
            <input
              id="new-password"
              type="password"
              className="ds-input"
              value={passwordForm.newPassword}
              onChange={(e) => setPasswordForm({ ...passwordForm, newPassword: e.target.value })}
              disabled={passwordLoading}
              required
            />
            <small>{t("settings.pwRule")}</small>
          </div>

          <div className="ds-field">
            <label htmlFor="confirm-password">{t("settings.confirmPw")}</label>
            <input
              id="confirm-password"
              type="password"
              className="ds-input"
              value={passwordForm.confirmPassword}
              onChange={(e) => setPasswordForm({ ...passwordForm, confirmPassword: e.target.value })}
              disabled={passwordLoading}
              required
            />
          </div>

          {passwordError && <div className="ds-banner ds-banner--error">{passwordError}</div>}
          {passwordSuccess && <div className="ds-banner ds-banner--ok">{t("settings.pwChanged")}</div>}

          <div className="ds-actions">
            <button type="submit" className="ds-btn ds-btn--primary" disabled={passwordLoading}>
              {passwordLoading ? t("settings.changing") : t("settings.changePw")}
            </button>
          </div>
        </form>
      </section>

      <section className="ds-card ds-card--stack">
        <h2 className="ds-card-title">{t("settings.apiTitle")}</h2>
        <p className="ds-muted" style={{ margin: "0 0 10px", fontSize: 12.5 }}>
          {t("settings.apiHint")}
        </p>
        <p className="ds-dim" style={{ margin: 0, fontSize: 12 }}>
          {t("settings.apiSoon")}
        </p>
      </section>

      <section className="ds-card">
        <h2 className="ds-card-title" style={{ color: "var(--red)" }}>
          {t("settings.logoutTitle")}
        </h2>
        <p className="ds-muted" style={{ margin: "0 0 14px", fontSize: 12.5 }}>
          {t("settings.logoutHint")}
        </p>
        <div className="ds-actions" style={{ justifyContent: "flex-start" }}>
          <button type="button" onClick={handleLogout} className="ds-btn ds-btn--danger">
            {t("nav.logout")}
          </button>
        </div>
      </section>
    </div>
  );
}
