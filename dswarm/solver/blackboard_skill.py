"""Blackboard skill discovery, materialization, and deployed-copy sync."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Optional

# The dswarm-blackboard skill ships in the repo at <repo>/skills/dswarm-blackboard/.
# This module lives at <repo>/dswarm/solver/, so the repo root is two parents up.
_REPO_BLACKBOARD_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "skills" / "dswarm-blackboard" / "blackboard.py"
)


def _repo_blackboard_script() -> Optional[str]:
    """Absolute path to the IN-REPO blackboard skill if we're running from a source
    checkout, else None.

    A non-containerized worker invokes the skill purely as
    `python3 "$DSWARM_BLACKBOARD_SCRIPT" <subcommand>` 鈥?so whatever path we hand it
    is the ONLY copy that runs. Historically that pointed at the DEPLOYED copy under
    ~/.claude or ~/.agents (installed once by scripts/install_blackboard_skill.sh),
    which silently rotted whenever the repo skill changed: run-75378 shipped workers a
    skill missing the entire G0-G4 + lifecycle landing (stale dedupe_key, no
    _retired_fact_seqs filter, no dispatch_state fence), half-defeating the run-75377
    echo-dedup fix. Pointing source runs straight at the repo copy removes that drift
    class entirely 鈥?there is no second copy to fall out of sync."""
    p = _REPO_BLACKBOARD_SCRIPT
    try:
        return str(p) if p.is_file() else None
    except OSError:
        return None


def materialize_runtime_blackboard_skill(workspace_root: str | Path) -> Optional[Path]:
    """Copy the current source skill into the run workspace for container workers.

    A container image is long-lived while the checkout changes on every patch.  The
    run workspace is already bind-mounted into every worker, so a small runtime
    copy gives both the explicit ``DSWARM_BLACKBOARD_SCRIPT`` command and pi's skill
    auto-discovery one authoritative, version-matched implementation.  This avoids
    trusting an image-baked copy that may predate the current CLI protocol.

    The helper is intentionally idempotent: several workers may prepare the same
    run concurrently, and an existing byte-identical file is left alone.
    """
    src_text = _repo_blackboard_script()
    if src_text is None:
        return None
    source_dir = Path(src_text).parent
    sources = {
        "blackboard.py": Path(src_text),
        "SKILL.md": source_dir / "SKILL.md",
    }
    if not all(src.is_file() for src in sources.values()):
        return None
    target = Path(workspace_root) / ".dswarm_runtime" / "dswarm-blackboard"
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name, src in sources.items():
            dst = target / name
            payload = src.read_bytes()
            if dst.is_file() and _file_sha256(dst) == hashlib.sha256(payload).hexdigest():
                continue
            tmp = target / f".{name}.staging.{os.getpid()}.{time.time_ns()}"
            try:
                tmp.write_bytes(payload)
                if name == "blackboard.py":
                    tmp.chmod(0o755)
                os.replace(tmp, dst)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        return target
    except OSError:
        return None


# The user-scope copy pi auto-discovers (~/.pi/agent/skills), installed once by
# scripts/install_blackboard_skill.sh.
_DEPLOYED_BLACKBOARD_SCRIPTS = (
    "~/.pi/agent/skills/dswarm-blackboard/blackboard.py",
)


def _file_sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def sync_deployed_blackboard_skills() -> list[dict]:
    """SAFETY NET (run once at swarm launch): reconcile the DEPLOYED user-scope skill
    copies with the in-repo source.

    Source runs invoke the repo skill directly (see _repo_blackboard_script), so the
    deployed copies don't gate THAT path. But a worker CLI also AUTO-DISCOVERS the
    skill from its user-scope dir for any unprompted `dswarm-blackboard` use, and an
    installed (non-source) deployment relies on the deployed copy outright. Those
    copies are installed once and then rot whenever the repo skill changes (run-75378:
    deployed skill missing the entire G0-G4 + lifecycle landing). When a repo source is
    present we treat it as truth and overwrite any stale/missing deployed copy.

    Returns one report row per deployed target: {path, status, ...} where status is
      'synced'        鈥?was stale/missing, overwritten from repo (action taken)
      'ok'            鈥?already byte-identical to repo
      'no-source'     鈥?no in-repo source (installed deployment); nothing to compare
      'error'         鈥?copy failed (details in 'error')
    The caller logs this + emits a board delta so a silent drift can't recur unseen."""
    src = _repo_blackboard_script()
    if src is None:
        # Installed deployment with no adjacent repo skill: the deployed copy IS the
        # source of truth, kept fresh by the package install, so there's nothing to
        # reconcile against.
        return [{"path": os.path.expanduser(t), "status": "no-source"}
                for t in _DEPLOYED_BLACKBOARD_SCRIPTS]
    src_path = Path(src)
    src_hash = _file_sha256(src_path)
    rows: list[dict] = []
    for target in _DEPLOYED_BLACKBOARD_SCRIPTS:
        dest = Path(os.path.expanduser(target))
        if dest.resolve() == src_path.resolve():
            # Deployed dir is a symlink (or the same file) into the repo 鈥?already
            # impossible to drift; nothing to do.
            rows.append({"path": str(dest), "status": "ok"})
            continue
        dest_hash = _file_sha256(dest)
        if dest_hash == src_hash:
            rows.append({"path": str(dest), "status": "ok"})
            continue
        # Stale or missing 鈫?overwrite from repo. Also refresh SKILL.md alongside it so
        # the discovered skill's docs and code move together.
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest)
            try:
                os.chmod(dest, 0o755)
            except OSError:
                pass
            skill_md = src_path.parent / "SKILL.md"
            if skill_md.is_file():
                shutil.copy2(skill_md, dest.parent / "SKILL.md")
            rows.append({
                "path": str(dest),
                "status": "synced",
                "was": "missing" if dest_hash is None else f"stale({dest_hash[:12]})",
                "now": (src_hash or "")[:12],
            })
        except OSError as e:
            rows.append({"path": str(dest), "status": "error", "error": str(e)})
    return rows
