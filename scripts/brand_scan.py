#!/usr/bin/env python3
"""Brand-remnant scanner (docs/07 Phase 0 / Phase 8 guard).

Case-insensitive scan for legacy brand names across the repo, excluding
dependency/build/history dirs. With ``--check`` it enforces an allowlist:
any hit outside an allowlisted file fails the scan (exit 1).

Usage:
    uv run python scripts/brand_scan.py            # human-readable inventory
    uv run python scripts/brand_scan.py --check    # allowlist enforcement
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    ".git", ".venv", "node_modules", ".next", "__pycache__", ".pytest_cache",
    "references", "sessions", ".pi-sessions", "build", "dist", "$out",
    "assets", ".claude", ".agents",
    # Agent session worktrees mirror the whole tree (legacy tokens included);
    # they are git-ignored dev scaffolding, not distribution content.
    ".worktrees", ".zcode",
}

# Legacy brand tokens. Matched case-insensitively as whole words where the
# token is alphanumeric (so "cursor" does not hit the CSS `cursor:` property
# unless scanned for explicitly — brand scan targets muteki only for now).
TOKENS = ["muteki"]

# Allowlist: paths (repo-relative, posix) where the legacy name may survive —
# AGPL legal text, historical design/planning documents kept as records of
# their era, historical eval reports, and this scanner's own legacy token.
ALLOWLIST = {
    "LICENSE",
    "NOTICE",
    "ROADMAP.md",  # historical iteration log
    "docs/archive/01-architecture.md",  # historical design doc
    "docs/archive/02-implementation-plan.md",  # historical design doc
    "docs/archive/03-worker-contract.md",  # historical design doc
    "docs/archive/04-coordination-and-state.md",  # historical design doc
    "docs/archive/05-security-and-eval.md",  # historical design doc
    "docs/archive/06-route-a-plan.md",  # historical planning context
    "docs/07-d-swarm-ui-audit-and-redesign.md",  # the rename plan's own audit
    "docs/08-oss-research-and-kernel-improvements.md",  # research record citing upstream
    "docs/archive/brand-inventory-2026-08-06.txt",  # dated pre-rename inventory artifact
    "eval_nyu/_reports/FINAL_eval_report.md",  # historical eval record
    "eval_nyu/_reports/RESULTS.md",  # historical eval record
    "public_eval/RESULTS.md",  # historical eval record
    "scripts/brand_scan.py",  # self-referential legacy token
    "README.md",  # upstream attribution note
    # Pre-rename worker images still look for /run/muteki/control/token; this
    # constant keeps those images mountable (functional compatibility shim).
    "dswarm/solver/container_exec.py",
    # Legacy localStorage key migration: browsers still hold `muteki.*` keys
    # from before the rebrand; the read-fallback map must keep the old names.
    "apps/web/ui/lib/storage.ts",
    "apps/web/ui/test/storage.test.ts",
}

BINARY_SUFFIXES = {".pyc", ".png", ".ico", ".icns", ".exe", ".dll", ".so"}


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path, rel.as_posix()


def scan(root: Path = REPO):
    """Yield (relpath, lineno, line) for every legacy-brand hit."""
    pat = re.compile("|".join(re.escape(t) for t in TOKENS), re.IGNORECASE)
    for path, rel in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                yield rel, lineno, line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if any hit falls outside the allowlist")
    args = ap.parse_args()

    hits = list(scan())
    by_file: dict[str, int] = {}
    for rel, _, _ in hits:
        by_file[rel] = by_file.get(rel, 0) + 1

    if not args.check:
        print(f"{len(hits)} hits in {len(by_file)} files")
        for rel, count in sorted(by_file.items(), key=lambda kv: -kv[1]):
            marker = " [allowlisted]" if rel in ALLOWLIST else ""
            print(f"  {count:5d}  {rel}{marker}")
        return 0

    violations = [(rel, n, line) for rel, n, line in hits if rel not in ALLOWLIST]
    if violations:
        print(f"brand scan: {len(violations)} hit(s) outside the allowlist:",
              file=sys.stderr)
        for rel, n, line in violations[:50]:
            print(f"  {rel}:{n}: {line.strip()[:120]}", file=sys.stderr)
        return 1
    print(f"brand scan: clean ({len(hits)} allowlisted hit(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
