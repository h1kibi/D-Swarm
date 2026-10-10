"use client";

/**
 * BTW chat state, shared by the sidebar quick-ask drawer and the full-page
 * /run/<id>/btw view. The transcript persists per run in localStorage (operator
 * decision 2026-08-30: closing/reloading must not drop the conversation), and
 * each assistant turn keeps the observer's structured response (evidence refs
 * + uncertainties) so the page can link back into the evidence chain.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/useRun";
import {
  applyBtwAnswerFrame,
  btwErrorBannerText,
  btwFailureTurn,
  observerSourceLabel,
  type BtwAnswerState,
  type ObserverSource,
} from "@/components/btwStream";

export interface BtwTurn {
  role: "user" | "assistant";
  content: string;
  /** observer-verified evidence ids from the structured answer (assistant only). */
  evidenceRefs?: string[];
  /** observer-flagged uncertainties (assistant only). */
  uncertainties?: string[];
  observerSource?: ObserverSource;
  /** Whether the configured lightweight Observer fallback answered. */
  fallbackUsed?: boolean;
}

export { observerSourceLabel } from "@/components/btwStream";

const MAX_TRANSCRIPT_CHARS = 60000;
const STORAGE_PREFIX = "dswarm.btw.";
const STORAGE_LIMIT = 200_000;

const storageKey = (runId: string) => STORAGE_PREFIX + runId;

