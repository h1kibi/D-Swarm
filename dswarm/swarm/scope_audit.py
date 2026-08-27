"""Post-hoc scope audit — detect out-of-scope host/asset references in provenance corpus.

Reads the `Challenge.scope` whitelist (hosts separated by comma/newline), normalises
each entry via the same `_clean_lane_host` that canonicalize_lane uses, then scans the
provenance corpus for any host reference outside that whitelist.  On a hit it returns
violation dicts that the caller can emit as `EV_REVIEW_FINDING(kind="scope_violation")`
events — the Review-Arbiter folds them into the shared graph, the report builder excludes
the finding from the deliverable, and a HITL prompt alerts the operator.

This module is intentionally pure (no IO, no event emission) — the caller is responsible
for bridging the result into the event bus and shared graph.
"""

from __future__ import annotations

import re
from typing import Optional

from dswarm.swarm.shared_graph import _clean_lane_host


# Broad host-like token heuristic — prefer false positives over missed violations.
_HOST_LIKE_RE = re.compile(
    r"(?:\b(?:https?://|ftps?://)?"
    r"(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?"
    r"|"
    r"(?:https?://|ftps?://)?"
    r"[a-z0-9](?:[a-z0-9.-]{0,200})"
    r"(?:\.[a-z]{2,})"
    r")(?:\:\d{1,5})?"
    r"(?:/|\b|$)",
    re.IGNORECASE,
)

_PRIVATE_IP = re.compile(r"^10\.|^172\.(?:1[6-9]|2\d|3[01])\.|^192\.168\.|^127\.|^0\.|^169\.254\.")
_LINK_LOCAL = re.compile(r"^fe80:|^ff00:|^::1$", re.IGNORECASE)


def parse_scope(scope_text: str) -> list[str]:
    """Parse the `Challenge.scope` whitelist into a list of canonical host names.

    Entries are separated by comma, semicolon, or newline.  Each is normalised
    via `_clean_lane_host`; entries that fail host normalisation are silently dropped.
    """
    if not scope_text or not scope_text.strip():
        return []
    raw: list[str] = re.split(r"[\n,;]+", scope_text.strip())
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        e = entry.strip()
        if not e:
            continue
        host, conf, _reason = _clean_lane_host(e)
        if not host or conf <= 0.0:
            continue
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


def _extract_host_tokens(text: str) -> list[str]:
    """Pull candidate host tokens from raw text using the broad heuristic."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _HOST_LIKE_RE.finditer(text):
        raw = m.group(0).strip().rstrip("/.:")
        if not raw:
            continue
        canonical, conf, _reason = _clean_lane_host(raw)
        if not canonical or conf <= 0.0:
            continue
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


def _is_private_or_link_local(host: str) -> bool:
    """True if the host is a private/reserved address that is always in-scope."""
    return bool(_PRIVATE_IP.match(host) or _LINK_LOCAL.match(host))


def scope_violations(
    scope_text: str,
    corpus: str,
) -> list[dict]:
    """Scan *corpus* for host references outside the *scope_text* whitelist.

    Returns a list of violation dicts, each with:
      - ``host``: the canonical hostname / IP that was referenced
      - ``context``: up to 80 characters of surrounding context (first occurrence)

    Private/reserved addresses (10.x, 172.16-31.x, 192.168.x, 127.x, ::1,
    link-local) are **always** considered in-scope and never flagged.
    No scope defined = everything is out-of-scope by default (all hosts flagged).
    """
    if not corpus or not corpus.strip():
        return []
    whitelist = parse_scope(scope_text)
    whitelist_set: set[str] = set(whitelist)
    violations: list[dict] = []
    seen_hosts: set[str] = set()

    candidates = _extract_host_tokens(corpus)
    for host in candidates:
        if host in seen_hosts:
            continue
        if _is_private_or_link_local(host):
            continue
        if host in whitelist_set:
            continue
        seen_hosts.add(host)
        # Find the first occurrence in the raw corpus for context.
        idx = corpus.lower().find(host.lower())
        start = max(0, idx - 40)
        end = min(len(corpus), idx + len(host) + 40)
        context = corpus[start:end].replace("\n", " ").strip()
        violations.append({
            "host": host,
            "context": context[:160],
        })
    return violations


def format_violation_finding(violation: dict) -> dict:
    """Build a payload dict for a single scope violation REVIEW_FINDING.

    The caller is responsible for calling ``add_review_finding`` on the
    shared graph with this payload expanded as keyword arguments.
    """
    return {
        "kind": "scope_violation",
        "severity": "high",
        "summary": (
            f"Out-of-scope host referenced: {violation['host']} "
            f"(context: {violation['context'][:100]})"
        ),
        "recommended_actions": [
            "exclude_from_report",
            "notify_operator",
        ],
    }
