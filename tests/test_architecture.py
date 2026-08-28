"""Dependency-direction guard: the dswarm core must not import apps layers."""

from __future__ import annotations

import re
from pathlib import Path


def test_dswarm_core_does_not_import_apps() -> None:
    root = Path(__file__).resolve().parents[1] / "dswarm"
    offenders: list[str] = []
    import_re = re.compile(r"^\s*(?:from\s+apps\.|import\s+apps\.)")
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if import_re.match(line):
                offenders.append(f"{path.relative_to(root.parent)}:{lineno}: {line.strip()}")
    assert not offenders, "dswarm core must not import apps:\n" + "\n".join(offenders)


def test_startup_shell_scripts_use_lf_line_endings() -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = [root / "init.sh", root / "run.sh"]
    offenders = [str(path.relative_to(root)) for path in scripts if b"\r\n" in path.read_bytes()]
    assert not offenders, "shell scripts must use LF line endings for bash: " + ", ".join(offenders)


def test_init_sh_guards_against_wsl_on_windows_workspace() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "init.sh").read_text(encoding="utf-8")
    assert "/mnt/" in script
    assert "WSL" in script
    assert "Windows workspace" in script
    assert "before running uv" in script


def test_worker_runtime_mixin_has_no_swarm_backedge() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "dswarm" / "swarm" / "worker_runtime_mixin.py").read_text(
        encoding="utf-8"
    )
    assert "from dswarm.swarm.swarm import" not in source
    assert "from dswarm.swarm._bootstrap_assets import" in source
