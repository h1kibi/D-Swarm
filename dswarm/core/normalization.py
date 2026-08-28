"""Pure normalization primitives shared across kernel and frontends.

This module intentionally has no solver, web, or graph imports.  Domain modules
own their vocabulary and policy; this leaf owns only deterministic text/key
normalization so callers cannot quietly grow subtly different copies.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse
from typing import Any


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FACT_ENGINE_PREFIX_RE = re.compile(r"^\[[a-z0-9 _.-]{1,40}\]\s*", re.IGNORECASE)
_ROUTE_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "for", "on", "in", "at", "and",
    "or", "with", "via", "try", "test", "probe", "inspect", "attack",
    "exploit", "route", "path", "endpoint", "issue",
})
_ROUTE_ALIAS = (
    (re.compile(r"\bsql\s+injection\b|\bunion\s+(?:select\s+)?(?:payload|sqli)\b", re.I), "sqli"),
    (re.compile(r"\bcross\s+site\s+scripting\b|\bxss\b", re.I), "xss"),
    (re.compile(r"\bserver\s+side\s+request\s+forgery\b|\bssrf\b", re.I), "ssrf"),
    (re.compile(r"\bserver\s+side\s+template\s+injection\b|\bssti\b", re.I), "ssti"),
    (re.compile(r"\bpath\s+traversal\b|\bdirectory\s+traversal\b", re.I), "traversal"),
    (re.compile(r"\bfile\s+upload\b|\bupload\b", re.I), "upload"),
    (re.compile(r"\bjson\s+web\s+token\b|\bjwts?\b", re.I), "jwt"),
    (re.compile(r"\bcommand\s+injection\b|\bcmdi\b", re.I), "cmdi"),
)
LANE_RISK_CLASSES = frozenset({
    "destructive",
    "exclusive_shell",
    "listener_port",
    "relay_service",
    "rate_limited",
})


# Expose immutable route vocabulary for compatibility wrappers without making
# callers duplicate the policy tables.
ROUTE_STOPWORDS = _ROUTE_STOPWORDS
ROUTE_ALIAS = _ROUTE_ALIAS


def clean_text(value: Any, *, default: str = "", max_length: int | None = None) -> str:
    """Coerce a value to stripped text, optionally bounded to ``max_length``."""
    text = default if value is None else str(value)
    text = text.strip()
    if max_length is not None:
        text = text[:max(0, int(max_length))]
    return text


def sanitize_raw_direction(value: Any, *, max_length: int = 40) -> str:
    """Return bounded, single-line direction text safe for events/UI diagnostics."""
    return clean_text(_CONTROL_RE.sub("", "" if value is None else str(value)), max_length=max_length)


def normalize_fact_identity(fact: Any) -> str:
    """Canonicalize fact text for deduplication without changing provenance."""
    text = _FACT_ENGINE_PREFIX_RE.sub("", str(fact or ""))
    return " ".join(text.split()).lower()


def normalize_lane_risk(risk_class: Any) -> str:
    """Canonicalize a lane risk class, defaulting unknown values safely."""
    risk = re.sub(r"[^a-z0-9_]+", "_", clean_text(risk_class or "").lower()).strip("_")
    return risk if risk in LANE_RISK_CLASSES else "destructive"


def normalize_route_hash(route_hash: Any, *, label: Any = "") -> str:
    """Build the stable route identity used by graph lineage and deduplication."""
    source = route_hash or label or ""
    raw = clean_text(source).lower()
    for rx, repl in _ROUTE_ALIAS:
        raw = rx.sub(repl, raw)
    parts = [
        part for part in re.findall(r"[a-z0-9]+", raw)
        if part and part not in _ROUTE_STOPWORDS
    ]
    if not parts:
        digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:10]
        return f"route:{digest}"
    return ":".join(parts[:6])


def normalize_observed_route(route_hash: Any) -> str:
    """Normalize an observed route while preserving an existing route digest."""
    value = clean_text(route_hash).lower()
    if not value:
        return ""
    if re.fullmatch(r"route:[0-9a-f]{10}", value):
        return value
    return normalize_route_hash(value)


def normalize_lane_key(lane_key: Any) -> str:
    """Canonicalize a lane key while preserving its risk/protocol/host shape."""
    raw = clean_text(lane_key or "").lower()
    raw = re.sub(r"\s+", "", raw)
    raw = raw.replace("://", ":")
    raw = re.sub(r"[^a-z0-9_:@.*-]+", "-", raw).strip("-")
    if not raw:
        return ""
    match = re.match(r"^(?P<risk>[a-z0-9_]+):(?P<proto>[a-z0-9_]+):(?P<port>[0-9*]+)@(?P<host>.+)$", raw)
    if not match:
        return raw[:180]
    risk = normalize_lane_risk(match.group("risk"))
    proto = match.group("proto") or "tcp"
    port = match.group("port") or "*"
    host = match.group("host").strip("[]")
    return f"{risk}:{proto}:{port}@{host}"[:180]


def normalize_resource_key(resource_key: Any) -> str:
    """Canonicalize a resource-lock key without interpreting its semantics."""
    raw = clean_text(resource_key or "").lower()
    raw = re.sub(r"\s+", "", raw)
    raw = re.sub(r"[^a-z0-9_:@.*/-]+", "-", raw).strip("-")
    return raw[:180]


def normalize_lane_host(host: Any) -> tuple[str, float, str]:
    """Parse and conservatively canonicalize a lane host without DNS lookups."""
    raw = clean_text(host or "")
    if not raw:
        return "", 0.0, "missing_host"
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    candidate = parsed.hostname or raw
    candidate = candidate.strip().strip("[]").lower()
    candidate = re.sub(r"^https?://", "", candidate)
    candidate = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    candidate = candidate.strip().strip("[]")
    if not candidate:
        bucket = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip("-")[:120]
        return f"unknown-host:{bucket or hashlib.sha1(raw.encode()).hexdigest()[:10]}", 0.30, "host_unparsed"
    if re.fullmatch(r"[0-9a-f:.]+", candidate) and ":" in candidate:
        return candidate, 0.95, ""
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", candidate):
        return candidate, 1.0, ""
    if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", candidate):
        return candidate.rstrip("."), 0.85, "host_not_verified"
    bucket = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip("-")[:120]
    return f"unknown-host:{bucket or hashlib.sha1(raw.encode()).hexdigest()[:10]}", 0.30, "host_unparsed"


__all__ = [
    "LANE_RISK_CLASSES",
    "ROUTE_ALIAS",
    "ROUTE_STOPWORDS",
    "clean_text",
    "normalize_fact_identity",
    "normalize_lane_host",
    "normalize_lane_key",
    "normalize_lane_risk",
    "normalize_observed_route",
    "normalize_resource_key",
    "normalize_route_hash",
    "sanitize_raw_direction",
]
