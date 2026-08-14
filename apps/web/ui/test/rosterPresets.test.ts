import { describe, expect, it } from "vitest";
import {
  ROSTER_PRESETS,
  buildPresetConfig,
  isPresetActive,
} from "../lib/rosterPresets";

describe("roster presets", () => {
  it("exposes the two quick-start rosters", () => {
    expect(ROSTER_PRESETS.map((p) => p.id)).toEqual(["ctf-7", "quick-single"]);
  });

  it("builds the CTF 7-direction roster with hidden generic fallback", () => {
    const cfg = buildPresetConfig("ctf-7");
    expect(cfg.worker_profiles).toHaveLength(8);
    expect(cfg.engines).toEqual([
      "pi-worker",
      "pi-web",
      "pi-pwn",
      "pi-rev",
      "pi-crypto",
      "pi-misc",
      "pi-forensics",
      "pi-aisec",
    ]);
    expect(cfg.start_workers).toBe(1);
    expect(cfg.max_workers).toBe(17);
    expect(cfg.worker_backend).toBe("container");
    for (const p of cfg.worker_profiles) {
      expect(p.enabled).toBe(true);
      expect(p.runtime).toBe("docker-web");
      expect(p.image).toBeTruthy();
      expect(p.model).toBe("deepseek-v4-flash");
      expect(p.effort).toBe("medium");
    }
    expect(cfg.worker_profiles[0].credential_account).toBe("pi-main");
    expect(cfg.worker_profiles[1].credential_account).toBe("pi-web-main");
    expect(cfg.worker_profiles[2].credential_account).toBe("pi-pwn-main");
    expect(cfg.worker_profiles[7].credential_account).toBe("pi-aisec-main");
    expect(cfg.overrides.web.engines).toEqual(["pi-web"]);
    expect(cfg.overrides.aisec.engines).toEqual(["pi-aisec"]);
  });

  it("builds a single generic worker for a quick start", () => {
    const cfg = buildPresetConfig("quick-single");
    expect(cfg.worker_profiles).toHaveLength(1);
    expect(cfg.worker_profiles[0]).toMatchObject({
      id: "pi-worker",
      image: "ctf-swarm-pi:0.2.0",
      credential_account: "pi-main",
    });
    expect(cfg.engines).toEqual(["pi-worker"]);
    expect(cfg.start_workers).toBe(1);
    expect(cfg.max_workers).toBe(4);
  });

  it("detects whether a preset matches the active config", () => {
    const ctf = buildPresetConfig("ctf-7");
    expect(isPresetActive("ctf-7", ctf)).toBe(true);
    expect(isPresetActive("quick-single", ctf)).toBe(false);
    const single = buildPresetConfig("quick-single");
    expect(isPresetActive("quick-single", single)).toBe(true);
    expect(isPresetActive("ctf-7", single)).toBe(false);
  });
});
