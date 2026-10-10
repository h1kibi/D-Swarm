﻿import { describe, expect, it } from "vitest";
import { groupTargetRunsByDirection, initialTargetId, projectTargets, targetForRun } from "../lib/targetProjection";
import { hasTargetDispatchMismatch } from "../lib/useRun";
import type { RunSummary } from "../lib/useRun";

function run(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "run-1", name: "web-001", category: "web", started: false,
    finished: false, solved: false, paused: false, status: "draft",
    pinned: false, archived: false, order: 0, updated: 1, ...overrides,
  };
}

describe("target projection", () => {

  it("uses durable target_id even when run names differ", () => {
    const targets = projectTargets([
      run({ run_id: "a", target_id: "target-1", target_name: "Portal", name: "Recon", updated: 2 }),
      run({ run_id: "b", target_id: "target-1", target_name: "Portal", name: "Auth bypass", updated: 3 }),
      run({ run_id: "c", target_id: "target-2", target_name: "Other", name: "Recon", updated: 4 }),
    ]);
    expect(targets).toHaveLength(2);
    expect(targets.find((target) => target.id === "target-1")?.runs).toHaveLength(2);
    expect(targets.find((target) => target.id === "target-1")?.name).toBe("Portal");
  });

  it("preserves Target dispatch mismatch diagnostics for the selected Run", () => {
    const mismatch = run({
      run_id: "startup-old",
      target_id: "target-bugku",
      dispatch_diagnostics: ["target_challenge_mismatch"],
      started: true,
      finished: true,
      status: "failed",
    });
    const target = projectTargets([mismatch], [{ id: "target-bugku", name: "Bugku", category: "web", runs: [] }])[0];
    expect(target.runs[0].dispatch_diagnostics).toEqual(["target_challenge_mismatch"]);
    expect(hasTargetDispatchMismatch(target.runs[0])).toBe(true);
    expect(hasTargetDispatchMismatch(run({ dispatch_diagnostics: ["provider_unhealthy"] }))).toBe(false);
  });

  it("keeps legacy runs grouped by the compatibility key", () => {
    const targets = projectTargets([
      run({ run_id: "a", name: "legacy", category: "web" }),
      run({ run_id: "b", name: "legacy", category: "web" }),
    ]);
    expect(targets).toHaveLength(1);
    expect(targets[0].id).toBe("legacy:web:legacy");
  });
  it("groups runs into one target and orders newest first", () => {
    const targets = projectTargets([
      run({ run_id: "older", updated: 10, status: "finished", finished: true }),
      run({ run_id: "newer", updated: 20, status: "running", started: true }),
      run({ run_id: "other", name: "pwn-002", category: "pwn", updated: 30 }),
    ]);
    expect(targets).toHaveLength(2);
    const target = targets.find((item) => item.category === "web");
    expect(target?.runs.map((item) => item.run_id)).toEqual(["newer", "older"]);
    expect(target?.status).toBe("active");
    expect(target?.activeCount).toBe(1);
    expect(targetForRun(targets, "older")?.id).toBe(target?.id);
  });

  it("projects solved, attention, flags, and archived states", () => {
    const solved = projectTargets([
      run({ run_id: "a", solved: true, started: true, finished: true, status: "solved", flag_count: 1 }),
      run({ run_id: "b", started: true, finished: true, status: "finished", flags: ["flag-2"], expected_flags: 2 }),
    ])[0];
    expect(solved.status).toBe("solved");
    expect(solved.flagCount).toBe(2);

    const attention = projectTargets([run({ run_id: "c", started: true, status: "failed", finished: true })])[0];
    expect(attention.status).toBe("failed");
    expect(attention.needsAttention).toBe(true);

    const archived = projectTargets([run({ run_id: "d", archived: true, finished: true, status: "finished" })])[0];
    expect(archived.status).toBe("archived");
  });

  it("projects editable target metadata without losing the target-first identity", () => {
    const targets = projectTargets([], [{
      id: "target-metadata",
      name: "Flag portal",
      category: "web",
      description: "Investigate the challenge endpoint",
      target_address: "https://portal.test",
      flag_format_hint: "flag{...}",
      updated_at: 12,
      runs: [],
    }]);

    expect(targets[0]).toMatchObject({
      id: "target-metadata",
      description: "Investigate the challenge endpoint",
      targetAddress: "https://portal.test",
      flagFormatHint: "flag{...}",
    });
  });

  it("preserves the persisted primary direction instead of assuming the first direction", () => {
    const targets = projectTargets([], [{
      id: "target-1",
      name: "Portal",
      category: "web",
      primary_direction_id: "direction-auth",
      directions: [
        { id: "direction-recon", label: "Recon" },
        { id: "direction-auth", label: "Authentication" },
      ],
      runs: [],
    }]);

    expect(targets[0].primaryDirection).toBe("Authentication");
    expect(targets[0].primaryDirectionId).toBe("direction-auth");
  });
  it("selects the newest durable target on boot but respects a deep-linked run", () => {
    const targets = projectTargets([], [
      { id: "target-old", name: "Old", category: "web", updated_at: 1, runs: [] },
      { id: "target-new", name: "New", category: "crypto", updated_at: 2, runs: [] },
    ]);
    expect(initialTargetId(targets, "draft-abc")).toBe("target-new");
    expect(initialTargetId(targets, "")).toBe("target-new");

    const withRun = projectTargets([
      run({ run_id: "run-deep", target_id: "target-old", target_name: "Old" }),
    ], [
      { id: "target-old", name: "Old", category: "web", runs: [] },
      { id: "target-new", name: "New", category: "crypto", runs: [] },
    ]);
    expect(initialTargetId(withRun, "run-deep")).toBe("target-old");
  });
  it("never lands on an empty placeholder card when a target owns runs", () => {
    const targets = projectTargets([], [
      { id: "target-smoke", name: "Smoke", category: "web", updated_at: 9, runs: [] },
      { id: "target-real", name: "Real", category: "reverse", updated_at: 1, runs: [] },
    ]);
    const withRuns = projectTargets([
      run({ run_id: "run-1", target_id: "target-real", target_name: "Real" }),
    ], [
      { id: "target-smoke", name: "Smoke", category: "web", updated_at: 9, runs: [] },
      { id: "target-real", name: "Real", category: "reverse", updated_at: 1 },
    ]);
    expect(initialTargetId(withRuns, "draft-abc")).toBe("target-real");
    // No runs anywhere: the newest card is still the deterministic fallback.
    expect(initialTargetId(targets, "draft-abc")).toBe("target-smoke");
  });
  it("prefers an attention target over older targets with runs", () => {
    const withAttention = projectTargets([
      run({ run_id: "run-old", target_id: "target-done", target_name: "Done", solved: true, finished: true }),
      run({ run_id: "run-help", target_id: "target-stuck", target_name: "Stuck", awaiting_help: true, status: "running" }),
    ]);
    expect(initialTargetId(withAttention, "draft-abc")).toBe("target-stuck");
  });
  it("keeps waiting runs distinct from active runs", () => {
    const targets = projectTargets([
      run({ run_id: "q", started: false, status: "queued", queued: true }),
      run({ run_id: "p", started: true, status: "paused", paused: true }),
    ]);
    expect(targets[0].status).toBe("waiting");
    expect(targets[0].activeCount).toBe(1);
    expect(targets[0].primaryDirection).toBe("web");
  });
});



