import { describe, expect, it } from "vitest";
import type { CredentialAccount, WorkerSettings } from "../lib/useRun";
import {
  WORKER_DIRECTIONS,
  batchWorkerFields,
  cloneRuntimeForDirection,
  copyWorkerFields,
  customWorkers,
  directionForProfile,
  setSecretUpdate,
  synthesizeDirectionProfiles,
  systemWorker,
  workerReadiness,
} from "../lib/workerSettingsDraft";

const runtimeProfiles: WorkerSettings["runtime_profiles"] = [
  { id: "local", backend: "local", label: "Local" },
  {
    id: "docker-web",
    backend: "container",
    label: "Docker web",
    network: "bridge",
    memory: "12g",
    cpus: "4",
    pids_limit: 2048,
  },
];

const profile = (
  id: string,
  patch: Partial<WorkerSettings["worker_profiles"][number]> = {}
): WorkerSettings["worker_profiles"][number] => ({
  id,
  name: id,
  label: id,
  engine: "pi",
  transport: "pi_cli",
  auth: "api_key",
  credential_mode: "api_key",
  credential_account: `${id}-main`,
  base_url: "https://api.example.test/v1",
  runtime: "docker-web",
  roles: ["recon", "bootstrap", "explore", "respond", "review"],
  max_running: 2,
  max_review_running: 1,
  priority: 20,
  model: "example-model",
  effort: "medium",
  image: `${id}:test`,
  enabled: false,
  ...patch,
});

const config = (profiles: WorkerSettings["worker_profiles"]): WorkerSettings => ({
  engines: [],
  max_workers: 0,
  worker_backend: "container",
  wall_clock_budget: 0,
  max_total_workers: 0,
  cost_budget_usd: 0,
  review_policy: { enabled: true, engine: "pi-worker" },
  llm_providers: [],
  llm_profiles: {
    planner: { provider: "deepseek", model: "planner" },
    titler: { provider: "deepseek", model: "titler" },
  },
  runtime_profiles: runtimeProfiles,
  worker_profiles: profiles,
  overrides: {},
});

const account = (accountId: string): CredentialAccount => ({
  account_id: accountId,
  engine: "pi",
  mode: "custom_endpoint",
  present: true,
  writable_state: false,
  details: {
    has_secret: true,
    base_url_value: "https://api.example.test/v1",
  },
});

