export type LaunchChallengePayloadInput = {
  name: string;
  category: string;
  direction: string;
  target: string;
  description: string;
  hints: string;
};

/** Build the CTF launch body without applying truthiness-based field merging. */
export function buildLaunchChallengePayload({
  name,
  category,
  direction,
  target,
  description,
  hints,
}: LaunchChallengePayloadInput): Record<string, any> {
  const trimmedHints = hints.trim();
  const desc = trimmedHints
    ? `${description.trim()}\n\nHints:\n${trimmedHints}`
    : description.trim();

  return {
    kind: "swarm",
    challenge: {
      name: name.trim() || "target",
      category,
      direction: direction === "auto" ? "" : direction,
      description: desc,
      target: target.trim() || undefined,
    },
  };
}
