import { describe, expect, it } from "vitest";
import {
  clampWorkerSettingsMasterWidth,
  defaultWorkerSettingsMasterWidth,
} from "../lib/workerSettingsSizing";

describe("Worker settings split sizing", () => {
  it("uses a balanced proportional default", () => {
    expect(defaultWorkerSettingsMasterWidth(1200)).toBe(540);
  });

  it("enforces the Worker list minimum", () => {
    expect(clampWorkerSettingsMasterWidth(120, 1200)).toBe(500);
  });

  it("preserves the editor minimum at the maximum", () => {
    expect(clampWorkerSettingsMasterWidth(1100, 1200)).toBe(730);
  });

  it("uses the configured fallback for non-finite widths", () => {
    expect(clampWorkerSettingsMasterWidth(Number.NaN, 1200)).toBe(620);
  });
});
