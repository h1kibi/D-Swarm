#!/usr/bin/env python3
"""Offline checks for the D-Swarm pi base extensions.

Run standalone (``python3 docker/worker-pi/scripts/check_pi_extensions.py``) or
import the ``run_checks`` helpers from tests. No third-party dependencies.

Checks:
1. Every ``pi-config/extensions/*.ts`` exists, default-exports a function and
   only imports whitelisted modules (``@earendil-works/pi-coding-agent`` as a
   type-only import, ``typebox``, ``node:*``, relative ``./``).
2. ``models.json`` only references the two ctf-gateway model ids and the
   gateway extension registers the same ids.
3. The base ``Dockerfile`` / ``Dockerfile.direction`` copy extensions by
   ``-name '*.ts'`` (never a bare ``cp .../*`` that would drag README/types in).
4. Optional: run ``tsc --noEmit`` against ``pi-config/extensions/tsconfig.json``
   when a TypeScript compiler is discoverable (``TS_BIN`` env, ``tsc`` on PATH,
   or the repo's ``apps/web/ui/node_modules/.bin/tsc``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PI_CONFIG = REPO_ROOT / "docker" / "worker-pi" / "pi-config"
EXT_DIR = PI_CONFIG / "extensions"
WORKER_PI = REPO_ROOT / "docker" / "worker-pi"

EXPECTED_EXTENSIONS = {
    "ctf-gateway-provider.ts",
    "ctf-blackboard-watchdog.ts",
    "ctf-provenance-guard.ts",
    "ctf-context-injector.ts",
    "ctf-evidence-note.ts",
    "dswarm-worker-provider.ts",
}

EXPECTED_GATEWAY_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro",
                          "glm-5.3-flash"}

IMPORT_RE = re.compile(
    r"^\s*import\s+(?P<kind>type\s+)?(?:[^'\"\n]*?\s+from\s+)?['\"](?P<spec>[^'\"]+)['\"]",
    re.MULTILINE,
)

# module specifier -> allow type import / allow value import
ALLOWED_IMPORTS = {
    "@earendil-works/pi-coding-agent": (True, False),
    "typebox": (True, True),
    "node:fs": (True, True),
    "node:path": (True, True),
    "node:url": (True, True),
}


def extension_files() -> list[Path]:
    return sorted(p for p in EXT_DIR.glob("*.ts"))


def check_imports(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for m in IMPORT_RE.finditer(text):
        spec = m.group("spec")
        is_type = bool(m.group("kind"))
        if spec.startswith("./") or spec.startswith("../"):
            continue  # relative imports within the extensions dir are fine
        entry = ALLOWED_IMPORTS.get(spec)
        if entry is None:
            errors.append(f"{path.name}: disallowed import '{spec}'")
            continue
        allow_type, allow_value = entry
        if is_type and not allow_type:
            errors.append(f"{path.name}: type import '{spec}' not allowed")
        if not is_type and not allow_value:
            errors.append(f"{path.name}: value import '{spec}' not allowed (use `import type`)")
    return errors


def check_extension_layout() -> list[str]:
    errors: list[str] = []
    files = extension_files()
    names = {p.name for p in files}
    for expected in sorted(EXPECTED_EXTENSIONS):
        if expected not in names:
            errors.append(f"missing extension {expected}")
    for path in files:
        text = path.read_text(encoding="utf-8")
        if len(text.strip()) == 0:
            errors.append(f"{path.name}: empty file")
        if "export default function" not in text:
            errors.append(f"{path.name}: missing `export default function`")
        errors.extend(check_imports(path))
    return errors


def check_models_json() -> list[str]:
    errors: list[str] = []
    models_path = PI_CONFIG / "models.json"
    try:
        data = json.loads(models_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"missing {models_path.relative_to(REPO_ROOT)}"]
    except json.JSONDecodeError as exc:
        return [f"models.json is not valid JSON: {exc}"]
    provider = (data.get("providers") or {}).get("ctf-gateway") or {}
    models = provider.get("models") or []
    ids = {m.get("id") for m in models}
    if ids != EXPECTED_GATEWAY_MODELS:
        errors.append(
            f"models.json ctf-gateway ids {sorted(ids)} != expected {sorted(EXPECTED_GATEWAY_MODELS)}"
        )
    gateway = (EXT_DIR / "ctf-gateway-provider.ts").read_text(encoding="utf-8")
    for model_id in EXPECTED_GATEWAY_MODELS:
        if model_id not in gateway:
            errors.append(f"ctf-gateway-provider.ts does not mention model '{model_id}'")
    if '"ctf-gateway"' not in gateway:
        errors.append("ctf-gateway-provider.ts does not register provider 'ctf-gateway'")
    if "thinkingLevelMap" not in gateway:
        errors.append(
            "ctf-gateway-provider.ts must keep thinkingLevelMap (registerProvider replaces "
            "the models from models.json, so the thinking map would be lost)"
        )
    return errors


def check_dockerfile_copy_only_ts() -> list[str]:
    errors: list[str] = []
    for dockerfile_name in ("Dockerfile", "Dockerfile.direction"):
        dockerfile = WORKER_PI / dockerfile_name
        text = dockerfile.read_text(encoding="utf-8")
        if "cp /opt/dswarm/pi-config/extensions/*" in text:
            errors.append(
                f"{dockerfile_name}: bare `cp .../extensions/*` would copy non-.ts files; "
                "use `find ... -name '*.ts' -exec cp`"
            )
        if "-name '*.ts'" not in text:
            errors.append(f"{dockerfile_name}: extension copy does not filter `-name '*.ts'`")
    return errors


def find_tsc() -> str | None:
    # TS_BIN (explicit override, e.g. TS6 CI) → repo-local → PATH.
    # PATH is checked LAST so a global/other-project tsc cannot shadow the
    # repo's pinned version — a stray TS6 on PATH made the self-test
    # machine-dependent (TS5101 on this repo's older config, skip/pass
    # elsewhere).
    env_bin = os.environ.get("TS_BIN", "").strip()
    if env_bin:
        return env_bin
    is_windows = os.name == "nt"
    candidates = [
        REPO_ROOT / "apps" / "web" / "ui" / "node_modules" / ".bin" / "tsc.cmd",
        REPO_ROOT / "apps" / "web" / "ui" / "node_modules" / ".bin" / "tsc",
        REPO_ROOT / "node_modules" / ".bin" / "tsc",
    ]
    for candidate in candidates:
        if is_windows and candidate.suffix not in (".cmd", ".exe", ".bat"):
            continue
        if candidate.is_file():
            return str(candidate)
    on_path = shutil.which("tsc")
    if on_path:
        return on_path
    return None


def run_tsc() -> tuple[bool, str]:
    tsc = find_tsc()
    if tsc is None:
        return True, "tsc not found - skipped (structural checks still ran)"
    cmd = [tsc, "--noEmit", "-p", str(EXT_DIR / "tsconfig.json")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, f"tsc --noEmit ok ({tsc})"
    detail = proc.stdout + proc.stderr
    return False, f"tsc --noEmit failed ({tsc}):\n{detail.strip()}"


def run_checks(*, with_tsc: bool = True) -> tuple[list[str], str]:
    errors: list[str] = []
    errors.extend(check_extension_layout())
    errors.extend(check_models_json())
    errors.extend(check_dockerfile_copy_only_ts())
    tsc_note = ""
    if with_tsc:
        ok, note = run_tsc()
        tsc_note = note
        if not ok:
            errors.append(note)
    return errors, tsc_note


def main() -> int:
    errors, tsc_note = run_checks()
    print(f"[check_pi_extensions] {tsc_note}")
    if errors:
        print("[check_pi_extensions] FAILED:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("[check_pi_extensions] ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
