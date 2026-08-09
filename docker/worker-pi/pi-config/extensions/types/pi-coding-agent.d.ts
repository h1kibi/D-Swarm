/**
 * Minimal ambient type surface for pi 0.84.1 extension type-checking.
 *
 * This mirrors the subset of
 * `@earendil-works/pi-coding-agent/dist/core/extensions/types.d.ts` that
 * `docker/worker-pi/pi-config/extensions/*.ts` actually uses. It exists so
 * `tsc --noEmit` works offline (the worker image has no node_modules for the
 * host-side check). When bumping the locked pi version, regenerate/verify this
 * file against the installed package's real declarations.
 */
declare module "@earendil-works/pi-coding-agent" {
  // ------------------------------------------------------------------ content
  export interface TextContent {
    type: "text";
    text: string;
    textSignature?: string;
  }
  export interface ImageContent {
    type: "image";
    image: unknown;
  }

  // -------------------------------------------------------------- tool types
  export type ToolExecutionMode = "sequential" | "parallel";

  export interface AgentToolResult<TDetails = unknown> {
    content: (TextContent | ImageContent)[];
    details: TDetails;
    usage?: unknown;
    addedToolNames?: string[];
    terminate?: boolean;
  }

  export interface BashToolInput {
    command: string;
    timeout?: number;
  }

  export interface ToolCallEventBase {
    type: "tool_call";
    toolCallId: string;
  }
  export interface BashToolCallEvent extends ToolCallEventBase {
    toolName: "bash";
    input: BashToolInput;
  }
  export interface CustomToolCallEvent extends ToolCallEventBase {
    toolName: string;
    input: Record<string, unknown>;
  }
  export type ToolCallEvent = BashToolCallEvent | CustomToolCallEvent;

  export interface ToolCallEventResult {
    block?: boolean;
    reason?: string;
    terminate?: boolean;
  }

  export interface ToolResultEventBase {
    type: "tool_result";
    toolCallId: string;
    input: Record<string, unknown>;
    content: (TextContent | ImageContent)[];
    isError: boolean;
  }
  export interface BashToolResultEvent extends ToolResultEventBase {
    toolName: "bash";
    details: unknown;
  }
  export interface CustomToolResultEvent extends ToolResultEventBase {
    toolName: string;
    details: unknown;
  }
  export type ToolResultEvent = BashToolResultEvent | CustomToolResultEvent;

  export interface ToolResultEventResult {
    content?: (TextContent | ImageContent)[];
    details?: unknown;
    isError?: boolean;
    usage?: unknown;
  }

  // -------------------------------------------------------- lifecycle events
  export interface BeforeAgentStartEvent {
    type: "before_agent_start";
    prompt: string;
    images?: ImageContent[];
    systemPrompt: string;
    systemPromptOptions: unknown;
  }
  export interface BeforeAgentStartEventResult {
    message?: unknown;
    systemPrompt?: string;
  }

  export interface SessionStartEvent {
    type: "session_start";
    reason: "startup" | "reload" | "new" | "resume" | "fork";
    previousSessionFile?: string;
  }

  export interface SessionCompactEvent {
    type: "session_compact";
    compactionEntry: unknown;
    fromExtension: boolean;
    reason: "manual" | "threshold" | "overflow";
    willRetry: boolean;
  }

  export interface ResourcesDiscoverEvent {
    type: "resources_discover";
    cwd: string;
    reason: "startup" | "reload";
  }
  export interface ResourcesDiscoverResult {
    skillPaths?: string[];
    promptPaths?: string[];
    themePaths?: string[];
  }

  // ------------------------------------------------------------- extensions
  export interface ExtensionContext {
    ui: unknown;
    mode: "tui" | "rpc" | "json" | "print";
    hasUI: boolean;
    cwd: string;
    sessionManager: unknown;
    modelRegistry: unknown;
    model: unknown;
    thinkingLevel?: unknown;
    isIdle(): boolean;
    isProjectTrusted(): boolean;
    signal: unknown;
    abort(): void;
    shutdown(): void;
    getContextUsage(): unknown;
    compact(options?: unknown): void;
    getSystemPrompt(): string;
  }

