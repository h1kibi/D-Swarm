"use client";

import { useState } from "react";
import { useAccounts, createAccount, updateAccount, toggleAccountActive, deleteAccount } from "@/lib/useAccountsApi";
import styles from "./AccountsList.module.css";

interface FormData {
  username: string;
  display_name: string;
  password: string;
  role: "admin" | "member";
}

export function AccountsList() {
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
        ? accounts.filter((a) => a.is_active)
        : accounts.filter((a) => !a.is_active);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.username.trim() || !formData.password.trim()) {
      setFormError("Username and password are required");
      return;
    }

    if (formData.password.length < 8) {
      setFormError("Password must be at least 8 characters");
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
      setFormError(result.error || "Failed to create account");
    }

    setSubmitting(false);
  };

  const handleToggle = async (id: string, isActive: boolean) => {
    const result = await toggleAccountActive(id, !isActive);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to toggle account");
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("Are you sure you want to delete this account? This cannot be undone."))
      return;

    const result = await deleteAccount(id);
    if (result.ok) {
      void refresh();
    } else {
      alert(result.error || "Failed to delete account");
    }
  };

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h1>Account Management</h1>
        <button onClick={() => setShowForm(!showForm)} className={styles.createBtn}>
          + New Account
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className={styles.form}>
          <div className={styles.row}>
            <div className={styles.formGroup}>
              <label htmlFor="username">Username *</label>
              <input
                id="username"
                type="text"
                value={formData.username}
                onChange={(e) => setFormData({ ...formData, username: e.target.value })}
                placeholder="e.g., john_doe"
                disabled={submitting}
                required
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="password">Password *</label>
              <input
                id="password"
                type="password"
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
                placeholder="Min 8 characters"
                disabled={submitting}
                required
              />
            </div>
          </div>

          <div className={styles.row}>
            <div className={styles.formGroup}>
              <label htmlFor="display-name">Display Name</label>
              <input
                id="display-name"
                type="text"
                value={formData.display_name}
                onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                placeholder="e.g., John Doe"
                disabled={submitting}
              />
            </div>

            <div className={styles.formGroup}>
              <label htmlFor="role">Role</label>
              <select
                id="role"
                value={formData.role}
                onChange={(e) => setFormData({ ...formData, role: e.target.value as "admin" | "member" })}
                disabled={submitting}
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </div>
          </div>

          {formError && <div className={styles.error}>{formError}</div>}

          <div className={styles.formActions}>
            <button type="submit" disabled={submitting}>
              {submitting ? "Creating..." : "Create Account"}
            </button>
            <button type="button" onClick={() => setShowForm(false)} disabled={submitting}>
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className={styles.filters}>
        {(["all", "active", "inactive"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={filter === f ? styles.filterActive : styles.filter}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>

      {loading && <div className={styles.loading}>Loading accounts...</div>}
      {error && <div className={styles.error}>Error: {error}</div>}

      {!loading && filteredAccounts.length === 0 && <div className={styles.empty}>No accounts found</div>}

      <div className={styles.table}>
        <div className={styles.tableHeader}>
          <div className={styles.col_username}>Username</div>
          <div className={styles.col_name}>Display Name</div>
          <div className={styles.col_role}>Role</div>
          <div className={styles.col_status}>Status</div>
          <div className={styles.col_login}>Last Login</div>
          <div className={styles.col_actions}>Actions</div>
        </div>

        {filteredAccounts.map((account) => (
          <div key={account.id} className={styles.tableRow}>
            <div className={styles.col_username}>
              <code>{account.username}</code>
            </div>
            <div className={styles.col_name}>{account.display_name}</div>
            <div className={styles.col_role}>
              <span className={styles.badge + " " + styles[account.role]}>
                {account.role === "admin" ? "Admin" : "Member"}
              </span>
            </div>
            <div className={styles.col_status}>
              <span className={styles.status + " " + styles[account.is_active ? "active" : "inactive"]}>
                {account.is_active ? "Active" : "Inactive"}
              </span>
            </div>
            <div className={styles.col_login}>
              {account.last_login
                ? new Date(account.last_login * 1000).toLocaleDateString()
                : "Never"}
            </div>
            <div className={styles.col_actions}>
              <button
                onClick={() => handleToggle(account.id, account.is_active)}
                className={account.is_active ? styles.disableBtn : styles.enableBtn}
              >
                {account.is_active ? "Disable" : "Enable"}
              </button>
              <button onClick={() => handleDelete(account.id)} className={styles.deleteBtn}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
