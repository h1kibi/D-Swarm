import { describe, expect, it } from "vitest";
import { formatTargetHintContext, normalizeTargetHints } from "../lib/targetHints";

describe("target hint launch context", () => {
  it("keeps only bounded text and stable ids", () => {
    expect(normalizeTargetHints([
      { id: "hint-auth", text: "  Check the login flow  ", source: "operator", pinned: true, secret: "never copy" },
      { id: "", text: "" },
      "not an object",
    ])).toEqual([{
      id: "hint-auth", text: "Check the login flow", source: "operator", pinned: true,
    }]);
  });

  it("marks Target hints as guidance rather than evidence", () => {
    const context = formatTargetHintContext([{ id: "h1", text: "Try the upload endpoint" }]);
    expect(context).toContain("[OPERATOR TARGET HINTS]");
    expect(context).toContain("not evidence");
    expect(context).toContain("Try the upload endpoint");
  });

  it("returns no prompt block for an empty card", () => {
    expect(formatTargetHintContext([])).toBe("");
  });
});
