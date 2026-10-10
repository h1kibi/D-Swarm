import type { RunBindingRef, RunBindingSnapshot } from "./useRun";

export type BindingRole = "reason" | "worker" | "review" | "observer" | "titler";

export function bindingText(ref: RunBindingRef | undefined, ...keys: string[]): string {
  if (!ref) return "";
  for (const key of keys) {
    const value = ref[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
  }
  return "";
}

/** Render only an origin so query strings, credentials, and relay paths never become UI. */
export function bindingEndpoint(value: string): string {
  if (!value) return "";
  try {
    return new URL(value).origin;
  } catch {
    return "custom endpoint";
  }
}

export function bindingRole(snapshot: RunBindingSnapshot | null | undefined, role: BindingRole): RunBindingRef | undefined {
  if (!snapshot?.roles) return undefined;
  // `planner` is the legacy name used by pre-Reason snapshots.
  return snapshot.roles[role] || (role === "reason" ? snapshot.roles.planner : undefined);
}

export function bindingProvider(ref: RunBindingRef | undefined): string {
  return bindingText(ref, "provider_ref", "provider", "label") || "Not configured";
}

export function bindingModel(ref: RunBindingRef | undefined): string {
  return bindingText(ref, "model", "engine");
}

export function bindingSummary(snapshot: RunBindingSnapshot | null | undefined, role: BindingRole): string {
  const ref = bindingRole(snapshot, role);
  if (!ref) return "binding unavailable";
  const provider = bindingProvider(ref);
  const model = bindingModel(ref);
  return model ? `${provider} / ${model}` : provider;
}

export function workerProfileLabel(snapshot: RunBindingSnapshot | null | undefined): string {
  const profile = snapshot?.worker_profile || snapshot?.worker_profiles?.[0];
  return bindingText(profile, "label", "name", "id", "engine") || "Default worker profile";
}