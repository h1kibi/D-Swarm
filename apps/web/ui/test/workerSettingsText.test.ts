import { describe, expect, it } from "vitest";
import { workerSettingsIssueMessage, workerSettingsText } from "../lib/workerSettingsText";

describe("workerSettingsText", () => {
  it("provides the Chinese Worker settings title", () => {
    expect(workerSettingsText("zh", "title")).toBe("Worker 配置");
  });

  it("provides the English Worker settings title", () => {
    expect(workerSettingsText("en", "title")).toBe("Worker Configuration");
  });

  it("translates the misc direction as 杂项", () => {
    expect(workerSettingsText("zh", "direction.misc")).toBe("杂项");
    expect(workerSettingsText("en", "direction.misc")).toBe("Misc");
  });

  it("interpolates localized values", () => {
    expect(workerSettingsText("zh", "enabledCount", { count: 5 })).toBe("5/7 已启用");
  });

  it("translates known validation issue codes", () => {
    expect(workerSettingsIssueMessage("zh", "missing_model", "Model is required."))
      .toBe("必须配置模型。");
  });

  it("falls back to the backend issue message for unknown codes", () => {
    expect(workerSettingsIssueMessage("zh", "future_issue", "Backend detail"))
      .toBe("Backend detail");
  });
});
