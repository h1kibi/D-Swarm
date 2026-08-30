import { describe, expect, it } from "vitest";
import {
  DETAIL_VIEWS,
  deckUrlForRun,
  detailUrlForRun,
  isDetailView,
  parseRunPath,
} from "./runRoute";

describe("run path parsing", () => {
  it("parses /run/<id>", () => {
    expect(parseRunPath("/run/run-1")).toEqual({ runId: "run-1" });
    expect(parseRunPath("/run/run-1/")).toEqual({ runId: "run-1" });
    expect(parseRunPath("/run/run%2Fx")).toEqual({ runId: "run/x" });
  });

  it("parses /run/<id>/<view> with whitelist validation", () => {
    expect(parseRunPath("/run/run-1/evidence")).toEqual({ runId: "run-1", view: "evidence" });
    expect(parseRunPath("/run/run-1/pocs/")).toEqual({ runId: "run-1", view: "pocs" });
    // unknown view segment → not a run path at all (404 page handles it)
    expect(parseRunPath("/run/run-1/nope")).toBeNull();
  });

  it("rejects non-run paths and empty ids", () => {
    expect(parseRunPath("/")).toBeNull();
    expect(parseRunPath("/settings/workers")).toBeNull();
    expect(parseRunPath("/run//evidence")).toBeNull();
    expect(parseRunPath("/run/run-1/evidence/extra")).toBeNull();
  });

  it("whitelist membership", () => {
    expect(isDetailView("evidence")).toBe(true);
    expect(isDetailView("nope")).toBe(false);
    // btw ships with its dedicated full-page chat
    expect(DETAIL_VIEWS).toContain("btw");
  });
});

describe("url builders", () => {
  it("maps real runs to /run/<id> and drafts to /", () => {
    expect(deckUrlForRun("run-1")).toBe("/run/run-1");
    expect(deckUrlForRun("draft-abc")).toBe("/");
  });

  it("builds detail urls and falls back for drafts", () => {
    expect(detailUrlForRun("run-1", "evidence")).toBe("/run/run-1/evidence");
    expect(detailUrlForRun("run-1", "btw")).toBe("/run/run-1/btw");
    expect(detailUrlForRun("draft-abc", "evidence")).toBe("/");
  });
});