export function loadBtwTurns(runId: string): BtwTurn[] {
  if (typeof window === "undefined" || !runId) return [];
  try {
    const raw = window.localStorage.getItem(storageKey(runId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (t): t is BtwTurn =>
        t && typeof t === "object" &&
        (t.role === "user" || t.role === "assistant") &&
        typeof t.content === "string",
    );
  } catch {
    return [];
  }
}

export function saveBtwTurns(runId: string, turns: BtwTurn[]): void {
  if (typeof window === "undefined" || !runId) return;
  try {
    const serialized = JSON.stringify(turns);
    // persistence is best-effort: an over-quota transcript keeps working in
    // memory, it just will not survive a reload.
    if (serialized.length <= STORAGE_LIMIT) {
      window.localStorage.setItem(storageKey(runId), serialized);
    }
  } catch {
    // localStorage may be blocked; the session still works in memory.
  }
}

export function clearBtwTurns(runId: string): void {
  if (typeof window === "undefined" || !runId) return;
  try {
    window.localStorage.removeItem(storageKey(runId));
  } catch {
    // nothing to do — an in-memory clear still happens via the caller
  }
}

export function btwTranscriptChars(turns: BtwTurn[]): number {
  return turns.reduce((n, t) => n + t.content.length, 0);
}

export function useBtwChat(runId: string) {
  const [turns, setTurns] = useState<BtwTurn[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const [workerStatus, setWorkerStatus] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const requestSeqRef = useRef(0);
  const hydratedRunRef = useRef("");

  // Hydrate once per run (never mix contexts across runs), then persist on
  // every turn change so close/reload/switch keeps the conversation.
  useEffect(() => {
    if (!runId || hydratedRunRef.current === runId) return;
    hydratedRunRef.current = runId;
    setTurns(loadBtwTurns(runId));
    setInput("");
    setError("");
    setWorkerStatus("");
  }, [runId]);

  useEffect(() => {
    if (runId && hydratedRunRef.current === runId) saveBtwTurns(runId, turns);
  }, [runId, turns]);

  const send = useCallback(
    async (question: string) => {
      const q = question.trim();
      if (!q || !runId) return;
      setError("");
      setWorkerStatus("正在整理只读证据…");
      if (abortRef.current) abortRef.current.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      const requestId = ++requestSeqRef.current;
      const transcript = turns.map(({ role, content }) => ({ role, content }));

      setTurns((prev) => [...prev, { role: "user", content: q }]);
      setInput("");
      setStreaming(true);

      const upsertAssistant = (patch: Partial<BtwTurn> & { content: string }) => {
        if (requestSeqRef.current !== requestId) return;
        setTurns((prev) => {
          const next = prev.slice();
          const last = next[next.length - 1];
          if (last && last.role === "assistant") {
            next[next.length - 1] = { ...last, ...patch };
          } else {
            next.push({
              role: "assistant",
              content: patch.content,
              evidenceRefs: patch.evidenceRefs,
              uncertainties: patch.uncertainties,
              observerSource: patch.observerSource,
              fallbackUsed: patch.fallbackUsed,
            });
          }
          return next;
        });
      };

      try {
        const resp = await apiFetch(`/api/runs/${runId}/btw`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, transcript }),
          signal: ctrl.signal,
        });
        if (!resp.ok || !resp.body) {
          const txt = await resp.text().catch(() => "");
          const message = `请求失败 (${resp.status}): ${txt.slice(0, 160)}`;
          setError(message);
          upsertAssistant(btwFailureTurn("http", message));
          return;
        }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        let answerState: BtwAnswerState = { content: "", final: false, error: "" };
        let refs: string[] | undefined;
        let unknowns: string[] | undefined;
        let observerSource: BtwTurn["observerSource"];
        let fallbackUsed: boolean | undefined;

        const consumeFrame = (frame: string) => {
          const m = frame.match(/^data: (.+)$/s);
          if (!m) return;
          let parsed: unknown;
          try {
            parsed = JSON.parse(m[1]);
          } catch {
            return;
          }
          if (!parsed || typeof parsed !== "object") return;
          const data = parsed as Record<string, unknown>;
          if (data.status) setWorkerStatus(String(data.status).slice(0, 160));
          const previous = answerState;
          answerState = applyBtwAnswerFrame(answerState, data);
          if (Array.isArray(data.evidence_refs)) {
            refs = data.evidence_refs.map((r) => String(r)).filter(Boolean);
          }
          if (Array.isArray(data.uncertainties)) {
            unknowns = data.uncertainties.map((u) => String(u)).filter(Boolean);
          }
          if (data.observer_source === "default" || data.observer_source === "fallback" || data.observer_source === "unavailable") {
            observerSource = data.observer_source;
          }
          if (typeof data.fallback_used === "boolean") {
            fallbackUsed = data.fallback_used;
          }
          if (answerState.content !== previous.content || observerSource !== undefined || fallbackUsed !== undefined) {
            upsertAssistant({ content: answerState.content, evidenceRefs: refs, uncertainties: unknowns, observerSource, fallbackUsed });
          }
          if (answerState.error) {
            setError(btwErrorBannerText(answerState.content, answerState.error));
            setWorkerStatus("");
          }
          if (data.done) setWorkerStatus("");
        };

        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const frames = buf.split(/\r?\n\r?\n/);
          buf = frames.pop() || "";
          for (const frame of frames) consumeFrame(frame);
        }
        if (buf.trim()) consumeFrame(buf);
        if (!answerState.final && !answerState.content.trim()) {
          const failure = btwFailureTurn("empty");
          setError(failure.content);
          upsertAssistant(failure);
        }
      } catch (e: unknown) {
        if (e && typeof e === "object" && "name" in e && e.name === "AbortError") {
          // silent — operator closed / re-asked
        } else {
          const message = String(e instanceof Error ? e.message : e).slice(0, 300);
          setError(message);
          upsertAssistant(btwFailureTurn("transport", message));
        }
      } finally {
        if (requestSeqRef.current === requestId) {
          setStreaming(false);
          setWorkerStatus("");
        }
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    },
    [runId, turns],
  );

  const clear = useCallback(() => {
    if (abortRef.current) abortRef.current.abort();
    clearBtwTurns(runId);
    setTurns([]);
    setError("");
    setWorkerStatus("");
    setInput("");
  }, [runId]);

  const abort = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
      setStreaming(false);
    }
  }, []);

  return {
    turns, input, setInput, streaming, error, workerStatus,
    send, clear, abort,
    transcriptChars: btwTranscriptChars(turns),
    maxChars: MAX_TRANSCRIPT_CHARS,
  };
}
