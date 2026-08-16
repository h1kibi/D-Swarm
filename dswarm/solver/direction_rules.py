"""Static direction registry used by Reason diagnostics and routing.

The registry deliberately owns only stable direction vocabulary and default profile
IDs. Runtime images, credentials, endpoints, and other deployment facts remain in
the worker-profile/runtime resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


DIRECTION_SOURCES = ("model", "operator", "keyword", "category", "default")

DIRECTION_OVERRIDE_REASONS = (
    "model_direction_empty",
    "model_direction_explicit_auto",
    "model_direction_invalid",
    "keyword_fallback",
    "category_fallback",
    "initial_recon_operator",
)

DIRECTION_RESOLUTIONS = (
    "empty",
    "explicit_auto",
    "explicit_canonical",
    "recognized_alias",
    "invalid",
    "mechanical_fallback",
    "category_fallback",
)

_MAX_RAW_DIRECTION_LENGTH = 40
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class DirectionSpec:
    canonical: str
    profile: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


_DEFAULT_SPECS = (
    DirectionSpec(
        "web", "pi-web",
        aliases=(),
        keywords=("web", "http", "https", "sql", "sqli", "xss", "ssrf", "csrf", "jwt", "cookie", "login"),
    ),
    DirectionSpec(
        "pwn", "pi-pwn",
        keywords=("pwn", "buffer overflow", "bof", "format string", "heap", "rop", "shellcode", "ret2libc", "use after free", "uaf", "binary exploitation"),
    ),
    DirectionSpec(
        "rev", "pi-rev",
        aliases=("reverse",),
        keywords=("reverse engineering", "disassemble", "decompile", "ghidra", "assembly", "crackme", "elf", "firmware"),
    ),
    DirectionSpec(
        "crypto", "pi-crypto",
        keywords=("crypto", "cryptography", "rsa", "aes", "ecc", "elliptic curve", "discrete log", "factor", "cipher"),
    ),
    DirectionSpec(
        "misc", "pi-misc",
        keywords=("misc", "qr", "osint", "programming", "trivia", "pyjail", "unicode"),
    ),
    DirectionSpec(
        "forensics", "pi-forensics",
        keywords=("forensics", "pcap", "packet capture", "memory dump", "disk image", "wireshark", "volatility", "metadata", "steganography"),
    ),
    DirectionSpec(
        "aisec", "pi-aisec",
        aliases=("ai_sec", "ai-security"),
        keywords=("aisec", "ai security", "prompt injection", "llm", "adversarial", "model poisoning", "jailbreak"),
    ),
)


def sanitize_raw_direction(value: Any, *, max_length: int = _MAX_RAW_DIRECTION_LENGTH) -> str:
    """Return bounded, single-line direction text safe for events/UI diagnostics."""
    if value is None:
        return ""
    text = _CONTROL_RE.sub("", str(value)).strip()
    return text[:max(0, int(max_length))]


class DirectionRegistry:
    """Deterministic vocabulary and keyword fallback registry."""

    def __init__(self, specs: tuple[DirectionSpec, ...] = _DEFAULT_SPECS) -> None:
        self._specs = tuple(specs)
        self._by_canonical = {spec.canonical: spec for spec in self._specs}
        self._aliases = {
            alias.lower(): spec.canonical
            for spec in self._specs
            for alias in spec.aliases
        }

    @property
    def directions(self) -> tuple[str, ...]:
        return tuple(spec.canonical for spec in self._specs)

    def spec_for(self, canonical: str) -> DirectionSpec | None:
        return self._by_canonical.get(sanitize_raw_direction(canonical).lower())

    def profile_for(self, canonical: str) -> str:
        spec = self.spec_for(canonical)
        return spec.profile if spec else ""

    def canonicalize(self, raw: Any) -> tuple[str, str]:
        value = sanitize_raw_direction(raw)
        lowered = value.lower()
        if not lowered:
            return "", "empty"
        if lowered in {"auto", "any", "unknown", "unclear"}:
            return "", "explicit_auto"
        if lowered in self._by_canonical:
            return lowered, "explicit_canonical"
        alias = self._aliases.get(lowered)
        if alias:
            return alias, "recognized_alias"
        return "", "invalid"

    def suggest(self, goal: str, brief: str = "") -> tuple[str, str] | None:
        """Choose a direction by deterministic keyword score.

        One match per keyword contributes to the score. Longer matching phrases
        break ties before registry order, making the result stable across runs.
        """
        haystack = f"{goal or ''}\n{brief or ''}".lower()
        ranked: list[tuple[int, int, int, str]] = []
        for order, spec in enumerate(self._specs):
            matched = [keyword for keyword in spec.keywords if self._keyword_match(haystack, keyword)]
            if not matched:
                continue
            ranked.append((len(matched), sum(len(k) for k in matched), -order, spec.canonical))
        if not ranked:
            return None
        ranked.sort(reverse=True)
        return ranked[0][3], "mechanical_fallback"

    @staticmethod
    def _keyword_match(haystack: str, keyword: str) -> bool:
        needle = str(keyword or "").strip().lower()
        if not needle:
            return False
        # Word boundaries prevent `rsa` from matching an unrelated identifier,
        # while still allowing spaces/hyphens inside multi-word phrases.
        pattern = r"(?<![a-z0-9])" + re.escape(needle).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        return re.search(pattern, haystack) is not None


DEFAULT_DIRECTION_REGISTRY = DirectionRegistry()

__all__ = [
    "DIRECTION_RESOLUTIONS",
    "DirectionRegistry",
    "DirectionSpec",
    "DEFAULT_DIRECTION_REGISTRY",
    "sanitize_raw_direction",
]


def normalize_operator_direction(value: Any) -> tuple[str, str]:
    """Normalize an operator CTF direction at the API boundary.

    Returns ``(canonical, resolution)``. Invalid values are represented
    by an empty canonical value and never become scheduler input.
    """
    return DEFAULT_DIRECTION_REGISTRY.canonicalize(value)