  export interface ToolDefinition<TParams = unknown, TDetails = unknown, TState = any> {
    name: string;
    label: string;
    description: string;
    promptSnippet?: string;
    promptGuidelines?: string[];
    parameters: TParams;
    renderShell?: "default" | "self";
    executionMode?: ToolExecutionMode;
    execute(
      toolCallId: string,
      params: any,
      signal: unknown,
      onUpdate: unknown,
      ctx: ExtensionContext,
    ): Promise<AgentToolResult<TDetails>>;
    renderCall?: unknown;
    renderResult?: unknown;
  }

  // ------------------------------------------------------------- providers
  export interface ProviderModelConfig {
    id: string;
    name: string;
    reasoning: boolean;
    input: ("text" | "image")[];
    cost: { input: number; output: number; cacheRead: number; cacheWrite: number };
    contextWindow: number;
    maxTokens: number;
    headers?: Record<string, string>;
    compat?: Record<string, unknown>;
    thinkingLevelMap?: Record<string, unknown>;
  }
  export interface ProviderConfig {
    name?: string;
    baseUrl?: string;
    apiKey?: string;
    api?: string;
    authHeader?: boolean;
    headers?: Record<string, string>;
    models?: ProviderModelConfig[];
    refreshModels?: unknown;
    oauth?: unknown;
  }

  // --------------------------------------------------------------- handlers
  export type ExtensionHandler<E, R = void> = (
    event: E,
    ctx: ExtensionContext,
  ) => Promise<R | void> | R | void;

  // -------------------------------------------------------------------- API
  export interface ExtensionAPI {
    on(event: "resources_discover", handler: ExtensionHandler<ResourcesDiscoverEvent, ResourcesDiscoverResult>): void;
    on(event: "session_start", handler: ExtensionHandler<SessionStartEvent>): void;
    on(event: "session_compact", handler: ExtensionHandler<SessionCompactEvent>): void;
    on(event: "before_agent_start", handler: ExtensionHandler<BeforeAgentStartEvent, BeforeAgentStartEventResult>): void;
    on(event: "tool_call", handler: ExtensionHandler<ToolCallEvent, ToolCallEventResult>): void;
    on(event: "tool_result", handler: ExtensionHandler<ToolResultEvent, ToolResultEventResult>): void;

    registerTool<TParams = unknown, TDetails = unknown>(tool: ToolDefinition<TParams, TDetails>): void;
    registerCommand(name: string, options: Record<string, unknown>): void;
    registerShortcut(
      shortcut: string,
      options: { description?: string; handler: (ctx: ExtensionContext) => Promise<void> | void },
    ): void;
    registerFlag(name: string, options: { description?: string; type: "boolean" | "string"; default?: boolean | string }): void;
    getFlag(name: string): boolean | string | undefined;
    registerMessageRenderer(customType: string, renderer: unknown): void;
    sendMessage(message: unknown, options?: Record<string, unknown>): void;
    sendUserMessage(content: unknown, options?: Record<string, unknown>): void;
    appendEntry(customType: string, data?: unknown): void;
    setSessionName(name: string): void;
    getSessionName(): string | undefined;
    setLabel(entryId: string, label: string | undefined): void;
    exec(command: string, args: string[], options?: unknown): Promise<unknown>;
    getActiveTools(): string[];
    getAllTools(): unknown[];
    setActiveTools(toolNames: string[]): void;
    setModel(model: unknown): Promise<boolean>;
    getThinkingLevel(): unknown;
    setThinkingLevel(level: unknown): void;
    registerProvider(provider: ProviderConfig): void;
    registerProvider(name: string, config: ProviderConfig): void;
    unregisterProvider(name: string): void;
    events: unknown;
  }
}