describe("worker settings draft helpers", () => {
  it("always synthesizes seven directions without changing migrated enablement", () => {
    const existing = profile("pi-web", { enabled: true, model: "kept-model" });
    const next = synthesizeDirectionProfiles(config([
      profile("pi-worker"),
      existing,
    ]));

    const directions = next.worker_profiles.filter((row) => directionForProfile(row));
    expect(directions).toHaveLength(WORKER_DIRECTIONS.length);
    expect(directions.map((row) => directionForProfile(row))).toEqual(
      WORKER_DIRECTIONS.map((row) => row.key)
    );
    expect(next.worker_profiles.find((row) => row.id === "pi-web")).toMatchObject({
      enabled: true,
      model: "kept-model",
    });
    expect(directions.filter((row) => row.id !== "pi-web").every((row) => !row.enabled)).toBe(true);
  });

  it("copies convenience fields but never credential identity", () => {
    const source = profile("pi-web", {
      credential_account: "source-secret-account",
      api_key_ref: "env:SOURCE_KEY",
      model: "copied-model",
      wire_api: "openai-responses",
      auth_mode: "custom",
      auth_header: "X-API-Token",
      auth_prefix: "Token",
      runtime: "local",
      max_running: 7,
    });
    const target = profile("pi-pwn", {
      credential_account: "target-account",
      api_key_ref: "env:TARGET_KEY",
    });

    const copied = copyWorkerFields(source, target);

    expect(copied.model).toBe("copied-model");
    expect(copied.wire_api).toBe("openai-responses");
    expect(copied.auth_mode).toBe("custom");
    expect(copied.auth_header).toBe("X-API-Token");
    expect(copied.auth_prefix).toBe("Token");
    expect(copied.runtime).toBe("local");
    expect(copied.max_running).toBe(7);
    expect(copied.credential_account).toBe("target-account");
    expect(copied.api_key_ref).toBe("env:TARGET_KEY");
  });

  it("applies only batch-safe fields", () => {
    const rows = [profile("pi-web"), profile("pi-pwn")];
    const next = batchWorkerFields(rows, ["pi-web"], {
      model: "batch-model",
      runtime: "local",
      max_running: 4,
    });

    expect(next[0]).toMatchObject({ model: "batch-model", runtime: "local", max_running: 4 });
    expect(next[0].credential_account).toBe("pi-web-main");
    expect(next[1]).toEqual(rows[1]);
  });

  it("treats blank keys as retain and supports explicit replace/remove", () => {
    expect(setSecretUpdate([], "pi-web-main", "retain")).toEqual([]);
    expect(setSecretUpdate([], "pi-web-main", "replace", "   ")).toEqual([]);
    expect(setSecretUpdate([], "pi-web-main", "replace", "new-key", "https://x.test/v1"))
      .toEqual([{
        account_id: "pi-web-main",
        action: "replace",
        value: "new-key",
        base_url: "https://x.test/v1",
      }]);
    expect(setSecretUpdate([], "pi-web-main", "remove")).toEqual([
      { account_id: "pi-web-main", action: "remove" },
    ]);
  });

  it("clones a direction-private runtime without mutating the source", () => {
    const cloned = cloneRuntimeForDirection(runtimeProfiles, "web", "docker-web");

    expect(cloned.runtimeId).toBe("direction-web-custom");
    expect(cloned.runtimes).toHaveLength(runtimeProfiles.length + 1);
    expect(cloned.runtimes.find((row) => row.id === cloned.runtimeId)).toMatchObject({
      backend: "container",
      memory: "12g",
      cpus: "4",
    });
    expect(runtimeProfiles).toHaveLength(2);

    const repeated = cloneRuntimeForDirection(cloned.runtimes, "web", "docker-web");
    expect(repeated.runtimes).toBe(cloned.runtimes);
  });

  it("reports provider-bound profiles without per-worker key duplication", () => {
    const web = profile("pi-web", { provider_ref: "deepseek", base_url: "", credential_account: "legacy-unused" });
    const providers = [{ id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com/v1", models: ["deepseek-chat"] }];
    expect(workerReadiness(web, runtimeProfiles, [], [], [
      { provider_id: "deepseek", present: true },
    ], [], providers)).toEqual({ ready: true, missing: [] });
    expect(workerReadiness(web, runtimeProfiles, [], [], [], [], providers).missing).toContain("api-key");
    expect(workerReadiness(web, runtimeProfiles, [], [], [], [
      { provider_id: "deepseek", action: "replace", value: "sk-test" },
    ], providers).missing).not.toContain("api-key");
  });

  it("reports the complete readiness chain", () => {
    const web = profile("pi-web");
    expect(workerReadiness(web, runtimeProfiles, [account("pi-web-main")], []))
      .toEqual({ ready: true, missing: [] });

    const missing = profile("pi-web", {
      base_url: "",
      model: "",
      runtime: "missing",
      image: "",
      max_running: 0,
    });
    expect(workerReadiness(missing, runtimeProfiles, [], [])).toEqual({
      ready: false,
      missing: ["endpoint", "api-key", "model", "runtime", "capacity"],
    });

    expect(workerReadiness(web, runtimeProfiles, [account("pi-web-main")], [
      { account_id: "pi-web-main", action: "remove" },
    ]).missing).toContain("api-key");
  });

  it("classifies System and custom Workers as non-directional/manual-only", () => {
    const system = profile("pi-worker");
    const manual = profile("manual-specialist", { label: "Manual Specialist", enabled: true });
    const rows = [system, profile("pi-web"), manual];

    expect(systemWorker(rows)).toBe(system);
    expect(customWorkers(rows)).toEqual([manual]);
    expect(directionForProfile(manual)).toBeNull();
  });
});
