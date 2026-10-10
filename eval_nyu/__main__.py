"""eval_nyu CLI — python -m eval_nyu.

Examples:
  # list a dataset without running anything
  python -m eval_nyu --dataset eval_nyu/datasets/local-cdut.json --list

  # run the pi engine on 3 challenges, 300s budget each, then render the report
  python -m eval_nyu --dataset eval_nyu/datasets/local-cdut.json --engines pi \
      --limit 3 --budget 300 --out eval_nyu/results/pilot-1.jsonl --report

  # compare against the June-2026 historical baseline
  python -m eval_nyu --report --out eval_nyu/results/pilot-1.jsonl \
      --baseline eval_nyu/results/baseline-2026-06-11.jsonl

  # re-ingest the baseline from the old report markdown
  python -m eval_nyu --ingest-baseline public_eval/RESULTS.md \
      --out eval_nyu/results/baseline-2026-06-11.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(prog="eval_nyu", description="CTF eval harness (P5)")
    ap.add_argument("--dataset", help="dataset manifest (local JSON) or NYU dataset root dir")
    ap.add_argument("--format", choices=["auto", "local", "nyu"], default="auto")
    ap.add_argument("--engines", default="pi",
                    help="comma-separated engine roster (default: pi)")
    ap.add_argument("--limit", type=int, default=0, help="max challenges to run (0 = all)")
    ap.add_argument("--budget", type=int, default=300,
                    help="wall-clock budget per challenge in seconds (default 300)")
    ap.add_argument("--out", default=str(ROOT / "eval_nyu" / "results" / "latest.jsonl"),
                    help="results JSONL path (appends; already-measured pairs are skipped)")
    ap.add_argument("--report", action="store_true", help="render the markdown report")
    ap.add_argument("--baseline", default=str(ROOT / "eval_nyu" / "results" / "baseline-2026-06-11.jsonl"),
                    help="baseline JSONL for the comparison table")
    ap.add_argument("--no-baseline", action="store_true", help="skip the baseline table")
    ap.add_argument("--list", action="store_true", help="print the dataset and exit")
    ap.add_argument("--no-deploy", action="store_true",
                    help="skip docker target deployment (static targets still run)")
    ap.add_argument("--sessions", default=str(ROOT / "sessions"),
                    help="per-challenge workspace root (default sessions/; "
                         "challenge dirs land under sessions/eval/)")
    ap.add_argument("--accounts", default=None,
                    help="credential-account store root (default: env DSWARM_ACCOUNTS_ROOT)")
    ap.add_argument("--ingest-baseline", metavar="MD", default=None,
                    help="ingest measured rows from the old report markdown into --out")
    args = ap.parse_args()

    if args.ingest_baseline:
        from eval_nyu.report import save_baseline
        n = save_baseline(args.ingest_baseline, args.out)
        print(f"ingested {n} measured baseline rows -> {args.out}")
        return 0

    if not args.dataset:
        if args.report and Path(args.out).exists():
            # report-only re-render of an existing results file
            _render(args)
            return 0
        ap.error("--dataset is required (or use --ingest-baseline)")

    from eval_nyu.runner import load_dataset

    challenges = load_dataset(args.dataset)
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    if args.limit > 0:
        challenges = challenges[: args.limit]
    print(f"dataset: {len(challenges)} challenges × engines {engines}", flush=True)
    for c in challenges:
        print(f"  - {c.challenge_id} [{c.category}] "
              f"deploy={c.deploy.get('kind', 'static')} gt={len(c.ground_truth)}", flush=True)
    if args.list:
        return 0

    from eval_nyu.runner import _run_all

    asyncio.run(_run_all(
        challenges, engines,
        out_path=Path(args.out),
        budget_s=args.budget,
        sessions_root=Path(args.sessions),
        credential_accounts_root=args.accounts,
        deploy=not args.no_deploy,
    ))
    print(f"results appended -> {args.out}", flush=True)

    if args.report:
        _render(args)
    return 0


def _render(args: argparse.Namespace) -> None:
    """Render the markdown report from a results file; console print is
    ASCII-safe (the GBK Windows console can't encode ✓/✗ — the FILE is UTF-8)."""
    from eval_nyu.report import load_results, render

    results = load_results(args.out)
    baseline = None
    if not args.no_baseline:
        bp = Path(args.baseline)
        if bp.exists():
            baseline = load_results(bp)
    md = render(results, baseline=baseline,
                title=f"CTF eval regression — {Path(args.out).stem}")
    out_md = Path(args.out).with_suffix(".report.md")
    out_md.write_text(md, encoding="utf-8")
    print(f"report -> {out_md}", flush=True)
    try:
        print(md)
    except UnicodeEncodeError:
        print(md.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    sys.exit(main())
