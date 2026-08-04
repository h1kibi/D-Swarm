import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// ctf-gateway provider (route A, P3): the worker container authenticates to the
// HOST model gateway with its per-run task token; the real upstream key never
// leaves the host process. The gateway is an OpenAI-compatible reverse proxy at
// http://host.docker.internal:9101/v1 (MUTEKI_GATEWAY_URL overrides).
//
// Ported from BTFly's platform-provider.ts (same pattern), with the deepseek
// thinking compat flags added for pi 0.83 + deepseek reasoning models.
export default function registerCtfGateway(pi: ExtensionAPI) {
  const baseUrl = process.env.MUTEKI_GATEWAY_URL ?? "http://host.docker.internal:9101/v1";
  const apiKey = process.env.MUTEKI_TASK_TOKEN;

  // No task token → this is a bare/manual container exec, not a managed worker.
  // Skip registration and let pi fall back to its default providers.
  if (!apiKey) {
    return;
  }

  const modelSpecs = [
    {
      id: "deepseek-v4-flash",
      name: "DeepSeek V4 Flash (gateway)",
      reasoning: true,
    },
    {
      id: "deepseek-v4-pro",
      name: "DeepSeek V4 Pro (gateway)",
      reasoning: true,
    },
  ];

  pi.registerProvider("ctf-gateway", {
    name: "CTF Swarm Model Gateway",
    baseUrl,
    apiKey,
    authHeader: true,
    api: "openai-completions",
    compat: {
      supportsStore: false,
      supportsDeveloperRole: false,
      requiresReasoningContentOnAssistantMessages: true,
      thinkingFormat: "deepseek",
    },
    models: modelSpecs.map((m) => ({
      id: m.id,
      name: m.name,
      reasoning: m.reasoning,
      // DeepSeek accepts only system/user/assistant/tool — never `developer`.
      compat: {
        supportsStore: false,
        supportsDeveloperRole: false,
        requiresReasoningContentOnAssistantMessages: true,
        thinkingFormat: "deepseek",
      },
      input: ["text"],
      contextWindow: 1000000,
      maxTokens: 384000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    })),
  });
}
