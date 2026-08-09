"use client";

/**
 * localStorage access for the deck, centralized so the D-Swarm rebrand key
 * migration lives in exactly one place.
 *
 * Migration policy (Phase 3): keys moved from `muteki.*` / `muteki_*` to
 * `dswarm.*` / `dswarm_*`. User data must not be lost, so READS prefer the
 * new key and fall back to the legacy one; WRITES only ever touch the new
 * key (the legacy key is left alone, except removeKey clears both).
 */

/** new key → legacy key. Reads fall back so existing users keep settings. */
const LEGACY_KEYS: Record<string, string> = {
  "dswarm.lang": "muteki.lang",
  "dswarm.theme": "muteki.theme",
  "dswarm.artifact.width": "muteki.artifact.width",
  "dswarm.runInspector.width": "muteki.runInspector.width",
  "dswarm.threadRail.width": "muteki.threadRail.width",
  "dswarm.webSearch": "muteki.webSearch",
  "dswarm.mode": "muteki.mode",
  "dswarm.collect": "muteki.collect",
  "dswarm.flagFormat": "muteki.flagFormat",
  "dswarm.flagWrapper": "muteki.flagWrapper",
  "dswarm.containerMode": "muteki.containerMode",
  "dswarm.evidence.newestFirst": "muteki.evidence.newestFirst",
  "dswarm.activity.compact": "muteki.activity.compact",
  "dswarm_auth_token": "muteki_auth_token",
};

/** Per-run blackboard layout keys are dynamic; map them by prefix. */
const BB_LAYOUT_PREFIX = "dswarm.bb.layout.v3.";
const BB_LAYOUT_LEGACY_PREFIX = "muteki.bb.layout.v3.";

function legacyKeyOf(key: string): string | null {
  if (key in LEGACY_KEYS) return LEGACY_KEYS[key];
  if (key.startsWith(BB_LAYOUT_PREFIX)) return BB_LAYOUT_LEGACY_PREFIX + key.slice(BB_LAYOUT_PREFIX.length);
  return null;
}

/** Read a preference: new key wins, legacy key is the fallback. Never throws. */
export function readKey(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    const v = window.localStorage.getItem(key);
    if (v != null) return v;
    const legacy = legacyKeyOf(key);
    return legacy ? window.localStorage.getItem(legacy) : null;
  } catch {
    return null; // storage disabled (private mode) — caller uses its default
  }
}

/** Write a preference: only the new key is ever written. Never throws. */
export function writeKey(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage disabled — the preference just won't persist */
  }
}

/** Clear a preference: removes BOTH the new and the legacy key. */
export function removeKey(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
    const legacy = legacyKeyOf(key);
    if (legacy) window.localStorage.removeItem(legacy);
  } catch {
    /* ignore */
  }
}
