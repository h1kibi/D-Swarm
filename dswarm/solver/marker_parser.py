from __future__ import annotations

import re
from typing import Optional

from dswarm.solver.cli_stream import _clean_flag_token, _normalize_need_kind
from dswarm.solver.gate import is_placeholder_flag

# Worker output markers are kept in this leaf module so parsing can evolve without
# making the shelled executor own another unrelated domain.
_FLAG_LINE = re.compile(r"FOUND_FLAG=\s*(.+)")
_VERIFIED_FACT_LINE = re.compile(r"VERIFIED_FACT=\s*(.+)")
_FINDING_TYPE_LINE = re.compile(r"FINDING_TYPE=\s*(\S+)")
_FINDING_TARGET_LINE = re.compile(r"FINDING_TARGET=\s*(\S+)")
_FINDING_DATA_LINE = re.compile(r"FINDING_DATA=\s*(.+)")
_BARE_RAW_FLAG_BAD_BODY = re.compile(r"[\s;:()\[\]{}<>]")
_FACT_WITNESS_LINE = re.compile(r"FACT_WITNESS\s*=\s*(.+)", re.IGNORECASE)
_DEADEND_LINE = re.compile(r"DEADEND=\s*(.+)")
_NEED_INPUT_LINE = re.compile(r"NEED_INPUT=\s*(.+)")
_NEED_KIND_LINE = re.compile(r"NEED_KIND\s*=\s*([a-z_]+)", re.IGNORECASE)
_POC_SAVE_LINE = re.compile(r"POC_SAVE=\s*([^|]+)\|([^|]+)\|([^|]+)\|(.*)")
_POC_REPRO_LINE = re.compile(r"POC_REPRO=\s*([^|]+)\|(.*)")
_CLEANUP_LINE = re.compile(r"CLEANUP=\s*(.+)")
_READY_TO_SUBMIT_LINE = re.compile(r"READY_TO_SUBMIT=\s*(.+)")


def is_bare_raw_flag(flag: str) -> bool:
    """Return whether a brace-shaped raw token is clean enough to inspect."""
    m = re.search(r"\{([^}]*)\}", flag or "")
    if not m:
        return False
    body = m.group(1)
    if len(body) < 8 or _BARE_RAW_FLAG_BAD_BODY.search(body):
        return False
    return bool(re.search(r"[A-Za-z0-9]", body))


def extract_flag(text: str) -> Optional[str]:
    """Extract the last non-placeholder FOUND_FLAG marker."""
    cand: Optional[str] = None
    for m in _FLAG_LINE.finditer(text):
        raw = m.group(1).strip()
        if raw.upper().startswith("NONE"):
            continue
        tok = _clean_flag_token(raw)
        if tok and tok != "NONE":
            cand = tok
    if cand is None or is_placeholder_flag(cand):
        return None
    return cand


def extract_flags(text: str) -> list[str]:
    """Extract all distinct non-placeholder FOUND_FLAG markers in order."""
    out: list[str] = []
    for m in _FLAG_LINE.finditer(text):
        raw = m.group(1).strip()
        if raw.upper().startswith("NONE"):
            continue
        tok = _clean_flag_token(raw)
        if not tok or tok == "NONE" or is_placeholder_flag(tok):
            continue
        if tok not in out:
            out.append(tok)
    return out


def extract_structured_facts(text: str) -> tuple[list[str], list[str]]:
    """Parse VERIFIED_FACT= and DEADEND= markers from worker output."""
    facts, deadends = [], []
    for line in (text or "").splitlines():
        m = _VERIFIED_FACT_LINE.match(line.strip())
        if m:
            facts.append(m.group(1).strip())
        m2 = _DEADEND_LINE.match(line.strip())
        if m2:
            deadends.append(m2.group(1).strip())
    return facts, deadends


