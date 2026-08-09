#!/usr/bin/env python3
"""Vendor external Agent Skills into the direction build context.

Pinned upstreams (both MIT):

  - ljagiello/ctf-skills   @ 0942e797a3deb6825cb40f2daabd25b738cd3a45
  - yaklang/hack-skills    @ c9a4b9ee8645eb60763eb4eef172f1ecb0a5b3e8
  - zhaoxuya520/reverse-skill @ 7427eb0dd28bd30d6c36b6793936acb4b0226cc4

The script copies the selected skill directories into
``docker/worker-pi/directions/<dir>/skills/<name>/`` (per-direction layers) and
``docker/worker-pi/base-skills/<name>/`` (available to every worker), applies
the minimal patches needed to make the copies self-contained inside a worker
image, drops a per-skill LICENSE copy, and regenerates
``THIRD_PARTY_SKILLS.md`` for provenance.

Usage:
  python docker/worker-pi/scripts/vendor_external_skills.py
      [--ljagiello DIR] [--yaklang DIR]

Without ``--ljagiello/--yaklang`` the script clones the pinned commits into
``.vendor-cache/`` under the repo root (requires network + git).

The copies are the source of truth that gets baked into the images; re-running
the script is safe and idempotent (destinations are replaced from the pinned
sources).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
WORKER_PI = REPO_ROOT / "docker" / "worker-pi"
DIRECTIONS = WORKER_PI / "directions"
BASE_SKILLS = WORKER_PI / "base-skills"
SCRIPTS = WORKER_PI / "scripts"
CACHE_ROOT = REPO_ROOT / ".vendor-cache"
THIRD_PARTY = WORKER_PI / "THIRD_PARTY_SKILLS.md"


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    commit: str
    copyright: str
    skill_root: str  # relative to the clone root


LJAGIELLO = Source(
    name="ljagiello/ctf-skills",
    url="https://github.com/ljagiello/ctf-skills",
    commit="0942e797a3deb6825cb40f2daabd25b738cd3a45",
    copyright="Copyright (c) 2026 Lukasz Jagiello",
    skill_root=".",
)
YAKLANG = Source(
    name="yaklang/hack-skills",
    url="https://github.com/yaklang/hack-skills",
    commit="c9a4b9ee8645eb60763eb4eef172f1ecb0a5b3e8",
    copyright="Copyright (c) 2026 VillanCh",
    skill_root="skills",
)

REVERSE = Source(
    name="zhaoxuya520/reverse-skill",
    url="https://github.com/zhaoxuya520/reverse-skill",
    commit="7427eb0dd28bd30d6c36b6793936acb4b0226cc4",
    copyright="Copyright (c) 2026 zhaoxuya520",
    skill_root="skills",
)


MIT_LICENSE_TMPL = """\
MIT License

{copyright}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# skill name -> destination directory (directions/<dir>/skills/<name>)
# "base" goes to base-skills/<name> instead (baked for every worker).
VENDOR_MAP: dict[tuple[str, str], str] = {
    # (source name, skill dir) -> destination direction
    (LJAGIELLO.name, "ctf-web"): "web",
    (LJAGIELLO.name, "ctf-reverse"): "rev",
    (LJAGIELLO.name, "ctf-pwn"): "pwn",
    (LJAGIELLO.name, "ctf-crypto"): "crypto",
    (LJAGIELLO.name, "ctf-misc"): "misc",
    (LJAGIELLO.name, "ctf-osint"): "misc",
    (LJAGIELLO.name, "ctf-forensics"): "forensics",
    (LJAGIELLO.name, "ctf-ai-ml"): "aisec",
    (LJAGIELLO.name, "solve-challenge"): "base",
    (LJAGIELLO.name, "ctf-writeup"): "base",
    (YAKLANG.name, "linux-privilege-escalation"): "web",
    (YAKLANG.name, "linux-lateral-movement"): "web",
    (YAKLANG.name, "linux-security-bypass"): "web",
    (YAKLANG.name, "container-escape-techniques"): "web",
    (YAKLANG.name, "kubernetes-pentesting"): "web",
    (YAKLANG.name, "unauthorized-access-common-services"): "web",
    (YAKLANG.name, "reverse-shell-techniques"): "web",
    (YAKLANG.name, "tunneling-and-pivoting"): "web",
    (REVERSE.name, "reverse-engineering"): "rev",
    (REVERSE.name, "ghidra-reverse"): "rev",
    (REVERSE.name, "ida-reverse"): "rev",
    (REVERSE.name, "radare2"): "rev",
    (REVERSE.name, "js-reverse"): "rev",
    (REVERSE.name, "apk-reverse"): "rev",
    (REVERSE.name, "dotnet-reverse"): "rev",
    (REVERSE.name, "go-rust-reverse"): "rev",
    (REVERSE.name, "macos-reverse"): "rev",
    (REVERSE.name, "mobile-reverse"): "rev",
    (REVERSE.name, "protocol-reverse"): "rev",
    (REVERSE.name, "binary-diff"): "rev",
    (REVERSE.name, "patch-diff-exploit"): "rev",
    (REVERSE.name, "edr-bypass-re"): "rev",
    (REVERSE.name, "firmware-pentest"): "rev",
    (REVERSE.name, "hardware-security"): "rev",
    (REVERSE.name, "pwn-chain"): "rev",
    (REVERSE.name, "malware-analysis"): "rev",
}

