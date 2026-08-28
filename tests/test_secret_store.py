"""Shared filesystem secret-store helpers."""

from __future__ import annotations

from dswarm.solver import secret_store
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


def test_atomic_write_retries_once_after_transient_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "API_KEY"
    atomic_write(target, "first\n")

    real_replace = secret_store.Path.replace
    replace_calls = []
    sleep_calls = []

    def flaky_replace(self, destination):
        replace_calls.append((self, destination))
        if len(replace_calls) == 1:
            raise PermissionError("transient Windows sharing violation")
        return real_replace(self, destination)

    monkeypatch.setattr(secret_store.Path, "replace", flaky_replace)
    monkeypatch.setattr(secret_store.time, "sleep", sleep_calls.append)

    atomic_write(target, "second\n")

    assert target.read_text(encoding="utf-8") == "second\n"
    assert len(replace_calls) == 2
    assert sleep_calls == [0.01]
