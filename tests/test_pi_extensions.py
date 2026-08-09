"""Deterministic checks for the D-Swarm pi base extensions (docker/worker-pi)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "docker" / "worker-pi" / "scripts" / "check_pi_extensions.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_pi_extensions", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_extensions_layout_and_import_whitelist():
    module = _load_checker()
    errors = module.check_extension_layout()
    assert errors == [], "\n".join(errors)


def test_models_json_only_references_gateway_models():
    module = _load_checker()
    errors = module.check_models_json()
    assert errors == [], "\n".join(errors)


def test_dockerfiles_copy_only_ts_extensions():
    module = _load_checker()
    errors = module.check_dockerfile_copy_only_ts()
    assert errors == [], "\n".join(errors)


def test_expected_extension_set_is_exact():
    module = _load_checker()
    files = {path.name for path in module.extension_files()}
    assert files == module.EXPECTED_EXTENSIONS


def test_legacy_root_extensions_package_removed():
    assert not (REPO_ROOT / "extensions").exists(), (
        "legacy root extensions/ package must be removed (consolidated into docker/worker-pi)"
    )
    assert not (REPO_ROOT / "tests" / "test_pi_extension.py").exists()


def test_tsc_no_emit_when_compiler_available():
    module = _load_checker()
    if module.find_tsc() is None:
        pytest.skip("no TypeScript compiler available on this machine")
    ok, note = module.run_tsc()
    assert ok, note