# yaklang skills vendored into the web direction. Cross-skill routing links in
# the vendored playbooks may only point at these siblings; references to other
# yaklang skills (windows-*, ssrf-*, jndi-injection, ...) are stripped because
# those skills are not bundled.
YAKLANG_WEB_SET = frozenset(
    name for (src, name), dest in VENDOR_MAP.items()
    if src == YAKLANG.name and dest == "web"
)

REVERSE_REV_SET = frozenset(
    name for (src, name), dest in VENDOR_MAP.items()
    if src == REVERSE.name and dest == "rev"
)

# Shared (non-skill) infrastructure dirs vendored next to the reverse-skill
# modules. They carry no SKILL.md (inert for pi's one-level skill discovery)
# but must exist as siblings so the modules' `../field-journal`, `../scripts`,
# `../ops`, `../config` and `../references` references resolve inside the
# worker HOME.
REVERSE_SHARED_DIRS = ("field-journal", "references", "scripts", "config", "ops")

# Loose root file vendored for the reverse-skill modules' "read tool-index.md"
# instruction. Upstream ships a template that is machine-generated per host; we
# ship it as tool-index.md so the instruction resolves (the direction linker
# exposes loose root files as well as skill dirs).
REVERSE_ROOT_FILES = {"tool-index.md.template": "tool-index.md"}

# Image path where the vendored ljagiello installer is baked; relative
# "scripts/install_ctf_tools.sh" references in the skills are rewritten to this
# absolute path so they work from any worker working directory.
INSTALLER_IMAGE_PATH = "/opt/dswarm/ctf-tools/install_ctf_tools.sh"

_SKILL_LINK_RE = re.compile(r"\[([^\]]+)\]\(\.\./([a-z0-9-]+)/SKILL\.md\)")
_JUNK_DIRS = {"__pycache__", ".git"}


def _git(clone: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(clone), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"git -C {clone} {' '.join(args)} failed: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def ensure_source(source: Source, local: str | None) -> Path:
    """Return a checkout of `source` at its pinned commit."""
    if local:
        clone = Path(local)
        if not (clone / ".git").is_dir():
            raise SystemExit(f"--{source.name.split('/')[0]} path is not a git repo: {clone}")
        return clone
    clone = CACHE_ROOT / source.name.replace("/", "__")
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--quiet", source.url, str(clone)],
            check=True,
        )
    head = _git(clone, "rev-parse", "HEAD")
    if head != source.commit:
        _git(clone, "fetch", "--quiet", "origin")
        _git(clone, "checkout", "--quiet", "--detach", source.commit)
    return clone


def _wipe_and_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    for junk in _JUNK_DIRS:
        shutil.rmtree(dst / junk, ignore_errors=True)


