import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
// Per-worker OpenAI-compatible provider. Every pi process receives its own
// environment, so no provider object or credential is shared between Workers.
import fs from "node:fs";

function readSecret(name: string): string | undefined {
  const direct = process.env[name];
  if (direct && direct.trim()) return direct.trim();
  const file = process.env[`${name}_FILE`];
  if (!file || !file.trim()) return undefined;
  try {
    const value = fs.readFileSync(file.trim(), "utf8").trim();
    return value || undefined;
  } catch {
    return undefined;
  }
}

export default function registerDswarmWorker(pi: ExtensionAPI) {
  const baseUrl = (process.env.DSWARM_WORKER_BASE_URL ?? process.env.OPENAI_BASE_URL ?? "").trim();
  const apiKey = readSecret("DSWARM_WORKER_API_KEY") ?? readSecret("OPENAI_API_KEY");
  if (!baseUrl) return;

  const wire = (process.env.DSWARM_WORKER_WIRE_API ?? "auto").trim().toLowerCase();
  const api = wire === "openai-responses" || wire === "responses"
    ? "openai-responses" : "openai-completions";
  const authMode = (process.env.DSWARM_WORKER_AUTH_MODE ?? "bearer").trim().toLowerCase();
  const authHeader = process.env.DSWARM_WORKER_AUTH_HEADER ?? "Authorization";
  const authPrefix = process.env.DSWARM_WORKER_AUTH_PREFIX ?? "Bearer";
  const headers: Record<string, string> = {};
  let useAuthHeader = false;
  if (apiKey && authMode === "bearer") {
    useAuthHeader = true;
  } else if (apiKey && authMode !== "none" && authMode !== "disabled") {
    headers[authMode === "x-api-key" ? "x-api-key" : authHeader] =
      authPrefix ? `${authPrefix} ${apiKey}` : apiKey;
  }

  const modelId = (process.env.DSWARM_WORKER_MODEL ?? "").trim() || "default";
  pi.registerProvider("dswarm-worker", {
    name: "D-Swarm Worker Endpoint",
    baseUrl,
    apiKey: useAuthHeader ? apiKey : undefined,
    authHeader: useAuthHeader,
    headers,
    api,
    models: [{
      id: modelId,
      name: modelId,
      reasoning: true,
      thinkingLevelMap: {
        minimal: null,
        low: null,
        medium: null,
        high: "high",
        max: "max",
      },
      input: ["text"],
      contextWindow: 1000000,
      maxTokens: 384000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }],
  });
}
