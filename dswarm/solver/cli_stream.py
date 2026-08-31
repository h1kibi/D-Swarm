"""Pure CLI stream/text parsing helpers shared by the CliSolver."""

from __future__ import annotations

import re
import json

_BRACE_FLAG = re.compile(r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")


def _clean_flag_token(raw: str) -> str:
    """Turn the raw FOUND_FLAG= tail into the actual flag token. Two shapes:
      - brace flag `xxx{...}`: return the FULL brace structure (spaces allowed
        inside) — fixes both the markdown-`**` pollution (BUG-1) and the
        space-truncation (BUG-2), since we grab exactly `…{…}` and drop any
        trailing prose / `**` / punctuation the worker appended on the same line.
      - bare token (no braces): take the first whitespace-delimited word, stripped
        of wrapping quotes/backticks/markdown — old behavior preserved.
    """
    s = (raw or "").strip().strip("`'\"*_ ").strip()
    m = _BRACE_FLAG.search(s)
    if m:
        return m.group(0)
    tok = s.split()[0] if s.split() else ""
    return tok.strip("`'\"*_.,;:!").strip()


# pi json-mode RPC stream artifacts are protocol envelopes, NOT worker prose.
_STREAM_ENVELOPE_TYPES = frozenset({
    "session", "agent_start", "turn_start", "message_start",
    "message_update", "message_delta", "text_delta",
    "toolcall_delta", "tool_call_delta", "content_update",
    "tool_execution_start", "tool_execution_end", "agent_settled",
})
_STREAM_CONTENT_TYPES = frozenset({"message", "message_end", "turn_end", "agent_end"})


def _json_event_has_prose(ev: dict) -> bool:
    """True if a pi json-mode event carries non-empty assistant text anywhere."""
    msgs = ev.get("messages")
    if isinstance(msgs, list):
        return any(isinstance(m, dict) and _json_event_has_prose(m) for m in msgs)
    msg = ev.get("message")
    if isinstance(msg, dict) and _json_event_has_prose(msg):
        return True
    v = ev.get("text")
    if isinstance(v, str) and v.strip():
        return True
    content = ev.get("content")
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if _json_event_has_prose(item):
                    return True
            elif isinstance(item, str) and item.strip():
                return True
    return False


def _is_stream_delta(line: str) -> bool:
    line = line.strip()
    if not line.startswith("{"):
        return False
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return False
    if not isinstance(ev, dict):
        return False
    t = ev.get("type")
    if not isinstance(t, str):
        return False
    if t in _STREAM_ENVELOPE_TYPES:
        return True
    if t in _STREAM_CONTENT_TYPES:
        return not _json_event_has_prose(ev)
    return True


_VALID_NEED_KINDS = {
    "external_blocker",
    "operator_directive_needed",
    "lane_lock_request",
    "route_dead_end",
    "worker_uncertainty",
}


def _normalize_need_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    return k if k in _VALID_NEED_KINDS else ""


def classify_need_kind(text: str) -> str:
    """Fine-grained HITL classifier, separate from legacy env_down/need_input."""
    low = (text or "").lower()
    if any(k in low for k in (
        "ask operator", "operator decide", "需要 operator", "need a decision from",
    )):
        return "operator_directive_needed"
    if any(k in low for k in (
        "exclusive", "独占", "serialize", "序列化", "another worker",
        "其它 worker", "其他 worker", "same target", "stop hammering",
    )):
        return "lane_lock_request"
    if any(k in low for k in (
        "unreachable", "connection refused", "refused", "timed out", "timeout",
        "expired", "instance", "502", "503", "down", "credential", "凭据",
        "vps", "attachment", "附件", "token", "runtime", "container",
    )):
        return "external_blocker"
    if any(k in low for k in (
        "已知失败", "repeatedly fail", "known dead", "dead end", "dead-end",
        "route dead", "route failed", "no longer viable", "打不通", "走死",
    )):
        return "route_dead_end"
    return "worker_uncertainty"


_LOCKOUT_RE = re.compile(
    r"(?:lock(?:ed|out)?|cooldown|wait|try again|rate.?limit|too many|burn)\D{0,40}?"
    r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE)


def _parse_lockout_seconds(text: str) -> float:
    """Return the LARGEST lockout duration (in seconds) mentioned in `text`."""
    best = 0.0
    for m in _LOCKOUT_RE.finditer(text or ""):
        try:
            n = float(m.group(1))
        except (TypeError, ValueError):
            continue
        unit = (m.group(2) or "s").lower().rstrip("s")
        if unit in ("minute", "min", "m"):
            n *= 60
        elif unit in ("hour", "hr", "h"):
            n *= 3600
        best = max(best, n)
    return best


_VERIFIER_VERDICT_RE = re.compile(
    r"burn-?lock(?:out)?\s*[:\-]|"
    r"\d+\s*burns?\s+in\s+(?:the\s+)?last|"
    r"attempts?\s+(?:left|remaining)|"
    r"too\s+many\s+(?:wrong\s+)?attempts?|"
    r"locked\s+for\s+\d|"
    r"wait\s+(?:for\s+)?(?:the\s+)?cooldown",
    re.IGNORECASE)
_DOC_READ_RE = re.compile(
    r"(?:^|\n)\s*read:\s|"
    r"PROBLEM_verifier|BRIEFING|known_intel|missions?\.json|"
    r"\.md\b|DESIGN_|SOP_",
    re.IGNORECASE)
_VERIFIER_INVOKE_RE = re.compile(
    r"(?:specter-verify|verify-[a-z0-9-]+\.sh|/opt/verify-)", re.IGNORECASE)


def _looks_like_verifier_output(text: str) -> bool:
    """True only when a lock phrase in `text` is the VERIFIER's own verdict."""
    t = text or ""
    if not _VERIFIER_VERDICT_RE.search(t):
        return False
    if not _VERIFIER_INVOKE_RE.search(t):
        return False
    if _DOC_READ_RE.search(t):
        return False
    return True


def _message_prose(msg: dict) -> str:
    """The last non-empty text inside one pi message dict (assistant preferred)."""
    content = msg.get("content")
    texts: list[str] = []
    if isinstance(content, str) and content.strip():
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                v = item.get("text")
                if isinstance(v, str) and v.strip():
                    texts.append(v)
            elif isinstance(item, str) and item.strip():
                texts.append(item)
    elif isinstance(msg.get("text"), str) and msg["text"].strip():
        texts.append(msg["text"])
    if not texts:
        return ""
    # the harness CONCLUDE/user directive is boilerplate, not worker words
    if str(msg.get("role") or "").lower() != "assistant":
        return ""
    return texts[-1].strip()


def extract_closing_prose(text: str, *, limit: int = 200) -> str:
    """The worker's last meaningful line for a closing-summary fact.

    Plain prose lines win (scanned bottom-up). pi json-mode ENVELOPES
    (message_end / agent_end snapshots of the whole conversation) are not
    worker output — a bare `{"type":"agent_end","messages":[...]}` line used
    to become a raw-JSON "fact"; the assistant text inside it is extracted
    instead. Returns "" when nothing assistant-authored exists.
    """
    for line in reversed((text or "").splitlines()):
        s = line.strip()
        if not s:
            continue
        if not s.startswith("{"):
            return s[:limit]
        try:
            ev = json.loads(s)
        except (ValueError, TypeError):
            return s[:limit]
        if not isinstance(ev, dict):
            return s[:limit]
        if ev.get("type") not in _STREAM_CONTENT_TYPES:
            continue
        msgs = ev.get("messages")
        candidates: list[str] = []
        if isinstance(msgs, list):
            for m in reversed(msgs):
                if isinstance(m, dict):
                    prose = _message_prose(m)
                    if prose:
                        candidates.append(prose)
        msg = ev.get("message")
        if isinstance(msg, dict):
            prose = _message_prose(msg)
            if prose:
                candidates.append(prose)
        if candidates:
            return candidates[0][:limit]
    return ""
