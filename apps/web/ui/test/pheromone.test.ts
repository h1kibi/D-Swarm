/**
 * Pheromone fold + strength tests (docs/07 §P0-4 / §6.3, Phase 5).
 *
 * Covers: the kernel half-life formula (hand-computed pairs), the §6.3 band
 * boundaries, fold tolerance for missing fields (→ N/A, never a throw),
 * arrival-order upsert semantics, reference-equality for unrelated events,
 * sort/filter pure functions, the finished-run clock freeze (replay virtual
 * time), and legacy-fixture replay degrading to an empty findings list.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  emptyDeck,
  EventType,
  reduce,
  type DeckState,
  type DSwarmEvent,
} from "../lib/events";
import {
  filterFindings,
  findingForFactSeq,
  findingKinds,
  foldFindingUpserted,
  formatAgeSec,
  parseIsoSec,
  pheromoneAgeSec,
  pheromoneBand,
  pheromoneClockSec,
  pheromoneStrength,
  sortFindingsByStrength,
  type PheromoneFindingView,
} from "../lib/pheromone";

let seq = 0;
function upsert(payload: Record<string, unknown>, ts = 1000): DSwarmEvent {
  return {
    event_type: EventType.BLACKBOARD_DELTA,
    seq: ++seq,
    ts,
    run_id: "run-test",
    payload: {
      kind: "finding_upserted",
      delta_type: "finding_upserted",
      actor: "projector",
      ...payload,
    },
  };
}

const full = {
  finding_id: "finding:7",
  finding_kind: "http_endpoint",
  target: "/admin",
  payload: { status: 200 },
  source_seq: 128,
  pheromone_base: 0.8,
  pheromone_half_life_sec: 3600,
  pheromone_created_at: "2026-08-07T00:00:00Z",
  experimental: true,
};

describe("foldFindingUpserted", () => {
  it("folds a projector finding_upserted delta with all pheromone params", () => {
    const out = foldFindingUpserted([], upsert(full));
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      findingId: "finding:7",
      kind: "http_endpoint",
      target: "/admin",
      payload: { status: 200 },
      sourceSeq: 128,
      base: 0.8,
      halfLifeSec: 3600,
      experimental: true,
    });
    // ISO UTC → epoch seconds (2026-08-07T00:00:00Z)
    expect(out[0].createdAt).toBe(Date.parse("2026-08-07T00:00:00Z") / 1000);
  });

  it("ignores non-projector actors, other kinds and other event types (same ref)", () => {
    const start: PheromoneFindingView[] = [];
    expect(foldFindingUpserted(start, upsert({ ...full, actor: "worker-1" }))).toBe(start);
    expect(foldFindingUpserted(start, upsert({ kind: "fact_added", finding_id: "x" }))).toBe(start);
    expect(
      foldFindingUpserted(start, {
        event_type: EventType.WORKER_STATUS,
        seq: 1, ts: 1, run_id: "r", payload: {},
      }),
    ).toBe(start);
  });

  it("tolerates missing pheromone params (→ N/A fields, no throw)", () => {
    const out = foldFindingUpserted([], upsert({ finding_id: "finding:1", finding_kind: "k" }));
    expect(out).toHaveLength(1);
    expect(out[0].base).toBeUndefined();
    expect(out[0].halfLifeSec).toBeUndefined();
    expect(out[0].createdAt).toBeUndefined();
    expect(out[0].target).toBe("");
    expect(out[0].payload).toEqual({});
    expect(pheromoneStrength(out[0], 999999)).toBeUndefined(); // → N/A
  });

  it("tolerates a garbled created_at and a missing finding_id", () => {
    const bad = foldFindingUpserted([], upsert({ ...full, pheromone_created_at: "not-a-date" }));
    expect(bad[0].createdAt).toBeUndefined();
    // no finding_id and no source_seq → no stable key → skipped silently
    expect(foldFindingUpserted([], upsert({ finding_kind: "k" }))).toHaveLength(0);
    // source_seq alone synthesizes a stable key
    const keyed = foldFindingUpserted([], upsert({ finding_kind: "k", source_seq: 42 }));
    expect(keyed[0].findingId).toBe("finding:seq-42");
  });

  it("upserts by findingId while preserving arrival order", () => {
    let out = foldFindingUpserted([], upsert({ ...full, finding_id: "finding:1" }));
    out = foldFindingUpserted(out, upsert({ ...full, finding_id: "finding:2" }));
    out = foldFindingUpserted(out, upsert({ ...full, finding_id: "finding:1", pheromone_base: 0.5 }));
    expect(out.map((f) => f.findingId)).toEqual(["finding:1", "finding:2"]);
    expect(out[0].base).toBe(0.5);
  });
});

describe("pheromoneStrength — the kernel formula (base × 2^(-age/half), clamp [0,1])", () => {
  const created = Date.parse("2026-08-07T00:00:00Z") / 1000;
  const f = { base: 0.8, halfLifeSec: 3600, createdAt: created };

  it("matches hand-computed decay points", () => {
    expect(pheromoneStrength(f, created)).toBeCloseTo(0.8, 10); // age 0 → base
    expect(pheromoneStrength(f, created + 3600)).toBeCloseTo(0.4, 10); // one half-life
    expect(pheromoneStrength(f, created + 7200)).toBeCloseTo(0.2, 10); // two
    expect(pheromoneStrength(f, created + 1800)).toBeCloseTo(0.8 * Math.pow(0.5, 0.5), 10);
  });

  it("never goes negative for future created_at (age floors at 0)", () => {
    expect(pheromoneStrength(f, created - 5000)).toBeCloseTo(0.8, 10);
  });

  it("clamps to [0, 1] and floors half-life at 1s", () => {
    expect(pheromoneStrength({ base: 1.4, halfLifeSec: 3600, createdAt: created }, created)).toBe(1);
    const tiny = pheromoneStrength({ base: 1, halfLifeSec: 0, createdAt: created }, created + 10);
    expect(tiny).toBeCloseTo(Math.pow(0.5, 10), 10);
  });

  it("returns undefined (N/A) when any immutable param is missing", () => {
    expect(pheromoneStrength({ halfLifeSec: 3600, createdAt: created }, created)).toBeUndefined();
    expect(pheromoneStrength({ base: 0.8, createdAt: created }, created)).toBeUndefined();
    expect(pheromoneStrength({ base: 0.8, halfLifeSec: 3600 }, created)).toBeUndefined();
    expect(pheromoneStrength({}, created)).toBeUndefined();
  });
});

describe("pheromoneBand — §6.3 boundaries", () => {
  it("0.75–1.00 hot / 0.40–0.74 warm / 0.15–0.39 cool / <0.15 faint", () => {
    expect(pheromoneBand(1)).toBe("hot");
    expect(pheromoneBand(0.75)).toBe("hot");
    expect(pheromoneBand(0.74)).toBe("warm");
    expect(pheromoneBand(0.4)).toBe("warm");
    expect(pheromoneBand(0.39)).toBe("cool");
    expect(pheromoneBand(0.15)).toBe("cool");
    expect(pheromoneBand(0.14)).toBe("faint");
    expect(pheromoneBand(0)).toBe("faint");
    expect(pheromoneBand(undefined)).toBe("na");
  });
});

describe("pheromone helpers", () => {
  const created = Date.parse("2026-08-07T00:00:00Z") / 1000;

  it("parseIsoSec / pheromoneAgeSec / formatAgeSec", () => {
    expect(parseIsoSec("2026-08-07T00:00:00Z")).toBe(created);
    expect(parseIsoSec("junk")).toBeUndefined();
    expect(parseIsoSec(undefined)).toBeUndefined();
    expect(pheromoneAgeSec({ createdAt: created }, created + 90)).toBe(90);
    expect(pheromoneAgeSec({ createdAt: created }, created - 5)).toBe(0);
    expect(pheromoneAgeSec({}, created)).toBeUndefined();
    expect(formatAgeSec(45)).toBe("45s");
    expect(formatAgeSec(360)).toBe("6m");
    expect(formatAgeSec(7200)).toBe("2h");
    expect(formatAgeSec(86400 * 3)).toBe("3d");
  });

  it("pheromoneClockSec freezes finished runs at finishedAt (replay virtual time)", () => {
    expect(pheromoneClockSec({ finished: false }, 5000)).toBe(5000);
    expect(pheromoneClockSec({ finished: true, finishedAt: 4000 }, 5000)).toBe(4000);
    // finishedAt may arrive in ms (>= 1e12, the deck's sec/ms threshold) — normalised
    expect(pheromoneClockSec({ finished: true, finishedAt: 1_750_000_000_000 }, 5e9)).toBe(1_750_000_000);
    expect(pheromoneClockSec({ finished: true }, 5000)).toBe(5000); // no finishedAt → wall clock
  });

  it("findingForFactSeq links a fact to its projected finding", () => {
    const findings = foldFindingUpserted([], upsert(full));
    expect(findingForFactSeq(findings, 128)?.findingId).toBe("finding:7");
    expect(findingForFactSeq(findings, 999)).toBeUndefined();
    expect(findingForFactSeq(findings, undefined)).toBeUndefined();
  });

  it("findingKinds / filterFindings", () => {
    let fs = foldFindingUpserted([], upsert({ ...full, finding_id: "f1", finding_kind: "http_endpoint" }));
    fs = foldFindingUpserted(fs, upsert({ ...full, finding_id: "f2", finding_kind: "cred", experimental: false }));
    fs = foldFindingUpserted(fs, upsert({ ...full, finding_id: "f3", finding_kind: "http_endpoint" }));
    expect(findingKinds(fs)).toEqual(["http_endpoint", "cred"]);
    expect(filterFindings(fs, { kind: "http_endpoint" }).map((f) => f.findingId)).toEqual(["f1", "f3"]);
    expect(filterFindings(fs, { experimentalOnly: true }).map((f) => f.findingId)).toEqual(["f1", "f3"]);
    expect(filterFindings(fs, {})).toHaveLength(3);
  });

  it("sortFindingsByStrength sorts desc, N/A last, ties keep arrival order", () => {
    const mk = (id: string, base?: number, ageSec = 0): PheromoneFindingView => ({
      findingId: id, kind: "k", target: "", payload: {},
      base, halfLifeSec: 3600, createdAt: base == null ? undefined : 1000 - ageSec,
      experimental: true,
    });
    const input = [mk("na"), mk("low", 0.2), mk("high", 0.9), mk("tie", 0.2)];
    const sorted = sortFindingsByStrength(input, 1000);
    expect(sorted.map((f) => f.findingId)).toEqual(["high", "low", "tie", "na"]);
    const asc = sortFindingsByStrength(input, 1000, "asc");
    expect(asc.map((f) => f.findingId)).toEqual(["low", "tie", "high", "na"]);
    // input untouched
    expect(input.map((f) => f.findingId)).toEqual(["na", "low", "high", "tie"]);
  });
});

describe("reducer integration + legacy degradation", () => {
  it("reduce() folds finding_upserted into deck.findings", () => {
    let deck: DeckState = emptyDeck("run-test");
    deck = reduce(deck, upsert(full));
    expect(deck.findings).toHaveLength(1);
    expect(deck.findings[0].findingId).toBe("finding:7");
    // an unrelated delta leaves the array reference untouched
    const before = deck.findings;
    deck = reduce(deck, {
      event_type: EventType.BLACKBOARD_DELTA,
      seq: 999, ts: 1001, run_id: "run-test",
      payload: { kind: "fact_added", actor: "w1", fact: "x", fact_seq: 5 },
    });
    expect(deck.findings).toBe(before);
  });

  it.each(["legacy-race.session.jsonl", "legacy-coordinator.session.jsonl"])(
    "legacy fixture %s replays to an empty findings list (N/A degradation)",
    (name) => {
      const path = fileURLToPath(new URL(`../test/fixtures/${name}`, import.meta.url));
      const events = readFileSync(path, "utf-8")
        .split("\n")
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line) as DSwarmEvent);
      let deck = emptyDeck(events[0]?.run_id ?? "unknown");
      for (const ev of events) deck = reduce(deck, ev);
      expect(deck.findings).toEqual([]);
    },
  );
});
