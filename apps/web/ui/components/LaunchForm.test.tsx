import { describe, expect, it } from "vitest";
import { buildLaunchChallengePayload } from "./launchPayload";

describe("LaunchForm direction payload", () => {
  it("sends explicit auto as an empty direction while preserving reverse category", () => {
    const body = buildLaunchChallengePayload({
      name: "target",
      category: "reverse",
      direction: "auto",
      target: "http://target",
      description: "Solve it",
      hints: "",
    });

    expect(body.challenge).toMatchObject({
      category: "reverse",
      direction: "",
    });
  });

  it("sends a selected canonical direction", () => {
    const body = buildLaunchChallengePayload({
      name: "target",
      category: "reverse",
      direction: "aisec",
      target: "",
      description: "Solve it",
      hints: "hint",
    });

    expect(body.challenge).toMatchObject({
      category: "reverse",
      direction: "aisec",
      description: "Solve it\n\nHints:\nhint",
      target: undefined,
    });
  });
});
