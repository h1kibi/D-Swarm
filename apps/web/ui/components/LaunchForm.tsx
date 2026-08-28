"use client";

import { useState } from "react";
import { buildLaunchChallengePayload } from "./launchPayload";

const CATEGORY_OPTIONS = ["web", "crypto", "reverse", "pwn", "forensics", "misc"] as const;
const DIRECTION_OPTIONS = ["auto", "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"] as const;

/**
 * Launch a run 鈥?a "new project" form, scoped to a CTF challenge spec.
 * Two modes: a real swarm (needs DSWARM_DEEPSEEK_API_KEY on the backend) or a
 * keyless mock stream for UI/e2e. The body shape matches drivers.build_driver.
 */
export function LaunchForm({
  onStart,
  disabled,
}: {
  onStart: (body: Record<string, any>) => void;
  disabled?: boolean;
}) {
  const [name, setName] = useState("target");
  const [category, setCategory] = useState<(typeof CATEGORY_OPTIONS)[number]>("web");
  const [direction, setDirection] = useState<(typeof DIRECTION_OPTIONS)[number]>("auto");
  const [target, setTarget] = useState("http://127.0.0.1:8000");
  const [description, setDescription] = useState("Solve the web challenge.");
  const [hints, setHints] = useState("");

  const launch = (kind: "swarm" | "mock") => {
    if (kind === "mock") {
      onStart({ kind: "mock" });
      return;
    }
    onStart(buildLaunchChallengePayload({
      name,
      category,
      direction,
      target,
      description,
      hints,
    }));
  };

  return (
    <div className="launch">
      <div className="launch-grid">
        <label>
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="challenge name" />
        </label>
        <label>
          <span>Category</span>
          <select value={category} onChange={(e) => setCategory(e.target.value as (typeof CATEGORY_OPTIONS)[number])}>
            {CATEGORY_OPTIONS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Direction (optional override)</span>
          <select value={direction} onChange={(e) => setDirection(e.target.value as (typeof DIRECTION_OPTIONS)[number])}>
            {DIRECTION_OPTIONS.map((value) => (
              <option key={value} value={value}>{value === "auto" ? "auto (follow category)" : value}</option>
            ))}
          </select>
        </label>
        <label className="wide">
          <span>Target (origin / URL / host)</span>
          <input value={target} onChange={(e) => setTarget(e.target.value)} placeholder="http://host:port or host" />
        </label>
        <label className="wide">
          <span>Goal / description</span>
          <textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={2}
            placeholder="what does solving look like? what's the objective?" />
        </label>
        <label className="wide">
          <span>Hints (optional, one per line)</span>
          <textarea value={hints} onChange={(e) => setHints(e.target.value)} rows={2}
            placeholder="known facts to seed the swarm with" />
        </label>
      </div>
      <div className="launch-actions">
        <button className="primary" disabled={disabled} onClick={() => launch("swarm")}>鈻?Launch swarm</button>
        <button disabled={disabled} onClick={() => launch("mock")} title="keyless demo stream">Run mock</button>
      </div>
    </div>
  );
}
