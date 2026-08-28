import type {
  CredentialAccount,
  WorkerSecretUpdate,
  ProviderSecretUpdate,
  LLMProviderSecretMeta,
  WorkerSettings,
} from "./useRun";

export const WORKER_DIRECTIONS = [
  { key: "web", id: "pi-web", label: "Web", category: "web" },
  { key: "pwn", id: "pi-pwn", label: "Pwn", category: "pwn" },
  { key: "rev", id: "pi-rev", label: "Rev", category: "reverse" },
  { key: "crypto", id: "pi-crypto", label: "Crypto", category: "crypto" },
  { key: "misc", id: "pi-misc", label: "杂项", category: "misc" },
  { key: "forensics", id: "pi-forensics", label: "Forensics", category: "forensics" },
  { key: "aisec", id: "pi-aisec", label: "AI Security", category: "aisec" },
] as const;

export type WorkerProfile = WorkerSettings["worker_profiles"][number];
export type RuntimeProfile = WorkerSettings["runtime_profiles"][number];
export type DirectionKey = (typeof WORKER_DIRECTIONS)[number]["key"];

export const BUILTIN_RUNTIME_IDS = new Set([
  "local",
  "docker-web",
  "docker-host-target",
  "docker-offline",
  "docker-pwn-heavy",
]);

export function profileLabel(profile: WorkerProfile): string {
  return String(profile.label || profile.name || profile.id || "").trim();
}

export function profileRef(profile: WorkerProfile): string {
  return String(profile.name || profile.id || profileLabel(profile)).trim();
}

export function directionForProfile(profile: WorkerProfile): DirectionKey | null {
  const label = profileLabel(profile).toLowerCase();
  const match = WORKER_DIRECTIONS.find((direction) => direction.id === label);
  return match?.key ?? null;
}

export function systemWorker(profiles: WorkerProfile[]): WorkerProfile | undefined {
  return profiles.find((profile) => profileLabel(profile).toLowerCase() === "pi-worker");
}

function directionDefault(direction: (typeof WORKER_DIRECTIONS)[number], template?: WorkerProfile): WorkerProfile {
  return {
    id: direction.id,
    name: direction.id,
    label: direction.id,
    engine: "pi",
    transport: "pi_cli",
    auth: "api_key",
    credential_mode: "api_key",
    credential_account: `${direction.id}-main`,
    api_key_ref: "",
    base_url: "https://api.deepseek.com/v1",
    wire_api: "auto",
    auth_mode: "bearer",
    auth_header: "Authorization",
    auth_prefix: "Bearer",
    runtime: template?.runtime || "docker-web",
    roles: ["recon", "bootstrap", "explore", "respond", "review"],
    max_running: Math.max(1, Number(template?.max_running || 2)),
    max_review_running: Math.max(1, Number(template?.max_review_running || 1)),
    priority: Number(template?.priority || 20),
    model: template?.model || "deepseek-v4-flash",
    effort: template?.effort || "medium",
    image: template?.image || "ctf-swarm-pi:0.2.0",
    enabled: false,
  };
}

/** Always present the seven product directions, without enabling new seats during migration. */
export function synthesizeDirectionProfiles(config: WorkerSettings): WorkerSettings {
  const next = structuredClone(config);
  const profiles = next.worker_profiles || [];
  const template = systemWorker(profiles) || profiles[0];
  for (const direction of WORKER_DIRECTIONS) {
    if (!profiles.some((profile) => directionForProfile(profile) === direction.key)) {
      profiles.push(directionDefault(direction, template));
    }
  }
  next.worker_profiles = profiles;
  return next;
}

export function customWorkers(profiles: WorkerProfile[]): WorkerProfile[] {
  return profiles.filter((profile) => !directionForProfile(profile) && profileLabel(profile) !== "pi-worker");
}

export function configuredAccount(accounts: CredentialAccount[], accountId: string): CredentialAccount | undefined {
  return accounts.find((account) => account.account_id === accountId && account.present);
}

export function endpointForProfile(profile: WorkerProfile, accounts: CredentialAccount[]): string {
  if (profile.base_url) return profile.base_url;
  const account = configuredAccount(accounts, profile.credential_account);
  const details = account?.details || {};
  return String(details.base_url_value || details.base_url || "");
}

