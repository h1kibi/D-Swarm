import type {
  BashToolCallEvent,
  ExtensionAPI,
  ToolCallEvent,
  ToolCallEventResult,
} from "@earendil-works/pi-coding-agent";

// Base extension: keep the shared evidence graph append-only from the worker
// side. Workers persist facts through the dswarm-blackboard skill; raw shell
// writes to the graph DB / winner snapshot are a data-integrity hazard (see
// BTW reports B2/B4). We block the obvious write forms and tell the worker to
// use blackboard.py instead. Read-only commands are never blocked.

const PROTECTED_PATHS = ["shared_graph.db", "winner.json"];
const WRITE_MARKERS = [
  ">",
  ">>",
  "rm ",
  "mv ",
  "cp ",
  "touch ",
  "truncate ",
  "shred ",
  "dd ",
  "tee ",
];

function isBashCall(event: ToolCallEvent): event is BashToolCallEvent {
  return event.toolName === "bash";
}

function hasWriteIntent(command: string): boolean {
  return WRITE_MARKERS.some((marker) => command.includes(marker));
}

export default function ctfProvenanceGuard(pi: ExtensionAPI) {
  pi.on("tool_call", (event): ToolCallEventResult | void => {
    try {
      if (!isBashCall(event)) {
        return;
      }
      const command = event.input.command;
      if (!hasWriteIntent(command)) {
        return;
      }
      for (const path of PROTECTED_PATHS) {
        if (command.includes(path)) {
          return {
            block: true,
            reason:
              `[provenance-guard] blocked direct write to shared graph path '${path}'. ` +
              "Persist findings through blackboard.py (DSWARM_BLACKBOARD_* env), never by " +
              "editing shared_graph.db / winner.json directly.",
          };
        }
      }
    } catch {
      // A guard must never break the worker loop.
    }
  });
}
