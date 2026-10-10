"use client";

import { useState } from "react";
import { useUser, changePassword, logout } from "@/lib/useUser";
import styles from "./Settings.module.css";

export function Settings() {
  const { user } = useUser();
  const [activeTab, setActiveTab] = useState<"password" | "api">("password");

  // Password form state
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
      setPasswordError("New passwords do not match");
      return;
    }

    if (passwordForm.newPassword.length < 8) {
      setPasswordError("Password must be at least 8 characters");
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
      setPasswordError(result.error || "Failed to change password");
    }

    setPasswordLoading(false);
  };

  const handleLogout = async () => {
    if (!window.confirm("Are you sure you want to log out?")) return;
    await logout();
    window.location.href = "/";
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Settings</h1>
      </div>

      {user && (
        <div className={styles.userInfo}>
          <div className={styles.infoCard}>
            <label>Username</label>
            <div className={styles.value}>{user.username}</div>
          </div>
          <div className={styles.infoCard}>
            <label>Display Name</label>
            <div className={styles.value}>{user.display_name}</div>
          </div>
          <div className={styles.infoCard}>
            <label>Role</label>
            <div className={styles.value}>
              <span className={styles.badge + " " + styles[user.role]}>
                {user.role === "admin" ? "Administrator" : "Member"}
              </span>
            </div>
          </div>
        </div>
      )}

      <div className={styles.tabs}>
        <button
          onClick={() => setActiveTab("password")}
          className={activeTab === "password" ? styles.active : ""}
        >
          Password
        </button>
        <button onClick={() => setActiveTab("api")} className={activeTab === "api" ? styles.active : ""}>
          API Keys
        </button>
      </div>

      {activeTab === "password" && (
        <div className={styles.section}>
          <h2>Change Password</h2>

          <form onSubmit={handlePasswordChange} className={styles.form}>
            <div className={styles.formGroup}>
              <label htmlFor="old-password">Current Password</label>
              <input
                id="old-password"
                type="password"
                value={passwordForm.oldPassword}
                onChange={(e) =>
                  setPasswordForm({ ...passwordForm, oldPassword: e.target.value })
                }
                disabled={passwordLoading}
                required
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="new-password">New Password</label>
              <input
                id="new-password"
                type="password"
                value={passwordForm.newPassword}
                onChange={(e) =>
                  setPasswordForm({ ...passwordForm, newPassword: e.target.value })
                }
                disabled={passwordLoading}
                required
              />
              <small>At least 8 characters</small>
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="confirm-password">Confirm New Password</label>
              <input
                id="confirm-password"
                type="password"
                value={passwordForm.confirmPassword}
                onChange={(e) =>
                  setPasswordForm({ ...passwordForm, confirmPassword: e.target.value })
                }
                disabled={passwordLoading}
                required
              />
            </div>

            {passwordError && <div className={styles.error}>{passwordError}</div>}
            {passwordSuccess && (
              <div className={styles.success}>Password changed successfully!</div>
            )}

            <button type="submit" disabled={passwordLoading} className={styles.submitBtn}>
              {passwordLoading ? "Changing..." : "Change Password"}
            </button>
          </form>
        </div>
      )}

      {activeTab === "api" && (
        <div className={styles.section}>
          <h2>API Keys</h2>
          <p className={styles.info}>
            API keys allow you to access D-Swarm programmatically. Keep them secret!
          </p>
          <p className={styles.notImplemented}>API key management coming soon...</p>
        </div>
      )}

      <div className={styles.section + " " + styles.danger}>
        <h2>Logout</h2>
        <p>End your current session and return to the login page.</p>
        <button onClick={handleLogout} className={styles.logoutBtn}>
          Logout
        </button>
      </div>
    </div>
  );
}