export function workerReadiness(
  profile: WorkerProfile,
  runtimes: RuntimeProfile[],
  accounts: CredentialAccount[],
  secretUpdates: WorkerSecretUpdate[],
  providerSecrets: LLMProviderSecretMeta[] = [],
  providerSecretUpdates: ProviderSecretUpdate[] = [],
  providers: WorkerSettings["llm_providers"] = []
): { ready: boolean; missing: string[] } {
  const missing: string[] = [];
  const providerRef = String(profile.provider_ref || "").trim();
  if (providerRef) {
    const provider = providers.find((row) => row.id === providerRef);
    const staged = providerSecretUpdates.find((row) => row.provider_id === providerRef);
    const hasProviderSecret = staged?.action === "replace"
      ? Boolean(staged.value?.trim())
      : staged?.action === "remove"
        ? false
        : Boolean(providerSecrets.find((row) => row.provider_id === providerRef && row.present));
    if (!provider) missing.push("provider");
    else if (!provider.base_url) missing.push("endpoint");
    if (!hasProviderSecret) missing.push("api-key");
  } else {
    const accountId = String(profile.credential_account || "").trim();
    const secretUpdate = secretUpdates.find((row) => row.account_id === accountId);
    const hasSecret = secretUpdate?.action === "replace"
      ? Boolean(secretUpdate.value?.trim())
      : secretUpdate?.action === "remove"
        ? false
        : Boolean(configuredAccount(accounts, accountId));
    if (!endpointForProfile(profile, accounts) && !secretUpdate?.base_url) missing.push("endpoint");
    if (!hasSecret) missing.push("api-key");
  }
  if (!String(profile.model || "").trim()) missing.push("model");
  const runtime = runtimes.find((row) => row.id === profile.runtime);
  if (!runtime) missing.push("runtime");
  if (runtime?.backend === "container" && !String(profile.image || "").trim()) missing.push("image");
  if (Number(profile.max_running || 0) < 1) missing.push("capacity");
  return { ready: missing.length === 0, missing };
}

/** Copy convenience fields only. Credential ids and write-only secret state never move. */
export function copyWorkerFields(source: WorkerProfile, target: WorkerProfile): WorkerProfile {
  return {
    ...target,
    provider_ref: source.provider_ref || "",
    base_url: source.base_url || "",
    model: source.model || "",
    effort: source.effort || "medium",
    wire_api: source.wire_api || "auto",
    auth_mode: source.auth_mode || "bearer",
    auth_header: source.auth_header || "Authorization",
    auth_prefix: source.auth_prefix ?? "Bearer",
    runtime: source.runtime,
    image: source.image || "",
    max_running: source.max_running,
    max_review_running: source.max_review_running,
    priority: source.priority,
  };
}

export function batchWorkerFields(
  profiles: WorkerProfile[],
  ids: string[],
  patch: Partial<Pick<WorkerProfile, "provider_ref" | "model" | "effort" | "runtime" | "image" | "max_running" | "max_review_running">>
): WorkerProfile[] {
  const selected = new Set(ids);
  return profiles.map((profile) => selected.has(profileLabel(profile)) ? { ...profile, ...patch } : profile);
}

export function setSecretUpdate(
  updates: WorkerSecretUpdate[],
  accountId: string,
  action: "retain" | "replace" | "remove",
  value = "",
  baseUrl = ""
): WorkerSecretUpdate[] {
  const remaining = updates.filter((row) => row.account_id !== accountId);
  if (action === "retain") return remaining;
  if (action === "remove") return [...remaining, { account_id: accountId, action: "remove" }];
  if (!value.trim()) return remaining; // blank means retain
  return [...remaining, { account_id: accountId, action: "replace", value, base_url: baseUrl }];
}

export function cloneRuntimeForDirection(
  runtimes: RuntimeProfile[],
  direction: DirectionKey,
  sourceRuntimeId: string
): { runtimes: RuntimeProfile[]; runtimeId: string } {
  const runtimeId = `direction-${direction}-custom`;
  if (runtimes.some((row) => row.id === runtimeId)) return { runtimes, runtimeId };
  const source = runtimes.find((row) => row.id === sourceRuntimeId) || runtimes[0];
  const clone: RuntimeProfile = {
    id: runtimeId,
    backend: source?.backend || "container",
    label: `${WORKER_DIRECTIONS.find((row) => row.key === direction)?.label || direction} private`,
    network: source?.network || (source?.backend === "local" ? "" : "bridge"),
    memory: source?.memory || "",
    cpus: source?.cpus || "",
    pids_limit: Number(source?.pids_limit || 0),
  };
  return { runtimes: [...runtimes, clone], runtimeId };
}
