"""Archive legacy SQLite runs before switching to Postgres-backed runs.

Usage:
  uv run python scripts/archive_legacy_runs.py --dry-run
  uv run python scripts/archive_legacy_runs.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from apps.web.run_manager import RunManager


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", default=os.environ.get("DSWARM_SESSIONS_ROOT", "sessions"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    manager = RunManager(sessions_root=Path(args.sessions))
    result = manager.archive_legacy_runs(dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
