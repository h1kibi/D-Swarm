import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Base extension: on every agent turn, make sure the worker sees its run
// metadata and the D-Swarm evidence protocol. This is the extension-side
// counterpart to the host prompt injection; it adds one compact, idempotent
// block (marker check) so repeated turns do not duplicate it.

const MARKER = "## D-Swarm run context (extension)";

function env(name: string): string | undefined {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : undefined;
}

export default function ctfContextInjector(pi: ExtensionAPI) {
  pi.on("before_agent_start", (event) => {
    try {
      if (event.systemPrompt.includes(MARKER)) {
        return;
      }

      const lines: string[] = [MARKER];

      const runId = env("DSWARM_BLACKBOARD_RUN_ID");
      const workerId = env("DSWARM_WORKER_ID");
      const taskKind = env("DSWARM_WORKER_TASK_KIND");
      const intentId = env("DSWARM_INTENT_ID");
      const directionPrompt = env("DSWARM_DIRECTION_PROMPT");
      const boardDb = env("DSWARM_BLACKBOARD_DB");

      const meta: string[] = [];
      if (runId) meta.push(`run: ${runId}`);
      if (workerId) meta.push(`worker: ${workerId}`);
      if (taskKind) meta.push(`category: ${taskKind}`);
      if (intentId) meta.push(`intent: ${intentId}`);
      if (directionPrompt) meta.push(`direction prompt: ${directionPrompt}`);
      if (meta.length > 0) {
        lines.push(meta.join("  |  "));
      }
      if (boardDb) {
        lines.push(`shared graph db (read via blackboard.py only): ${boardDb}`);
      }

      lines.push(
        "Evidence protocol: follow the FOUND_FLAG= / VERIFIED_FACT= / DEADEND= contract already in " +
          "this system prompt; never write to shared_graph.db / winner.json directly (use blackboard.py).",
      );

      return { systemPrompt: `${event.systemPrompt}\n\n${lines.join("\n")}` };
    } catch {
      // Never break the agent loop.
    }
  });
}