def _patch_installer_paths(root: Path) -> int:
    """Rewrite relative install_ctf_tools.sh references to the image path."""
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or not path.suffix.lower() in {".md", ".sh"}:
            continue
        text = path.read_text(encoding="utf-8")
        new = text.replace("../scripts/install_ctf_tools.sh", INSTALLER_IMAGE_PATH)
        new = new.replace("scripts/install_ctf_tools.sh", INSTALLER_IMAGE_PATH)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def _patch_yaklang_routing(root: Path) -> int:
    """Keep only cross-skill routing links that point at vendored siblings."""
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        out: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            refs = list(_SKILL_LINK_RE.finditer(line))
            if not refs:
                out.append(line)
                continue
            vendored = [m for m in refs if m.group(2) in YAKLANG_WEB_SET]
            if line.lstrip().startswith("- [") and not vendored:
                # routing bullet with no vendored sibling -> drop the line
                changed += 1
                continue
            new_line = line
            for m in refs:
                if m.group(2) not in YAKLANG_WEB_SET:
                    new_line = new_line.replace(
                        m.group(0),
                        f"{m.group(1)} (not bundled in this image)",
                    )
                    changed += 1
            out.append(new_line)
        # Drop a dangling "See X skills:" section header when every bullet that
        # followed it was removed (the next non-empty line is a heading).
        out = _drop_dangling_see_headers(out, changed)
        path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return changed


_SEE_HEADER_RE = re.compile(r"^See .+:$")


def _drop_dangling_see_headers(lines: list[str], changed: int) -> list[str]:
    """Remove ``See X skills:`` lines whose following list was fully dropped."""
    if changed == 0:
        return lines
    keep = [True] * len(lines)
    for i, line in enumerate(lines):
        if not _SEE_HEADER_RE.match(line.strip()):
            continue
        nxt = i + 1
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt < len(lines) and lines[nxt].strip().startswith("#"):
            keep[i] = False
    return [line for i, line in enumerate(lines) if keep[i]]


def _patch_reverse_routing(root: Path) -> int:
    """Neutralize reverse-skill references to non-vendored modules.

    Every `../x/...` reference must resolve inside the rev worker: `x` must be a
    vendored reverse-skill module or a vendored shared dir. References to
    anything else (e.g. the upstream CTF-Sandbox-Orchestrator tree) are replaced
    with plain text so the worker never follows a dangling path.
    """
    allowed = REVERSE_REV_SET | set(REVERSE_SHARED_DIRS)
    changed = 0
    ref_re = re.compile(r"\[([^\]]+)\]\(\.\./([^)]+)\)")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        text = path.read_text(encoding="utf-8")
        new = text
        for m in ref_re.finditer(text):
            top = m.group(2).split("/", 1)[0]
            if top in allowed:
                continue
            new = new.replace(
                m.group(0), f"{m.group(1)} (not bundled in this image)"
            )
            changed += 1
        if new != text:
            path.write_text(new, encoding="utf-8")
    return changed


def _write_license(dst: Path, source: Source) -> None:
    license_path = dst / "LICENSE"
    if not license_path.exists():
        license_path.write_text(
            MIT_LICENSE_TMPL.format(copyright=source.copyright), encoding="utf-8"
        )


def _vendor_installer(clone: Path, source: Source) -> None:
    """Vendor ljagiello's install_ctf_tools.sh for build + on-demand install."""
    src = clone / "scripts" / "install_ctf_tools.sh"
    if not src.exists():
        raise SystemExit(f"installer not found in {source.name}: {src}")
    dst = SCRIPTS / "install_ctf_tools.sh"
    shutil.copy2(src, dst)
    print(f"vendored installer: {dst.relative_to(REPO_ROOT)}")


