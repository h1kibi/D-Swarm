/**
 * i18n key parity + hygiene (docs/07 §P1-6): every key must carry a non-empty
 * zh AND en string, keys must be unique (object literal dupes collapse
 * silently), and normalizer-referenced keys must exist.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { STRINGS } from "../lib/strings";

describe("i18n parity", () => {
  it("every key has non-empty zh and en", () => {
    const bad = Object.entries(STRINGS).filter(
      ([, v]) => !v.zh?.trim() || !v.en?.trim(),
    );
    expect(bad.map(([k]) => k)).toEqual([]);
  });

  it("has no duplicate keys in the source table", () => {
    const src = readFileSync(
      fileURLToPath(new URL("../lib/strings.ts", import.meta.url)),
      "utf-8",
    );
    const keys = [...src.matchAll(/^\s*"([a-z][a-zA-Z0-9.*]+)":\s*\{/gm)].map(
      (m) => m[1],
    );
    const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
    expect([...new Set(dupes)]).toEqual([]);
  });

  it("contains the keys referenced by the event normalizer", () => {
    for (const k of [
      "legacy.raceStarted",
      "legacy.raceConcluded",
      "legacy.coordinatorPlan",
      "legacy.engine",
    ]) {
      expect(STRINGS[k], k).toBeDefined();
    }
  });
});
