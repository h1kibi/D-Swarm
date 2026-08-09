/**
 * Session-replay fixture tests (docs/07 Phase 1 acceptance): real historical
 * sessions must load through the normalizer + reducer with no exceptions,
 * retired-path events must map to generic legacy activity, and the deck state
 * must recover workers / evidence / terminal status.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { emptyDeck, reduce, type DeckState, type DSwarmEvent } from "../lib/events";
import { normalizeStream, type NormalizedEvent } from "../lib/normalize";

function loadFixture(name: string): DSwarmEvent[] {
  const path = fileURLToPath(new URL(`../test/fixtures/${name}`, import.meta.url));
  return readFileSync(path, "utf-8")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as DSwarmEvent);
}

function replay(name: string): { state: DeckState; norm: NormalizedEvent[] } {
  const raw = loadFixture(name);
  const norm = normalizeStream(raw);
  let state = emptyDeck(raw[0]?.run_id ?? "unknown");
  for (const n of norm) state = reduce(state, n.raw);
  return { state, norm };
}

describe("legacy session replay", () => {
  it("replays a race-era session without reducer errors", () => {
    const { state } = replay("legacy-race.session.jsonl");
    expect(state.started).toBe(true);
    expect(state.finished).toBe(true);
    expect(Object.keys(state.lanes).length).toBeGreaterThan(0);
  });

  it("maps race events to generic legacy activity, never a live mode", () => {
    const { norm } = replay("legacy-race.session.jsonl");
    const race = norm.filter((n) => n.legacyActivity?.kind === "race");
    expect(race.length).toBeGreaterThanOrEqual(2); // race_started + race_concluded
    for (const n of race) {
      expect(n.legacyActivity?.i18nKey).toMatch(/^legacy\./);
      expect(n.stage).toBe("legacy");
    }
  });

  it("replays a coordinator-era session without reducer errors", () => {
    const { state } = replay("legacy-coordinator.session.jsonl");
    expect(state.started).toBe(true);
    expect(state.finished).toBe(true);
    expect(Object.keys(state.lanes).length).toBeGreaterThan(0);
  });

  it("derives an approximate stage for legacy sessions", () => {
    const { norm } = replay("legacy-coordinator.session.jsonl");
    const staged = norm.filter((n) => n.stage !== undefined);
    expect(staged.length).toBeGreaterThan(0);
    // every legacy stage is derived, never explicit
    for (const n of staged) expect(n.stageDerived).toBe(true);
    // the stream ends at finalize
    expect(norm[norm.length - 1].stage).toBe("finalize");
  });

  it("recovers evidence from blackboard deltas", () => {
    const { state } = replay("legacy-coordinator.session.jsonl");
    const facts = Object.keys(state.blackboard?.facts ?? {});
    expect(facts.length).toBeGreaterThan(0);
  });
});
