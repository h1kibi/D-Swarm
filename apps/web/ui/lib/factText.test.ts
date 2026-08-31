import { describe, expect, it } from "vitest";
import { prettyFact } from "./factText";

describe("prettyFact", () => {
  it("extracts assistant words from a pi agent_end snapshot", () => {
    const raw = `[pi] ${JSON.stringify({
      type: "agent_end",
      messages: [
        { role: "user", content: [{ type: "text", text: "CONCLUDE: stop exploring NOW" }] },
        { role: "assistant", content: [{ type: "text", text: "Found Werkzeug debugger on :8000" }] },
      ],
    })}`;
    expect(prettyFact(raw)).toBe("[pi] Found Werkzeug debugger on :8000");
  });

  it("falls back to the bare actor prefix when only harness directives exist", () => {
    const raw = `[pi] ${JSON.stringify({
      type: "agent_end",
      messages: [{ role: "user", content: [{ type: "text", text: "CONCLUDE: stop" }] }],
    })}`;
    expect(prettyFact(raw)).toBe("[pi]");
  });

  it("leaves clean facts and non-envelope json untouched", () => {
    expect(prettyFact("[pi] plain worker line")).toBe("[pi] plain worker line");
    expect(prettyFact('{"type":"tool_execution_end","id":1}')).toBe('{"type":"tool_execution_end","id":1}');
    expect(prettyFact("")).toBe("");
  });
});
