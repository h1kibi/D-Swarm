#!/usr/bin/env python3
"""Pre-fill the small pip gap from ljagiello's install_ctf_tools.sh.

Runs at image build time with the image interpreter (/opt/venv/bin/python):
parses the vendored installer's PIP_PACKAGES list, probes each import name with
the current interpreter, and pip-installs only the modules that are missing.
Best-effort by design: a failure is logged and the exit code stays 0 so a flaky
PyPI never breaks the image build (workers can still install at runtime).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "install_ctf_tools.sh"
_PIP_BLOCK_RE = re.compile(r"PIP_PACKAGES=\((.*?)\)", re.S)


def parse_pip_packages(path: Path) -> list[tuple[str, str]]:
    """Return [(pip spec, import name), ...] from the installer's PIP_PACKAGES."""
    text = path.read_text(encoding="utf-8")
    match = _PIP_BLOCK_RE.search(text)
    if not match:
        return []
    packages: list[tuple[str, str]] = []
    for line in match.group(1).splitlines():
        entry = line.strip().strip('"').strip("'")
        if not entry or entry.startswith("#"):
            continue
        spec, _, import_name = entry.partition(":")
        spec = spec.strip()
        import_name = import_name.strip()
        if spec and import_name:
            packages.append((spec, import_name))
    return packages


def main() -> int:
    if not SCRIPT.exists():
        print(f"[pip-gap] installer not found: {SCRIPT}", file=sys.stderr)
        return 0
    missing: list[str] = []
    for spec, import_name in parse_pip_packages(SCRIPT):
        probe = subprocess.run(
            [sys.executable, "-c", f"import {import_name}"],
            capture_output=True,
        )
        if probe.returncode != 0:
            missing.append(spec)
    if not missing:
        print("[pip-gap] no missing pip packages")
        return 0
    print(f"[pip-gap] installing {len(missing)} missing: {' '.join(missing)}")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", *missing],
        check=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
