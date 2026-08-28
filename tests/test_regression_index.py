from __future__ import annotations

import re
from pathlib import Path


_RUN_ID = re.compile(r"run-\d+")
_INDEX_ROW = re.compile(r"^\|\s*`(run-\d+)`\s*\|")


def _source_run_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for path in sorted((root / "dswarm").rglob("*.py")):
        ids.update(_RUN_ID.findall(path.read_text(encoding="utf-8")))
    return ids


def _indexed_run_ids(root: Path) -> set[str]:
    index = (root / "docs" / "regression-index.md").read_text(encoding="utf-8")
    return {match.group(1) for match in map(_INDEX_ROW.match, index.splitlines()) if match}


def test_every_kernel_incident_id_is_indexed() -> None:
    root = Path(__file__).resolve().parents[1]
    source_ids = _source_run_ids(root)
    indexed_ids = _indexed_run_ids(root)
    assert source_ids == indexed_ids, (
        "kernel regression IDs must be registered in docs/regression-index.md; "
        f"missing={sorted(source_ids - indexed_ids)} "
        f"stale={sorted(indexed_ids - source_ids)}"
    )


def test_regression_index_declares_its_maintenance_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "regression-index.md").read_text(encoding="utf-8")
    assert "new incident identifier" in text
    assert "tests/test_regression_index.py" in text
    assert "not a flag source" in text
