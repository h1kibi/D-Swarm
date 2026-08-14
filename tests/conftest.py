"""Shared test fixtures + Windows platformization.

- `posix` mark: tests that exercise POSIX-only behavior (container exec paths,
  process groups, /bin/sh scripts, chmod semantics). They are skipped on
  Windows — dswarm's container execution is Linux-container based, so these
  tests are only meaningful on a POSIX host (or CI).
- `Path.read_text` default encoding: on a Chinese-locale Windows host the
  locale codec is GBK, which breaks reading UTF-8 source/UI files. The whole
  repo is UTF-8, so default to utf-8 for tests.
"""
from __future__ import annotations

import pathlib
import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "posix: POSIX-only behavior — skipped automatically on Windows",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        skip = pytest.mark.skip(reason="POSIX-only behavior (skipped on Windows)")
        for item in items:
            if "posix" in item.keywords:
                item.add_marker(skip)


# The repo is UTF-8 everywhere; on a Chinese-locale Windows host the default
# locale codec is GBK and Path.read_text() chokes on non-ASCII bytes. Tests
# should behave identically on every host, so default to utf-8.
_orig_read_text = pathlib.Path.read_text


def _read_text_utf8(self, encoding: str | None = None, *args, **kwargs):
    return _orig_read_text(self, encoding or "utf-8", *args, **kwargs)


pathlib.Path.read_text = _read_text_utf8  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _disable_auto_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    # Auto-binding pi-main from DEEPSEEK_API_KEY is a runtime convenience. Keep
    # unit tests on the old explicit-account behavior unless a test opts in.
    monkeypatch.setenv("DSWARM_AUTO_BIND_PI_ACCOUNT", "0")
