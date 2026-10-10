"""eval_nyu/report.py — regression report generation (route A, P5).

Consumes result JSONLs (one row per (challenge, engine)) and emits a markdown
report: per-engine solve rate, per-category breakdown, timing/cost, and — when
a `--baseline` file is given — a per-challenge pi-vs-baseline comparison.

Also ingests the historical baseline from the June-2026 NYU report's markdown
tables (public_eval/RESULTS.md, rows marked `measured`), so pi runs can be
compared against the recorded claude/codex/cursor winners.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional

_ENGINE_LABEL = {"pi": "pi (DeepSeek via gateway)", "claude": "claude", "codex": "codex",
                 "cursor": "cursor"}


def load_results(path: "str | Path") -> "list[dict[str, Any]]":
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _solve_ok(row: dict) -> bool:
    return bool(row.get("solved")) or bool(row.get("matched"))


def _stats(rows: "list[dict]") -> dict:
    solved = [r for r in rows if _solve_ok(r)]
    elapsed = [float(r.get("elapsed_s") or 0.0) for r in solved if r.get("elapsed_s")]
    return {
        "total": len(rows),
        "solved": len(solved),
        "rate": (len(solved) / len(rows)) if rows else 0.0,
        "median_s": round(statistics.median(elapsed), 1) if elapsed else None,
        "cost": round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 4),
        "tokens": sum(int(r.get("tokens") or 0) for r in rows),
    }


def _by_category(rows: "list[dict]") -> "list[tuple[str, dict]]":
    cats: dict[str, list] = {}
    for r in rows:
        cats.setdefault(str(r.get("category") or "?"), []).append(r)
    return sorted((c, _stats(v)) for c, v in cats.items())


def render(results: "list[dict]", *, baseline: "Optional[list[dict]]" = None,
           title: str = "Eval regression report") -> str:
    lines: list[str] = [f"# {title}", ""]

    # engine-level table
    by_engine: dict[str, list] = {}
    for r in results:
        by_engine.setdefault(str(r.get("engine") or "?"), []).append(r)
    lines.append("## Per-engine")
    lines.append("| engine | solved / total | solve rate | median time (s) | cost ($) | tokens |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for eng in sorted(by_engine):
        s = _stats(by_engine[eng])
        med = s["median_s"] if s["median_s"] is not None else "—"
        lines.append(f"| {eng} | {s['solved']} / {s['total']} | {s['rate']:.1%} | "
                     f"{med} | {s['cost']:.4f} | {s['tokens']} |")
    lines.append("")

    # per-category
    lines.append("## By category")
    lines.append("| category | solved / total | rate |")
    lines.append("| --- | --- | --- |")
    for cat, s in _by_category(results):
        lines.append(f"| {cat} | {s['solved']} / {s['total']} | {s['rate']:.1%} |")
    lines.append("")

    # baseline comparison
    if baseline:
        base_by_id = {str(b.get("challenge_id")): b for b in baseline}
        lines.append("## vs baseline (2026-06-11 NYU measured)")
        lines.append("| challenge | category | pi | baseline winner | pi vs baseline |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in sorted(results, key=lambda x: str(x.get("challenge_id"))):
            b = base_by_id.get(str(r.get("challenge_id")))
            if b is None:
                continue
            pi_ok = _solve_ok(r)
            base_win = str(b.get("engine") or "?")
            cmp = "PI WINS" if pi_ok and not _solve_ok(b) else (
                "pi match" if pi_ok and _solve_ok(b) else (
                    "pi miss (baseline solved)" if base_win != "?" and _solve_ok(b) else "both miss"))
            lines.append(f"| {r.get('challenge_id')} | {r.get('category') or b.get('category') or ''} | "
                         f"{'✓' if pi_ok else '✗'} | {base_win} | {cmp} |")
        lines.append("")

    # per-challenge detail
    lines.append("## Per-challenge")
    lines.append("| challenge | category | engine | solved | matched | time (s) | cost ($) | detail |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(results, key=lambda x: (str(x.get("challenge_id")), str(x.get("engine")))):
        lines.append(f"| {r.get('challenge_id')} | {r.get('category') or ''} | {r.get('engine')} | "
                     f"{'✓' if _solve_ok(r) else '✗'} | {r.get('matched') or ''} | "
                     f"{r.get('elapsed_s') or '—'} | {r.get('cost_usd') or 0} | "
                     f"{(r.get('detail') or r.get('note') or '')[:80]} |")
    return "\n".join(lines) + "\n"


# ── baseline ingestion from the June-2026 report markdown ────────────────────

def ingest_baseline_md(path: "str | Path") -> "list[dict[str, Any]]":
    """Parse the measured rows of public_eval/RESULTS.md (one markdown table per
    category; rows ending in `measured` carry a real engine/time/tokens).
    Accepts a file path OR the markdown text itself."""
    if isinstance(path, str) and ("\n" in path or path.lstrip().startswith("|")):
        text = path
    else:
        text = Path(path).read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    current_category = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            head = line[4:].strip()
            current_category = head.split()[0].lower() if head else ""
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip("|").split("|")]
        if len(cols) < 8 or cols[0] in ("Challenge", "---"):
            continue
        name, year, difficulty, engine, model, time_s, tokens, status = cols[:8]
        if status != "measured" or engine in ("", "—"):
            continue
        rows.append({
            "challenge_id": name,
            "category": current_category.lower(),
            "difficulty": difficulty,
            "engine": engine,
            "model": model,
            "time_s": time_s,
            "tokens": tokens,
            "solved": True,
        })
    return rows


def save_baseline(md_path: "str | Path", out_path: "str | Path") -> int:
    rows = ingest_baseline_md(md_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)
