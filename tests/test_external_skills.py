"""Deterministic tests for the vendored external CTF/pentest skills.

These tests validate the vendored layout under ``docker/worker-pi/`` (what gets
baked into the worker images) and the swarm runtime wiring
(``_ensure_base_skill_links``). They never need docker or the network.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_PI = REPO_ROOT / "docker" / "worker-pi"
DIRECTIONS = WORKER_PI / "directions"
BASE_SKILLS = WORKER_PI / "base-skills"
SCRIPTS = WORKER_PI / "scripts"


EXPECTED_VENDORED = {
    "web": {
        "ctf-web",
        # yaklang host-pentest playbooks
        "linux-privilege-escalation",
        "linux-lateral-movement",
        "linux-security-bypass",
        "container-escape-techniques",
        "kubernetes-pentesting",
        "unauthorized-access-common-services",
        "reverse-shell-techniques",
        "tunneling-and-pivoting",
    },
    "rev": {
        "ctf-reverse",
        # zhaoxuya520/reverse-skill modules (primary reversing toolkit)
        "reverse-engineering",
        "ghidra-reverse",
        "ida-reverse",
        "radare2",
        "js-reverse",
        "apk-reverse",
        "dotnet-reverse",
        "go-rust-reverse",
        "macos-reverse",
        "mobile-reverse",
        "protocol-reverse",
        "binary-diff",
        "patch-diff-exploit",
        "edr-bypass-re",
        "firmware-pentest",
        "hardware-security",
        "pwn-chain",
        "malware-analysis",
    },
    "pwn": {"ctf-pwn"},
    "crypto": {"ctf-crypto"},
    "misc": {"ctf-misc", "ctf-osint"},
    "forensics": {"ctf-forensics"},
    "aisec": {"ctf-ai-ml"},
}

EXPECTED_BASE = {"solve-challenge", "ctf-writeup"}

# reverse-skill shared infrastructure dirs vendored next to the rev modules.
# They carry no SKILL.md (inert for pi's one-level discovery) but must exist so
# the modules' `../field-journal`, `../scripts`, `../ops`, `../config` and
# `../references` references resolve.
REVERSE_SHARED_DIRS = {
    "field-journal",
    "references",
    "scripts",
    "config",
    "ops",
}

# Upstream nested SKILL.md kept as a sub-workflow inside a vendored module. pi
# discovery is one level, so this file is inert (reachable only as a path).
NESTED_SKILL_MD_ALLOW = {
    Path("rev") / "skills" / "reverse-engineering" / "dsl-vm-reverse" / "SKILL.md",
}


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    _, block, _rest = text.split("---", 2)
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _all_skill_dirs() -> list[Path]:
    """Skill dirs = top-level dirs that carry a SKILL.md (pi-discoverable)."""
    dirs: list[Path] = []
    for direction_dir in DIRECTIONS.iterdir():
        if not direction_dir.is_dir():
            continue
        skills = direction_dir / "skills"
        if skills.is_dir():
            dirs.extend(
                d for d in skills.iterdir()
                if d.is_dir() and (d / "SKILL.md").is_file()
            )
    dirs.extend(
        d for d in BASE_SKILLS.iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    )
    return dirs


def test_expected_skill_layout_present() -> None:
    for direction, names in EXPECTED_VENDORED.items():
        for name in names:
            skill = DIRECTIONS / direction / "skills" / name
            assert (skill / "SKILL.md").is_file(), f"missing {direction}/{name}"
    for name in EXPECTED_BASE:
        assert (BASE_SKILLS / name / "SKILL.md").is_file(), f"missing base {name}"


def test_every_vendored_skill_has_name_and_description_frontmatter() -> None:
    for skill in _all_skill_dirs():
        meta = _frontmatter(skill / "SKILL.md")
        assert meta.get("name"), f"{skill}: missing frontmatter name"
        assert meta.get("description"), f"{skill}: missing frontmatter description"
        assert meta["name"] == skill.name, (
            f"{skill}: frontmatter name {meta['name']!r} != dir name {skill.name!r}"
        )


def test_skill_names_unique_across_directions_and_base() -> None:
    names = [skill.name for skill in _all_skill_dirs()]
    assert len(names) == len(set(names)), f"duplicate skill names: {names}"


def test_no_nested_skill_markdown_beyond_one_level() -> None:
    """pi skill discovery is one level; no SKILL.md may sit deeper."""
    for skill in _all_skill_dirs():
        nested = list(skill.rglob("SKILL.md"))
        unexpected = [
            p for p in nested
            if p.parent != skill
            and p.relative_to(DIRECTIONS) not in NESTED_SKILL_MD_ALLOW
        ]
        assert unexpected == [], (
            f"{skill}: nested SKILL.md files would break pi discovery: {unexpected}"
        )


@pytest.mark.parametrize("direction", list(EXPECTED_VENDORED))
def test_direction_skill_size_budget(direction: str) -> None:
    skills = DIRECTIONS / direction / "skills"
    total = sum(p.stat().st_size for p in skills.rglob("*") if p.is_file())
    # rev includes the upstream reverse-skill modules plus shared infra dirs.
    budget = 4_000_000 if direction == "rev" else 2_000_000
    assert total < budget, f"{direction}: {total} bytes exceeds {budget // 1_000_000}MB"


def test_yaklang_routing_references_only_vendored_siblings() -> None:
    link_re = re.compile(r"\]\(\.\./([a-z0-9-]+)/SKILL\.md\)")
    vendored = EXPECTED_VENDORED["web"]
    for skill_dir in (DIRECTIONS / "web" / "skills").iterdir():
        if skill_dir.name == "ctf-web":
            continue  # ljagiello skill; no ../skill routing links
        for path in skill_dir.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            refs = set(link_re.findall(text))
            assert refs <= vendored, (
                f"{path.relative_to(REPO_ROOT)} references non-vendored "
                f"skills: {sorted(refs - vendored)}"
            )


def test_installer_references_rewritten_to_image_path() -> None:
    relative = re.compile(r"(?<!\.\./)scripts/install_ctf_tools\.sh")
    image_path = "/opt/dswarm/ctf-tools/install_ctf_tools.sh"
    for skill in _all_skill_dirs():
        for path in skill.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert not relative.search(text), (
                f"{path.relative_to(REPO_ROOT)} still has a relative installer ref"
            )
    solve = BASE_SKILLS / "solve-challenge" / "SKILL.md"
    assert image_path in solve.read_text(encoding="utf-8")


def test_base_skills_baked_by_dockerfile() -> None:
    dockerfile = (WORKER_PI / "Dockerfile").read_text(encoding="utf-8")
    for name in EXPECTED_BASE:
        assert (
            f"COPY ./base-skills/{name} /home/ctf/.pi/agent/skills/{name}"
            in dockerfile
        ), f"{name} not baked by the base Dockerfile"


def test_dockerfile_distributes_installer_and_pip_gap_fill() -> None:
    dockerfile = (WORKER_PI / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY ./scripts/install_ctf_tools.sh /opt/dswarm/ctf-tools/install_ctf_tools.sh" in dockerfile
    assert "COPY ./scripts/pip_gap_fill.py /opt/dswarm/ctf-tools/pip_gap_fill.py" in dockerfile
    assert "pip_gap_fill.py" in dockerfile


def test_vendored_installer_parses_pip_packages() -> None:
    installer = SCRIPTS / "install_ctf_tools.sh"
    assert installer.is_file()
    spec = importlib.util.spec_from_file_location(
        "pip_gap_fill", SCRIPTS / "pip_gap_fill.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    packages = mod.parse_pip_packages(installer)
    assert len(packages) >= 20, f"expected >=20 pip entries, got {len(packages)}"
    for spec_entry, import_name in packages:
        assert spec_entry and "==" in spec_entry
        assert import_name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", import_name)


def test_third_party_manifest_records_sources_and_commits() -> None:
    manifest = (WORKER_PI / "THIRD_PARTY_SKILLS.md").read_text(encoding="utf-8")
    assert "ljagiello/ctf-skills" in manifest
    assert "yaklang/hack-skills" in manifest
    assert "zhaoxuya520/reverse-skill" in manifest
    assert "0942e797a3de" in manifest
    assert "c9a4b9ee8645" in manifest
    assert "7427eb0dd28b" in manifest


def test_reverse_skill_shared_infra_vendored_without_skill_md() -> None:
    rev_skills = DIRECTIONS / "rev" / "skills"
    for name in REVERSE_SHARED_DIRS:
        d = rev_skills / name
        assert d.is_dir(), f"missing reverse-skill shared dir {name}"
        assert not (d / "SKILL.md").exists(), (
            f"{name} must stay inert (no SKILL.md) for pi discovery"
        )
    assert (rev_skills / "tool-index.md").is_file(), (
        "reverse-skill modules reference tool-index.md at the skills root"
    )


def test_reverse_skill_module_references_resolve_in_rev_home(tmp_path: Path) -> None:
    """Every ../ reference from a vendored rev module resolves under rev skills."""
    rev_skills = DIRECTIONS / "rev" / "skills"
    allowed_dirs = (
        set(EXPECTED_VENDORED["rev"]) | REVERSE_SHARED_DIRS
    )
    link_re = re.compile(r"\]\(\.\./([^)]+)\)")
    for skill_dir in rev_skills.iterdir():
        if not skill_dir.is_dir():
            continue
        for path in skill_dir.rglob("*.md"):
            for target in link_re.findall(path.read_text(encoding="utf-8")):
                top = target.split("/", 1)[0]
                assert top in allowed_dirs, (
                    f"{path.relative_to(REPO_ROOT)}: {target} does not resolve "
                    f"inside the rev skill set"
                )


def test_ensure_base_skill_links_creates_links(tmp_path: Path) -> None:
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    home.mkdir()
    swarm_mod._ensure_base_skill_links(home)
    for name in swarm_mod._BASE_SKILLS:
        link = home / ".pi" / "agent" / "skills" / name
        assert link.is_symlink()
        assert link.readlink() == Path(f"/home/ctf/.pi/agent/skills/{name}")


def test_ensure_direction_links_surfaces_vendored_web_skills(tmp_path: Path) -> None:
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    home.mkdir()
    swarm_mod._ensure_direction_links(home, "web")
    for name in ("ctf-web", "linux-privilege-escalation"):
        link = home / ".pi" / "agent" / "skills" / name
        assert link.is_symlink(), f"missing link for {name}"
        assert link.readlink() == Path(f"/opt/dswarm/direction/skills/{name}")
    # .gitkeep-style placeholders must not leak into the worker skill dir
    assert not (home / ".pi" / "agent" / "skills" / ".gitkeep").exists()


def test_ensure_direction_links_surfaces_vendored_rev_skills(tmp_path: Path) -> None:
    from dswarm.swarm import swarm as swarm_mod

    home = tmp_path / "home"
    home.mkdir()
    swarm_mod._ensure_direction_links(home, "rev")
    for name in ("reverse-engineering", "ctf-reverse"):
        link = home / ".pi" / "agent" / "skills" / name
        assert link.is_symlink(), f"missing link for {name}"
        assert link.readlink() == Path(f"/opt/dswarm/direction/skills/{name}")
    # loose root reference files (e.g. tool-index.md) are linked as files
    tool = home / ".pi" / "agent" / "skills" / "tool-index.md"
    assert tool.is_symlink()
    assert tool.readlink() == Path("/opt/dswarm/direction/skills/tool-index.md")
