import type { WorkerSettings } from "./useRun";

export type RosterPresetId = "ctf-7" | "quick-single";

type WorkerProfile = WorkerSettings["worker_profiles"][number];
type PresetConfig = Pick<
  WorkerSettings,
  | "worker_profiles"
  | "engines"
  | "start_workers"
  | "max_workers"
  | "worker_backend"
  | "overrides"
>;

const ROLES: WorkerProfile["roles"] = [
  "recon",
  "bootstrap",
  "explore",
  "respond",
  "review",
];

const IMAGE_ROOT = "ctf-swarm-pi";
const IMAGE_VERSION = "0.2.0";
const DEFAULT_ACCOUNT = "pi-main";

const directionImages: Record<string, string> = {
  web: `${IMAGE_ROOT}-web:${IMAGE_VERSION}`,
  pwn: `${IMAGE_ROOT}-pwn:${IMAGE_VERSION}`,
  rev: `${IMAGE_ROOT}-rev:${IMAGE_VERSION}`,
  crypto: `${IMAGE_ROOT}-crypto:${IMAGE_VERSION}`,
  misc: `${IMAGE_ROOT}-misc:${IMAGE_VERSION}`,
  forensics: `${IMAGE_ROOT}-forensics:${IMAGE_VERSION}`,
  aisec: `${IMAGE_ROOT}-aisec:${IMAGE_VERSION}`,
};

const makeProfile = (
  id: string,
  image: string,
  priority: number,
  maxRunning = 2,
  account = DEFAULT_ACCOUNT,
  model = "deepseek-v4-flash",
  effort = "medium"
): WorkerProfile => ({
  id,
  name: id,
  engine: "pi",
  transport: "pi_cli",
  auth: "api_key",
  credential_mode: "api_key",
  credential_account: account,
  api_key_ref: "",
  base_url: "",
  wire_api: "",
  runtime: "docker-web",
  roles: [...ROLES],
  image,
  race: true,
  max_running: maxRunning,
  max_review_running: 1,
  priority,
  model,
  effort,
  enabled: true,
});

const categoryOverrides = (): PresetConfig["overrides"] => ({
  web: { engines: ["pi-web"], start_workers: 1 },
  pwn: { engines: ["pi-pwn"], start_workers: 1 },
  reverse: { engines: ["pi-rev"], start_workers: 1 },
  rev: { engines: ["pi-rev"], start_workers: 1 },
  crypto: { engines: ["pi-crypto"], start_workers: 1 },
  misc: { engines: ["pi-misc"], start_workers: 1 },
  forensics: { engines: ["pi-forensics"], start_workers: 1 },
  aisec: { engines: ["pi-aisec"], start_workers: 1 },
});

const ctfSevenConfig = (): PresetConfig => {
  const directions = [
    ["pi-web", directionImages.web, "pi-web-main"],
    ["pi-pwn", directionImages.pwn, "pi-pwn-main"],
    ["pi-rev", directionImages.rev, "pi-rev-main"],
    ["pi-crypto", directionImages.crypto, "pi-crypto-main"],
    ["pi-misc", directionImages.misc, "pi-misc-main"],
    ["pi-forensics", directionImages.forensics, "pi-forensics-main"],
    ["pi-aisec", directionImages.aisec, "pi-aisec-main"],
  ] as const;
  return {
    worker_profiles: [
      makeProfile("pi-worker", `${IMAGE_ROOT}:${IMAGE_VERSION}`, 10, 3),
      ...directions.map(([id, image, account]) =>
        makeProfile(id, image, 20, 2, account)
      ),
    ],
    engines: [
      "pi-worker",
      "pi-web",
      "pi-pwn",
      "pi-rev",
      "pi-crypto",
      "pi-misc",
      "pi-forensics",
      "pi-aisec",
    ],
    start_workers: 1,
    max_workers: 17,
    worker_backend: "container",
    overrides: categoryOverrides(),
  };
};

const quickSingleConfig = (): PresetConfig => ({
  worker_profiles: [
    makeProfile("pi-worker", `${IMAGE_ROOT}:${IMAGE_VERSION}`, 10, 3),
  ],
  engines: ["pi-worker"],
  start_workers: 1,
  max_workers: 4,
  worker_backend: "container",
  overrides: {},
});

export interface RosterPreset {
  id: RosterPresetId;
  titleKey: string;
  descKey: string;
  image: string;
  profileCount: number;
}

export const ROSTER_PRESETS: RosterPreset[] = [
  {
    id: "ctf-7",
    titleKey: "settings.presetCtf7",
    descKey: "settings.presetCtf7Desc",
    image: `${IMAGE_ROOT}:${IMAGE_VERSION}`,
    profileCount: 7,
  },
  {
    id: "quick-single",
    titleKey: "settings.presetSingle",
    descKey: "settings.presetSingleDesc",
    image: `${IMAGE_ROOT}:${IMAGE_VERSION}`,
    profileCount: 1,
  },
];

export function buildPresetConfig(id: RosterPresetId): PresetConfig {
  if (id === "quick-single") return quickSingleConfig();
  return ctfSevenConfig();
}

const sortedNames = (profiles: WorkerProfile[]): string =>
  profiles
    .map((p) => p.name || p.id)
    .filter(Boolean)
    .sort()
    .join(",");

export function isPresetActive(
  id: RosterPresetId,
  cfg: Pick<WorkerSettings, "worker_profiles" | "engines" | "worker_backend" | "start_workers" | "max_workers">
): boolean {
  const preset = buildPresetConfig(id);
  return (
    sortedNames(cfg.worker_profiles) === sortedNames(preset.worker_profiles) &&
    [...cfg.engines].sort().join(",") === [...preset.engines].sort().join(",") &&
    cfg.worker_backend === preset.worker_backend &&
    cfg.start_workers === preset.start_workers &&
    cfg.max_workers === preset.max_workers
  );
}
