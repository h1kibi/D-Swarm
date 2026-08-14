"""Shared filesystem secret-store helpers."""

from __future__ import annotations

from dswarm.solver.secret_store import atomic_write, updated_at


def test_atomic_write_creates_file_and_records_mtime(tmp_path):
    target = tmp_path / "nested" / "API_KEY"
    atomic_write(target, "sk-value\n")
    assert target.read_text(encoding="utf-8") == "sk-value\n"
    assert updated_at(target.parent) is not None


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / "API_KEY"
    atomic_write(target, "first\n")
    atomic_write(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
