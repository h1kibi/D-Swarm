"use client";

/**
 * localStorage access for the deck, centralized so every preference read/write
 * goes through exactly one place.
 *
 * Historical note: this module once carried a muteki.* → dswarm.* key
 * migration map; that shim was retired once the rebrand window closed.
 * Keys are plain `dswarm.*` / `dswarm_*` now.
 */

/** Read a preference (null when unset). Never throws (storage may be disabled). */
export function readKey(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // storage disabled (private mode) — caller uses its default
  }
}

/** Write a preference. Never throws (storage may be disabled). */
export function writeKey(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage disabled — the preference just won't persist */
  }
}

/** Clear a preference. Never throws (storage may be disabled). */
export function removeKey(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}
