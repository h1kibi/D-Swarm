"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "member";
  created_at: number;
  auth_required: boolean;
}

export interface UserState {
  user: User | null;
  loading: boolean;
  error: string | null;
  isAdmin: boolean;
  isMember: boolean;
}

let cachedUser: User | null = null;
let userListeners: Set<(user: User | null) => void> | undefined;

function getListeners(): Set<(user: User | null) => void> {
  if (!userListeners) userListeners = new Set();
  return userListeners;
}

function notifyUserChange(user: User | null): void {
  cachedUser = user;
  getListeners().forEach((listener) => {
    try {
      listener(user);
    } catch (e) {
      /* ignore listener errors */
    }
  });
}

export async function fetchCurrentUser(): Promise<User | null> {
  try {
    const res = await apiFetch("/api/auth/me");
    if (!res.ok) return null;
    const body = (await res.json()) as unknown;
    if (body && typeof body === "object" && "user" in body) {
      const candidate = (body as Record<string, unknown>).user;
      return typeof candidate === "object" && candidate !== null
        ? (candidate as unknown as User)
        : null;
    }
    return null;
  } catch {
    return null;
  }
}

export function useUser(): UserState {
  const [user, setUser] = useState<User | null>(cachedUser);
  const [loading, setLoading] = useState(!cachedUser);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (cachedUser) {
      setUser(cachedUser);
      setLoading(false);
      return;
    }

    const run = async (): Promise<void> => {
      try {
        const result = await fetchCurrentUser();
        setUser(result);
        if (result) notifyUserChange(result);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to fetch user");
        setUser(null);
      } finally {
        setLoading(false);
      }
    };

    void run();
    return subscribeToUserChanges(setUser);
  }, []);

  return {
    user,
    loading,
    error,
    isAdmin: (user?.role === "admin") || false,
    isMember: (user?.role === "member") || false,
  };
}

export function subscribeToUserChanges(callback: (user: User | null) => void): () => void {
  const listeners = getListeners();
  listeners.add(callback);
  return () => {
    listeners.delete(callback);
  };
}

export async function logout(): Promise<void> {
  try {
    await apiFetch("/api/auth/logout", { method: "POST" });
  } catch {
    /* ignore errors */
  } finally {
    notifyUserChange(null);
  }
}

export async function changePassword(
  oldPassword: string,
  newPassword: string
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch("/api/auth/change-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to change password";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}
