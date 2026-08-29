"""Worker bootstrap assets shared by the swarm and worker runtime.

This leaf module intentionally has no dependency on ``swarm.py``.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional

_CONTAINER_BLACKBOARD_SKILL = "/opt/dswarm/dswarm-blackboard"
_BLACKBOARD_SKILL_LINKS = (
    # pi (route A): pi discovers skills under ~/.pi/agent/skills
    ".pi/agent/skills/dswarm-blackboard",
)

# pi's provider configuration (settings.json + models-store.json + models.json) is baked into
# the image at /opt/dswarm/pi-config; the worker HOME is ISOLATED per worker, so
# the files must be linked into each isolated HOME for pi to find its provider.
_CONTAINER_PI_CONFIG = "/opt/dswarm/pi-config"
_PI_CONFIG_LINKS = (
    ".pi/agent/settings.json",
    ".pi/agent/models-store.json",
    ".pi/agent/models.json",
    ".pi/agent/extensions",  # ctf-gateway provider extension (route A P3)
)


def _repo_pi_config_root() -> "Optional[Path]":
    root = Path(__file__).resolve().parent.parent.parent / "docker" / "worker-pi" / "pi-config"
    return root if root.exists() else None


def _materialize_runtime_pi_config(workspace_root: str | Path) -> "Optional[Path]":
    """Copy current pi provider config into the bind-mounted run workspace.

    Worker images are long-lived; the checkout can add/fix provider extensions
    without rebuilding the image.  Linking isolated HOME directly to the image copy
    caused stale images to miss the ``dswarm-worker`` provider, so container workers
    should prefer this run-local, version-matched config when a source checkout is
    available.
    """
    src = _repo_pi_config_root()
    if src is None:
        return None
    target = Path(workspace_root) / ".dswarm_runtime" / "pi-config"
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in ("settings.json", "models-store.json", "models.json"):
            s = src / name
            if s.is_file():
                d = target / name
                payload = s.read_bytes()
                if d.is_file() and d.read_bytes() == payload:
                    continue
                tmp = target / f".{name}.staging.{os.getpid()}.{time.time_ns()}"
                try:
                    tmp.write_bytes(payload)
                    os.replace(tmp, d)
                finally:
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass
        ext_src = src / "extensions"
        ext_dst = target / "extensions"
        if ext_src.is_dir():
            shutil.copytree(ext_src, ext_dst, dirs_exist_ok=True)
        return target
    except OSError:
        return None


def _ensure_pi_config_links(
    home: Path, *, config_target_root: str = _CONTAINER_PI_CONFIG,
    copy_source: "Optional[Path]" = None,
) -> None:
    """Expose pi provider config inside an isolated worker HOME.

    Symlinks are the primary mechanism, but on Windows dev hosts (no symlink
    privilege) ``os.symlink`` raises and the silent ``continue`` left worker
    HOMEs without any provider config (``Unknown provider``). When
    ``copy_source`` (the HOST-side config dir) is given, a failed link falls
    back to a real copy so the bind-mounted HOME always carries a config.
    """
    for rel in _PI_CONFIG_LINKS:
        link = home / rel
        target = f"{config_target_root}/{link.name}"
        try:
            if link.is_symlink():
                if os.readlink(link) == target:
                    continue
                link.unlink()
            elif link.exists():
                # Managed worker HOME directories may survive backend restarts.
                # Older images/runs could leave a real copied pi-config directory or
                # file here; if we keep it, fresh runtime providers such as
                # dswarm-worker are shadowed and pi reports ``Unknown provider``.
                if link.is_dir():
                    shutil.rmtree(link)
                else:
                    link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=rel.endswith("extensions"))
        except OSError:
            # Windows dev host without symlink privilege: copy the host-side
            # source instead so the isolated HOME still gets a working config.
            src = (copy_source / link.name) if copy_source is not None else None
            if src is None or not src.exists():
                continue
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, link, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, link)
            except OSError:
                continue


def _ensure_blackboard_skill_links(
    home: Path, *, skill_target: str = _CONTAINER_BLACKBOARD_SKILL,
) -> None:
    """Expose the current blackboard skill inside an isolated worker HOME.

    Container runs normally pass a run-workspace target rather than the image copy,
    so pi's skill auto-discovery follows the same source-versioned implementation
    as ``DSWARM_BLACKBOARD_SCRIPT``.  The image target remains a safe fallback for
    installed deployments without an adjacent source checkout.
    """
    for rel in _BLACKBOARD_SKILL_LINKS:
        link = home / rel
        try:
            if link.is_symlink():
                if os.readlink(link) == skill_target:
                    continue
                link.unlink()
            elif link.exists():
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(skill_target, target_is_directory=True)
        except OSError:
            continue


_CONTAINER_DIRECTION_CONFIG = "/opt/dswarm/direction"
_CONTAINER_DIRECTION_SKILLS = f"{_CONTAINER_DIRECTION_CONFIG}/skills"
_CONTAINER_DIRECTION_PROMPT = f"{_CONTAINER_DIRECTION_CONFIG}/prompt.md"

# BTFly bakes a per-category pi skill into the image default HOME
# (~/.pi/agent/skills/<category>). The dswarm worker HOME is isolated per
# worker, so link the category skill in by its image path (the worker container
# has it; the host only needs the name).
_BTFLY_CATEGORY_SKILL = {
    "web": "web",
    "pwn": "pwn",
    "rev": "reverse",
    "crypto": "crypto",
    "misc": "misc",
    "forensics": "forensics",
    "aisec": "web",  # aisec image is built on the web toolchain base
}


def _direction_from_profile_id(profile_id: str) -> str:
    """pi-web → web; pi-worker (the generic fallback) → ''."""
    pid = (profile_id or "").strip()
    if pid.startswith("pi-") and pid != "pi-worker":
        return pid[len("pi-"):]
    return ""


def _repo_direction_root() -> "Optional[Path]":
    # repo root = dswarm/swarm/swarm.py → parents[2]; the direction build
    # context mirrors what gets baked into the worker image.
    root = Path(__file__).resolve().parent.parent.parent / "docker" / "worker-pi" / "directions"
    return root if root.exists() else None


def _ensure_direction_links(
    home: Path,
    direction: str,
    *,
    skill_target_root: str = _CONTAINER_DIRECTION_SKILLS,
) -> None:
    """Expose the image-baked direction skill set inside an isolated worker HOME.

    Skill NAMES are enumerated from the repo build context (the same files the
    Dockerfile bakes into /opt/dswarm/direction/skills), and each is symlinked
    into ~/.pi/agent/skills/ so pi's one-level skill discovery sees them. No-op
    when the repo context is absent (installed deployments rely on the image's
    default HOME copy for bare docker runs).
    """
    root = _repo_direction_root()
    if root is None:
        return
    skills_src = root / direction / "skills"
    if not skills_src.is_dir():
        return
    for child in sorted(skills_src.iterdir()):
        # Skill directories AND loose root reference files (e.g. reverse-skill's
        # tool-index.md) are exposed so ../ references resolve in the worker HOME.
        if not (child.is_dir() or child.is_file()):
            continue
        if child.name.startswith("."):
            # keep .gitkeep-style placeholders out of the worker skill dir
            continue
        name = child.name
        link = home / ".pi" / "agent" / "skills" / name
        target = f"{skill_target_root}/{name}"
        try:
            if link.is_symlink():
                if os.readlink(link) == target:
                    continue
                link.unlink()
            elif link.exists():
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=child.is_dir())
        except OSError:
            continue
    # The BTFly base image also bakes its category skill into the default HOME.
    # Link it so the isolated worker can use it too (name it by category).
    category = _BTFLY_CATEGORY_SKILL.get(direction)
    if category:
        link = home / ".pi" / "agent" / "skills" / category
        target = f"/home/ctf/.pi/agent/skills/{category}"
        try:
            if link.is_symlink():
                if os.readlink(link) == target:
                    return
                link.unlink()
            elif link.exists():
                return
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pass


# Base (direction-agnostic) skills baked into every worker image default
# HOME (docker/worker-pi/base-skills -> /home/ctf/.pi/agent/skills/).
# Linked into isolated worker HOMEs unconditionally so triage/writeup
# helpers are available to every profile, including the generic pi-worker.
_BASE_SKILLS = ("solve-challenge", "ctf-writeup")


def _ensure_base_skill_links(home: Path) -> None:
    """Expose the image-baked base skills inside an isolated worker HOME."""
    for name in _BASE_SKILLS:
        link = home / ".pi" / "agent" / "skills" / name
        target = f"/home/ctf/.pi/agent/skills/{name}"
        try:
            if link.is_symlink():
                if os.readlink(link) == target:
                    continue
                link.unlink()
            elif link.exists():
                continue
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            continue

# ── shared health-probe cache ────────────────────────────────────────────────
# `Swarm._healthy_engines` shells a REAL one-turn CLI hello per engine on EVERY
# dispatch (subprocess.run, up to a 60s/150s timeout + a retry, run SERIALLY).
# That whole-roster probe sits on the critical path BEFORE the first worker spawns
# and the first RUN_STARTED reaches the deck — so a fresh dispatch "freezes for ~a
# minute" with the rail stuck on WORKER 0/0 until it returns.
#
# This module-level cache memoizes the (ok, detail) verdict per probe-identity
# (engine + role + resolved account) for a short TTL, so a SECOND dispatch — or a
# sibling run in the same server, or a re-bootstrap round — reuses the roster we
# JUST verified instead of re-shelling every CLI. A successful probe is the strong
# signal (auth+quota+backend all round-tripped seconds ago); a FAILURE is cached
# too but for a shorter window so a recovered engine rejoins quickly. monotonic
# clock only (Date.now is banned in this codebase). Keyed process-wide so it
# survives across Swarm instances; bounded by natural roster size (a handful of
# engines × roles), so no eviction needed.
