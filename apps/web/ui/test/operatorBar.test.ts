import { describe, expect, it } from "vitest";
import { STRINGS } from "../lib/strings";

describe("Operator command bar strings & actions", () => {
  it("defines prompt, stop, resume, and review actions with zh/en text", () => {
    expect(STRINGS["op.action.prompt"]?.zh).toBe("提示");
    expect(STRINGS["op.action.prompt"]?.en).toBe("Prompt");
    expect(STRINGS["op.action.stop"]?.zh).toBe("停止");
    expect(STRINGS["op.action.stop"]?.en).toBe("Stop");
    expect(STRINGS["op.action.resume"]?.zh).toBe("恢复");
    expect(STRINGS["op.action.resume"]?.en).toBe("Resume");
    expect(STRINGS["op.action.review"]?.zh).toBe("回顾");
    expect(STRINGS["op.action.review"]?.en).toBe("Review");
  });

  it("clarifies the four operational commands in the scope note", () => {
    const noteZh = STRINGS["op.note"]?.zh || "";
    expect(noteZh).toContain("四类指令");
    expect(noteZh).toContain("提示");
    expect(noteZh).toContain("停止");
    expect(noteZh).toContain("恢复");
    expect(noteZh).toContain("回顾");
  });
});
