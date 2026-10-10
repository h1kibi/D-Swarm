import { describe, expect, it } from "vitest";
import { DETAIL_VIEWS, parseRunPath, detailUrlForRun, isDetailView } from "../lib/runRoute";

describe("Run investigation workspace routes", () => {
  it("includes all first-class investigation views in stable order", () => {
    expect(DETAIL_VIEWS).toEqual([
      "workers",
      "intents",
      "evidence",
      "routes",
      "timeline",
      "ledger",
      "graph",
      "blackboard",
      "findings",
      "credentials",
      "pocs",
      "directives",
      "btw",
    ]);
  });

  it("parses primary investigation routes", () => {
    expect(parseRunPath("/run/run-101/workers")).toEqual({ runId: "run-101", view: "workers" });
    expect(parseRunPath("/run/run-101/intents")).toEqual({ runId: "run-101", view: "intents" });
    expect(parseRunPath("/run/run-101/ledger")).toEqual({ runId: "run-101", view: "ledger" });
    expect(parseRunPath("/run/run-101/routes")).toEqual({ runId: "run-101", view: "routes" });
    expect(parseRunPath("/run/run-101/btw")).toEqual({ runId: "run-101", view: "btw" });
  });

  it("validates detail view membership", () => {
    expect(isDetailView("workers")).toBe(true);
    expect(isDetailView("intents")).toBe(true);
    expect(isDetailView("ledger")).toBe(true);
    expect(isDetailView("routes")).toBe(true);
    expect(isDetailView("arbitrary")).toBe(false);
  });

  it("builds correct detail URLs", () => {
    expect(detailUrlForRun("run-42", "intents")).toBe("/run/run-42/intents");
    expect(detailUrlForRun("run-42", "ledger")).toBe("/run/run-42/ledger");
    expect(detailUrlForRun("run-42", "routes")).toBe("/run/run-42/routes");
  });
});