describe("Target direction tree", () => {
  it("keeps persisted empty directions and attaches runs by direction id", () => {
    const target = {
      id: "target-1", name: "Portal", category: "web", status: "idle" as const, runs: [],
      activeCount: 0, verifiedFactCount: 0, flagCount: 0, updated: 1, needsAttention: false,
      primaryDirection: "Auth", primaryDirectionId: "direction-auth",
      directions: [
        { id: "direction-auth", label: "Auth" },
        { id: "direction-upload", label: "Upload" },
      ],
    };
    const base = { name: "run", category: "web", started: true, finished: false, solved: false, paused: false, status: "running" as const, pinned: false, archived: false, order: 0, updated: 1, run_id: "run-1" };
    const groups = groupTargetRunsByDirection(target, [
      { ...base, run_id: "run-auth", direction_id: "direction-auth" },
    ]);
    expect(groups.map((group) => group.label)).toEqual(["Auth", "Upload"]);
    expect(groups[0].runs.map((run) => run.run_id)).toEqual(["run-auth"]);
    expect(groups[1].runs).toHaveLength(0);
  });

  it("does not drop legacy runs without a direction id", () => {
    const target = { id: "target-1", name: "Portal", category: "web", status: "idle" as const, runs: [], activeCount: 0, verifiedFactCount: 0, flagCount: 0, updated: 1, needsAttention: false, primaryDirection: "General" };
    const run = { name: "run", category: "web", direction: "Legacy branch", run_id: "run-legacy", started: true, finished: false, solved: false, paused: false, status: "finished" as const, pinned: false, archived: false, order: 0, updated: 1 };
    const groups = groupTargetRunsByDirection(target, [run]);
    expect(groups[0].label).toBe("Legacy branch");
    expect(groups[0].runs[0].run_id).toBe("run-legacy");
  });
});
