import type {
  BashToolCallEvent,
  ExtensionAPI,
  ToolCallEvent,
} from "@earendil-works/pi-coding-agent";

// Base extension: cap long blackboard.py invocations so a hung board service
// (stuck socket, connection refused, slow host) can never wedge a whole worker
// turn. The bash tool already supports a per-call `timeout` (SECONDS); we only
// fill it in when the worker did not set one, so an explicit worker timeout
// wins.
//
// Override with DSWARM_BLACKBOARD_TIMEOUT_SECONDS.

const DEFAULT_TIMEOUT_SECONDS = 30;
// The board skill is always invoked as the `blackboard.py` script (bare
// `blackboard.py ...`, `python3 .../blackboard.py ...`). Anchoring on the
// script name avoids capping `cat blackboard-notes.md` / `grep blackboard x`.
const BLACKBOARD_RE = /blackboard\.py/;

function isBashCall(event: ToolCallEvent): event is BashToolCallEvent {
  return event.toolName === "bash";
}

function timeoutFromEnv(): number {
  const raw = Number(process.env.DSWARM_BLACKBOARD_TIMEOUT_SECONDS ?? "");
  return Number.isFinite(raw) && raw > 0 ? Math.round(raw) : DEFAULT_TIMEOUT_SECONDS;
}

export default function ctfBlackboardWatchdog(pi: ExtensionAPI) {
  pi.on("tool_call", (event) => {
    try {
      if (!isBashCall(event)) {
        return;
      }
      if (!BLACKBOARD_RE.test(event.input.command)) {
        return;
      }
      const cap = timeoutFromEnv();
      if (typeof event.input.timeout !== "number" || event.input.timeout > cap) {
        event.input.timeout = cap;
      }
    } catch {
      // A guard must never break the worker loop.
    }
  });
}
