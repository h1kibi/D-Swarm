/**
 * Run Fleet view-model tests (docs/07 §5.2, Phase 4): the Needs Attention
 * rule, per-filter membership, attention-first ordering, flag progress, and
 * batch-selection targeting for pause/resume/stop fan-out.
 */
import { describe, expect, it } from "vitest";
import type { RunSummary } from "../lib/useRun";
import {
  batchTargets,
  filterFleet,
  flagProgress,
  fleetCounts,
  runNeedsAttention,
  sortFleet,
  toggleSelection,
} from "../lib/fleet";

let n = 0;
function run(patch: Partial<RunSummary>): RunSummary {
  n += 1;
  return {
    run_id: `run-${n.toString().padStart(3, "0")}`,
    name: `run ${n}`,
    category: "web",
    started: true,
    finished: false,
    solved: false,
    paused: false,
    status: "running",
    pinned: false,
    archived: false,
    order: n,
    updated: n,
    updated_at: 1000 + n,
    ...patch,
  };
}

const LIVE = run({ run_id: "live", awaiting_help: false });
const ATTENTION = run({ run_id: "attn", awaiting_help: true });
const QUEUED = run({ run_id: "q", status: "queued", queued: true, queue_position: 3 });
const PAUSED = run({ run_id: "p", status: "paused", paused: true });
const SOLVED = run({ run_id: "s", status: "solved", solved: true, finished: true });
const FAILED = run({ run_id: "f", status: "failed", finished: true });
const ARCHIVED = run({ run_id: "a", archived: true, finished: true, status: "finished" });
const ALL = [LIVE, ATTENTION, QUEUED, PAUSED, SOLVED, FAILED, ARCHIVED];

describe("runNeedsAttention", () => {
  it("flags HITL-pending live runs and failures, not finished/idle runs", () => {
    expect(runNeedsAttention(ATTENTION)).toBe(true);
    expect(runNeedsAttention(FAILED)).toBe(true);
    expect(runNeedsAttention(LIVE)).toBe(false);
    expect(runNeedsAttention(run({ awaiting_help: true, finished: true }))).toBe(false);
  });
});

describe("filterFleet", () => {
  it("all hides archived; archived shows only archived", () => {
    expect(filterFleet(ALL, "all").map((r) => r.run_id)).not.toContain("a");
    expect(filterFleet(ALL, "archived").map((r) => r.run_id)).toEqual(["a"]);
  });

  it("active = in-flight, not queued, not terminal", () => {
    expect(filterFleet(ALL, "active").map((r) => r.run_id).sort()).toEqual(["attn", "live"]);
  });

  it("attention / queued / paused / solved / failed membership", () => {
    expect(filterFleet(ALL, "attention").map((r) => r.run_id)).toEqual(["attn", "f"]);
    expect(filterFleet(ALL, "queued").map((r) => r.run_id)).toEqual(["q"]);
    expect(filterFleet(ALL, "paused").map((r) => r.run_id)).toEqual(["p"]);
    expect(filterFleet(ALL, "solved").map((r) => r.run_id)).toEqual(["s"]);
    expect(filterFleet(ALL, "failed").map((r) => r.run_id)).toEqual(["f"]);
  });

  it("fleetCounts covers every filter", () => {
    const c = fleetCounts(ALL);
    expect(c.all).toBe(6);
    expect(c.attention).toBe(2);
    expect(c.archived).toBe(1);
  });
});

describe("sortFleet", () => {
  it("attention-first floats needs-attention, then active, then newest", () => {
    const sorted = sortFleet(ALL, "attention");
    expect(sorted[0].run_id).toBe("attn"); // attention + active
    expect(sorted[1].run_id).toBe("f");    // attention, terminal
    expect(sorted[2].run_id).toBe("q");    // active (queued), newest
    expect(sorted[3].run_id).toBe("live"); // active
    expect(sorted[sorted.length - 1].run_id).toBe("p"); // paused, oldest sinks
  });

  it("newest/oldest/name orderings do not mutate the input", () => {
    const before = ALL.map((r) => r.run_id);
    expect(sortFleet(ALL, "newest")[0].run_id).toBe("a"); // highest updated_at
    expect(sortFleet(ALL, "oldest")[0].run_id).toBe("live");
    expect(sortFleet(ALL, "name")[0].run_id).toBe("live"); // "run 1"
    expect(ALL.map((r) => r.run_id)).toEqual(before);
  });
});

describe("flagProgress", () => {
  it("reports got/need for started runs, null for drafts", () => {
    expect(flagProgress(run({ flags: ["f1"], expected_flags: 3 }))).toEqual({ got: 1, need: 3 });
    expect(flagProgress(run({ flag: "f1" }))).toEqual({ got: 1, need: 1 });
    expect(flagProgress(run({ started: false, status: "draft" }))).toBeNull();
  });
});

describe("batch selection", () => {
  it("toggleSelection adds and removes immutably", () => {
    const s0 = new Set<string>();
    const s1 = toggleSelection(s0, "live");
    expect(s0.size).toBe(0);
    expect([...s1]).toEqual(["live"]);
    expect([...toggleSelection(s1, "live")]).toEqual([]);
  });

  it("batchTargets applies per-action eligibility in selection order", () => {
    const sel = new Set(["live", "p", "s", "q"]);
    expect(batchTargets(ALL, sel, "pause")).toEqual(["live", "q"]);
    expect(batchTargets(ALL, sel, "resume")).toEqual(["p"]);
    expect(batchTargets(ALL, sel, "stop")).toEqual(["live", "q", "p"]);
    // stop never targets a terminal run (and never deletes history — it is a
    // hitl "stop", not a delete)
    expect(batchTargets(ALL, new Set(["s"]), "stop")).toEqual([]);
  });

  it("batchTargets archive/unarchive split the selection by archived state", () => {
    const sel = new Set(["live", "a", "s"]);
    expect(batchTargets(ALL, sel, "archive")).toEqual(["live", "s"]);
    expect(batchTargets(ALL, sel, "unarchive")).toEqual(["a"]);
    // a full-archived selection has nothing left to archive
    expect(batchTargets(ALL, new Set(["a"]), "archive")).toEqual([]);
  });
});
