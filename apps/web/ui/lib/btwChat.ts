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
  type BtwAnswerState,
} from "@/components/btwStream";

export interface BtwTurn {
  role: "user" | "assistant";
  content: string;
  /** observer-verified evidence ids from the structured answer (assistant only). */
  evidenceRefs?: string[];
  /** observer-flagged uncertainties (assistant only). */
  uncertainties?: string[];
}

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

      // Do not create an empty assistant bubble up front. A failed/empty request
      // must become an explicit answer, never a blank turn that looks like no reply.
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
            next.push({ role: "assistant", content: patch.content, evidenceRefs: patch.evidenceRefs, uncertainties: patch.uncertainties });
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
          upsertAssistant({ content: `观察员暂时无法回答：${message}` });
          return;
        }
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        let answerState: BtwAnswerState = { content: "", final: false, error: "" };
        let refs: string[] | undefined;
        let unknowns: string[] | undefined;

        const consumeFrame = (frame: string) => {
          const m = frame.match(/^data: (.+)$/s);
          if (!m) return;
          let obj: any;
          try {
            obj = JSON.parse(m[1]);
          } catch {
            return;
          }
          if (obj.status) setWorkerStatus(String(obj.status).slice(0, 160));
          // `final` is authoritative. Any provisional delta is replaced, so
          // lifecycle echoes or a late complete message cannot duplicate text.
          const previous = answerState;
          answerState = applyBtwAnswerFrame(answerState, obj);
          if (Array.isArray(obj.evidence_refs)) {
            refs = obj.evidence_refs.map((r: unknown) => String(r)).filter(Boolean);
          }
          if (Array.isArray(obj.uncertainties)) {
            unknowns = obj.uncertainties.map((u: unknown) => String(u)).filter(Boolean);
          }
          if (answerState.content !== previous.content) {
            upsertAssistant({ content: answerState.content, evidenceRefs: refs, uncertainties: unknowns });
          }
          if (answerState.error) {
            // An error frame is also promoted into the assistant bubble by the
            // state reducer. Only show a separate banner when it contains
            // additional information; otherwise the same message appears twice.
            setError(btwErrorBannerText(answerState.content, answerState.error));
            setWorkerStatus("");
          }
          if (obj.done) setWorkerStatus("");
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
          const message = "观察员未返回任何回答；没有启动深度 worker。";
          setError(message);
          upsertAssistant({ content: message });
        }
      } catch (e: any) {
        if (e?.name === "AbortError") {
          // silent — operator closed / re-asked
        } else {
          const message = String(e?.message || e).slice(0, 300);
          setError(message);
          upsertAssistant({ content: `观察员暂时无法回答：${message}` });
        }
      } finally {
        if (requestSeqRef.current === requestId) {
          setStreaming(false);
          setWorkerStatus("");
        }
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    },
    [runId, streaming, turns],
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