def _write_third_party() -> None:
    rows: list[tuple[str, str, str, str, str]] = []
    for (src_name, skill), dest in sorted(VENDOR_MAP.items()):
        source = {
            LJAGIELLO.name: LJAGIELLO,
            YAKLANG.name: YAKLANG,
            REVERSE.name: REVERSE,
        }[src_name]
        where = (
            f"base-skills/{skill}/"
            if dest == "base"
            else f"directions/{dest}/skills/{skill}/"
        )
        rows.append((skill, source.name, source.commit, source.url, where))
    lines = [
        "# Third-party vendored skills",
        "",
        "The directories below are copied (vendored) from upstream repositories at a",
        "pinned commit and are shipped inside the worker images. Each copied skill",
        "directory carries its own `LICENSE` copy (both upstreams are MIT).",
        "",
        "| Skill | Upstream | Commit | Source | Vendored at |",
        "|---|---|---|---|---|",
    ]
    for skill, name, commit, url, where in rows:
        lines.append(f"| {skill} | {name} | `{commit[:12]}` | {url} | {where} |")
    lines += [
        "",
        "## Patches applied to the vendored copies",
        "",
        "- yaklang playbooks: cross-skill routing links are kept only when they point",
        "  at a vendored sibling; references to non-bundled yaklang skills are removed",
        "  (or marked \"not bundled in this image\").",
        "- ljagiello skills: relative `scripts/install_ctf_tools.sh` references are",
        f"  rewritten to the image path `{INSTALLER_IMAGE_PATH}` (the installer is",
        "  vendored at `docker/worker-pi/scripts/install_ctf_tools.sh`).",
        "- zhaoxuya520/reverse-skill modules: cross-module references are kept only",
        "  when they point at a vendored module or a vendored shared dir"
        "  (field-journal, references, scripts, config, ops); the upstream `tool-index.md.template`",
        "  is vendored as `tool-index.md` so the modules' tool-index instruction",
        "  resolves.",
        "",
        "Reverse-skill shared infra (no SKILL.md, referenced via `../`): "
        "directions/rev/skills/field-journal, directions/rev/skills/references, directions/rev/skills/scripts, directions/rev/skills/config, directions/rev/skills/ops.",
        "",
        f"Re-vendor with: `python docker/worker-pi/scripts/vendor_external_skills.py`",
        "",
    ]
    THIRD_PARTY.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ljagiello", help="local clone of ljagiello/ctf-skills")
    parser.add_argument("--yaklang", help="local clone of yaklang/hack-skills")
    parser.add_argument("--reverse", help="local clone of zhaoxuya520/reverse-skill")
    args = parser.parse_args()

    lja = ensure_source(LJAGIELLO, args.ljagiello)
    yak = ensure_source(YAKLANG, args.yaklang)
    rev = ensure_source(REVERSE, args.reverse)
    sources = {
        LJAGIELLO.name: (lja, LJAGIELLO),
        YAKLANG.name: (yak, YAKLANG),
        REVERSE.name: (rev, REVERSE),
    }

    for (src_name, skill), dest in sorted(VENDOR_MAP.items()):
        clone, source = sources[src_name]
        src = clone / source.skill_root / skill
        if not (src / "SKILL.md").exists():
            raise SystemExit(f"missing SKILL.md in {source.name} {src}")
        dst = (
            BASE_SKILLS / skill
            if dest == "base"
            else DIRECTIONS / dest / "skills" / skill
        )
        _wipe_and_copy(src, dst)
        _write_license(dst, source)
        if source is LJAGIELLO:
            _patch_installer_paths(dst)
        elif source is YAKLANG:
            _patch_yaklang_routing(dst)
        else:
            _patch_reverse_routing(dst)
        print(f"vendored {source.name}/{skill} -> {dst.relative_to(REPO_ROOT)}")


    # reverse-skill shared infra + loose root file for the rev direction
    rev_clone, rev_source = sources[REVERSE.name]
    for shared in REVERSE_SHARED_DIRS:
        src = rev_clone / "skills" / shared
        if not src.is_dir():
            raise SystemExit(f"missing reverse-skill shared dir {src}")
        dst = DIRECTIONS / "rev" / "skills" / shared
        _wipe_and_copy(src, dst)
        _write_license(dst, rev_source)
        _patch_reverse_routing(dst)
        print(f"vendored {rev_source.name}/{shared} -> {dst.relative_to(REPO_ROOT)}")
    for template, name in REVERSE_ROOT_FILES.items():
        src = rev_clone / "skills" / template
        dst = DIRECTIONS / "rev" / "skills" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"vendored {rev_source.name}/{template} -> {dst.relative_to(REPO_ROOT)}")

    _vendor_installer(lja, LJAGIELLO)
    _write_third_party()
    print(f"wrote {THIRD_PARTY.relative_to(REPO_ROOT)}")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
