"use client";

import { useCallback, useEffect, useState } from "react";
import { useUser, changePassword, logout } from "@/lib/useUser";
import { useT } from "@/lib/i18n";
import { createApiKey, fetchApiKeys, revokeApiKey, type ApiKeyInfo } from "@/lib/apiKeys";

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

  const [keys, setKeys] = useState<ApiKeyInfo[]>([]);
  const [keysLoading, setKeysLoading] = useState(true);
  const [keysError, setKeysError] = useState<string | null>(null);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [keyBusy, setKeyBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const loadKeys = useCallback(async () => {
    setKeysLoading(true);
    const next = await fetchApiKeys();
    setKeys(next);
    setKeysLoading(false);
  }, []);

  useEffect(() => {
    void loadKeys();
  }, [loadKeys]);

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

  const handleCreateKey = async () => {
    setKeyBusy(true);
    setKeysError(null);
    setCopied(false);
    const result = await createApiKey();
    if (result.ok && result.token) {
      setNewToken(result.token);
      await loadKeys();
    } else {
      setKeysError(result.error || t("settings.apiFailed"));
    }
    setKeyBusy(false);
  };

  const handleRevokeKey = async (keyId: string) => {
    if (!window.confirm(t("settings.apiRevokeConfirm"))) return;
    setKeysError(null);
    const ok = await revokeApiKey(keyId);
    if (ok) {
      setNewToken((current) => (current ? current : current));
      await loadKeys();
    } else {
      setKeysError(t("settings.apiFailed"));
    }
  };

  const handleCopyToken = async () => {
    if (!newToken) return;
    try {
      await navigator.clipboard.writeText(newToken);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setKeysError(t("settings.apiFailed"));
    }
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
        <div className="ds-card-head">
          <h2 className="ds-card-title">{t("settings.apiTitle")}</h2>
          <button
            type="button"
            className="ds-btn ds-btn--sm"
            onClick={handleCreateKey}
            disabled={keyBusy}
          >
            {t("settings.apiCreate")}
          </button>
        </div>

        <p className="ds-muted" style={{ margin: "0 0 12px", fontSize: 12.5 }}>
          {t("settings.apiHint")}
        </p>

        {newToken && (
          <div style={{ marginBottom: 14 }}>
            <div className="ds-banner ds-banner--ok" style={{ marginBottom: 8 }}>
              {t("settings.apiCreated")}
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
              <code
                className="ds-code"
                style={{ flex: 1, overflowWrap: "anywhere", padding: "8px 10px" }}
              >
                {newToken}
              </code>
              <button type="button" className="ds-btn ds-btn--sm" onClick={handleCopyToken}>
                {copied ? t("settings.apiCopied") : t("settings.apiCopy")}
              </button>
            </div>
          </div>
        )}

        {keysError && <div className="ds-banner ds-banner--error">{keysError}</div>}

        {keysLoading ? (
          <div className="ds-loading">{t("common.loading")}</div>
        ) : keys.length === 0 ? (
          <div className="ds-empty">{t("settings.apiEmpty")}</div>
        ) : (
          <div className="ds-table" style={{ "--cols": "1fr 110px 100px 90px" } as React.CSSProperties}>
            {keys.map((key) => (
              <div key={key.key_id} className="ds-row">
                <span className="ds-code">{key.key_id.substring(0, 12)}</span>
                <span className="ds-dim" style={{ fontSize: 11.5 }}>
                  {key.created_at ? new Date(key.created_at * 1000).toLocaleDateString() : "—"}
                </span>
                <span>
                  <span className={"ds-badge " + (key.is_active ? "ds-badge--green" : "ds-badge--muted")}>
                    {key.is_active ? t("settings.apiActive") : t("settings.apiRevoked")}
                  </span>
                </span>
                <span className="ds-row-actions">
                  {key.is_active && (
                    <button
                      type="button"
                      className="ds-btn ds-btn--sm ds-btn--danger"
                      onClick={() => handleRevokeKey(key.key_id)}
                    >
                      {t("settings.apiRevoke")}
                    </button>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
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