def extract_structured_findings(text: str) -> list[dict[str, str]]:
    """Parse FINDING_TYPE / FINDING_TARGET / FINDING_DATA triples."""
    out: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (text or "").splitlines():
        s = line.strip()
        m = _FINDING_TYPE_LINE.match(s)
        if m:
            current = {"kind": m.group(1).strip()}
            continue
        if not current:
            continue
        m = _FINDING_TARGET_LINE.match(s)
        if m:
            current["target"] = m.group(1).strip()
            continue
        m = _FINDING_DATA_LINE.match(s)
        if m:
            current["data"] = m.group(1).strip()
            if current.get("kind") and current.get("target"):
                out.append(dict(current))
                current = {}
    return out


def extract_fact_witness(text: str) -> str:
    for line in (text or "").splitlines():
        m = _FACT_WITNESS_LINE.match(line.strip())
        if m:
            return m.group(1).strip()[:500]
    return ""


def fact_witnessed_in_chunk(fact: str, text: str) -> bool:
    """Check that a VERIFIED_FACT has non-marker output in the same chunk."""
    fact = (fact or "").strip()
    if not fact:
        return False
    raw_lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if (_VERIFIED_FACT_LINE.match(s) or _DEADEND_LINE.match(s)
                or _FLAG_LINE.match(s) or _NEED_INPUT_LINE.match(s)
                or _POC_SAVE_LINE.match(s) or _FACT_WITNESS_LINE.match(s)):
            continue
        raw_lines.append(line)
    raw = "\n".join(raw_lines).strip().lower()
    if not raw:
        return False
    fact_l = fact.lower()
    if fact_l in raw:
        return True
    tokens = [
        t for t in re.findall(r"[a-z0-9_./:-]{4,}", fact_l)
        if t not in {"http", "https", "true", "false", "with", "from",
                     "that", "this", "there", "have", "confirmed"}
    ]
    if not tokens:
        return False
    hits = sum(1 for t in dict.fromkeys(tokens) if t in raw)
    needed = max(2, int(len(set(tokens)) * 0.6 + 0.5))
    return hits >= needed


def extract_need_requests(text: str) -> list[tuple[str, str]]:
    """Parse single- or multi-line NEED_INPUT blocks."""
    stop = ("FOUND_FLAG=", "VERIFIED_FACT=", "DEADEND=", "DEAD_END=",
            "POC_SAVE=", "CLEANUP=", "READY_TO_SUBMIT=", "ALL_FLAGS_FOUND=")
    lines = (text or "").splitlines()
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        m = _NEED_INPUT_LINE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        buf = [m.group(1).strip()]
        reported_kind = ""
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                break
            if _NEED_INPUT_LINE.match(nxt) or any(nxt.startswith(s) for s in stop):
                break
            km = _NEED_KIND_LINE.match(nxt)
            if km:
                reported_kind = _normalize_need_kind(km.group(1))
                i += 1
                continue
            buf.append(nxt)
            i += 1
        need = "\n".join(buf).strip()
        if need:
            out.append((need, reported_kind))
    return out


def extract_need_inputs(text: str) -> list[str]:
    return [need for need, _kind in extract_need_requests(text)]


def extract_ready_to_submit(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        m = _READY_TO_SUBMIT_LINE.match(line.strip())
        if m:
            note = m.group(1).strip()
            if note:
                out.append(note)
    return out


def extract_poc_saves(text: str) -> list[tuple[str, str, str, str]]:
    out: list[tuple[str, str, str, str]] = []
    for line in (text or "").splitlines():
        m = _POC_SAVE_LINE.match(line.strip())
        if m:
            out.append(tuple(part.strip() for part in m.groups()))
    return out


def extract_poc_repros(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        m = _POC_REPRO_LINE.match(line.strip())
        if m:
            path_text, indicator = (part.strip() for part in m.groups())
            if path_text:
                out.append((path_text, indicator))
    return out


def extract_cleanup_markers(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        m = _CLEANUP_LINE.match(line.strip())
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out
