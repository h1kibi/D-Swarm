import type {
  AgentToolResult,
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

// Base extension: a tiny local evidence journal for the worker itself.
// Findings are appended as JSONL under the CURRENT cwd (.dswarm/evidence.jsonl)
// so they survive compaction and give the worker structured memory. It never
// touches shared graph state (provenance-guard keeps that boundary).

const KINDS = ["fact", "finding", "deadend", "hint"] as const;

export default function ctfEvidenceNote(pi: ExtensionAPI) {
  pi.registerTool({
    name: "ctf_evidence_note",
    label: "CTF evidence note",
    description:
      "Append one structured evidence note (fact/finding/deadend/hint) to the worker's local JSONL journal under the current workspace. Use it after confirming a fact in real output, after a dead end, and before long sessions.",
    promptSnippet: "Record verified facts, findings and dead ends locally with ctf_evidence_note",
    promptGuidelines: [
      "After confirming a fact in real tool output, record it with kind=fact and the concrete evidence (command/output path).",
      "After ruling a direction out, record kind=deadend so you and the board do not retry it.",
      "Notes are local to this worker; they never modify shared graph state.",
    ],
    parameters: Type.Object({
      kind: Type.Union(KINDS.map((k) => Type.Literal(k))),
      content: Type.String({
        minLength: 1,
        description: "What was found or ruled out, with the concrete evidence path or command.",
      }),
      evidence: Type.Optional(
        Type.String({ description: "Optional verbatim evidence snippet or artifact path." }),
      ),
      tags: Type.Optional(
        Type.Array(
          Type.String({ description: "Optional tags, e.g. target, service, technique." }),
        ),
      ),
    }),
    execute: async (
      _toolCallId,
      params,
      _signal,
      _onUpdate,
      ctx: ExtensionContext,
    ): Promise<AgentToolResult> => {
      const dir = join(ctx.cwd, ".dswarm");
      const file = join(dir, "evidence.jsonl");
      try {
        mkdirSync(dir, { recursive: true });
        const entry = {
          ts: new Date().toISOString(),
          kind: params.kind,
          content: params.content,
          evidence: params.evidence ?? "",
          tags: params.tags ?? [],
        };
        appendFileSync(file, `${JSON.stringify(entry)}\n`, "utf8");
        return {
          content: [{ type: "text", text: `appended evidence note to ${file}` }],
          details: { file, kind: params.kind },
        };
      } catch (err) {
        return {
          content: [
            {
              type: "text",
              text: `failed to write evidence note: ${err instanceof Error ? err.message : String(err)}`,
            },
          ],
          details: { error: true },
        };
      }
    },
  });
}
