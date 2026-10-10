import { describe, expect, it } from "vitest";
import { bindingEndpoint, bindingSummary, workerProfileLabel } from "../lib/runBinding";
import type { RunBindingSnapshot } from "../lib/useRun";

const snapshot: RunBindingSnapshot = {
  version: 1,
  worker_profile: { id: "pi-web", label: "Web analyst" },
  roles: {
    reason: { provider_ref: "deepseek", model: "deepseek-reasoner" },
    worker: { provider_ref: "openai-relay", model: "gpt-5-mini", base_url: "https://relay.example.test/v1?token=must-not-render" },
    review: {},
  },
  review_policy: { enabled: true },
};

describe("run binding projection", () => {
  it("summarizes role provider/model references without requiring credentials", () => {
    expect(bindingSummary(snapshot, "reason")).toBe("deepseek / deepseek-reasoner");
    expect(bindingSummary(snapshot, "titler")).toBe("binding unavailable");
    expect(workerProfileLabel(snapshot)).toBe("Web analyst");
  });

  it("keeps planner snapshots readable as the legacy alias for Reason", () => {
    expect(bindingSummary({ roles: { planner: { provider_ref: "legacy", model: "planner-v1" } } }, "reason")).toBe("legacy / planner-v1");
  });

  it("reduces an endpoint to its origin and never exposes query credentials", () => {
    expect(bindingEndpoint("https://relay.example.test/v1?token=must-not-render")).toBe("https://relay.example.test");
    expect(bindingEndpoint("not-a-url")).toBe("custom endpoint");
  });
});