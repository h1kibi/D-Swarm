#!/usr/bin/env python3
"""dswarm-blackboard — a worker's CLI to the shared solve graph (the blackboard).

A swarm worker (claude / codex) calls this to coordinate with its teammates
through the shared, append-only SQLite blackboard — NOT by talking to them
directly (stigmergy). The board holds:
  - facts      : confirmed, objective findings (with verified/candidate status)
  - dead-ends  : ruled-out directions (so nobody retries them)
  - intents    : declared exploration directions, claimable atomically

The DB path comes from $DSWARM_BLACKBOARD_DB (the coordinator sets it per worker).

Usage:
  blackboard.py read-facts [--verified-only]   # what teammates confirmed
  blackboard.py read-review                    # review-arbiter challenges/directives
  blackboard.py read-routes                    # suppressed/reopened routes
  blackboard.py read-branches                  # branch hypotheses to split/verify
  blackboard.py read-deadends                  # paths already ruled out — AVOID
  blackboard.py read-flags                     # flags already found (multi-flag) — don't re-hunt
  blackboard.py list-intents                   # open directions you can claim
  blackboard.py write-fact "<text>" [--verified --artifact PATH]  # own worker file is materialized to shared CAS
  blackboard.py mark-deadend "<reason>"
  blackboard.py claim <intent_id>              # atomic; prints WON or LOST
  blackboard.py register-cleanup <type:target>  # typed action only; never a shell command
  blackboard.py read-cleanups                    # cleanup status (targets are redacted)

This script is intentionally dependency-free (stdlib sqlite3 only) so it runs in
any worker container without setup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

_ACTOR = os.environ.get("DSWARM_WORKER_ID", "worker")
_INTENT_ID = os.environ.get("DSWARM_INTENT_ID", "").strip()


def _http_url() -> str:
    return os.environ.get("DSWARM_BLACKBOARD_URL", "").strip()


def _http_run(raw_args: list[str]) -> None:
    base = _http_url().rstrip("/")
    run_id = os.environ.get("DSWARM_BLACKBOARD_RUN_ID", "").strip()
    token = os.environ.get("DSWARM_BLACKBOARD_TOKEN", "").strip()
    if not run_id:
        print("ERROR: DSWARM_BLACKBOARD_RUN_ID is required in HTTP blackboard mode",
              file=sys.stderr)
        sys.exit(2)
    if not raw_args:
        print("ERROR: no blackboard command supplied", file=sys.stderr)
        sys.exit(2)
    body = {"cmd": raw_args[0], "args": raw_args[1:]}
    req = urllib.request.Request(
        f"{base}/{run_id}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Blackboard-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        print(f"ERROR: blackboard HTTP call failed: {exc}", file=sys.stderr)
        sys.exit(2)
    if data.get("stderr"):
        print(str(data["stderr"]), end="", file=sys.stderr)
    if data.get("stdout"):
        print(str(data["stdout"]), end="")
    if not data.get("ok"):
        sys.exit(1)


def _windows_path_for_posix(path: str) -> str:
    """Translate a host Windows path for POSIX Python launched from WSL/Git Bash.

    Local pi workers may invoke this script from a POSIX shell while the
    coordinator exported DSWARM_BLACKBOARD_DB as ``C:\\...``. sqlite3 treats that
    literally as a relative filename (or creates an empty DB), so read-facts later
    crashes with "no such table: events". Prefer the real mounted path instead.
    """
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not m:
        return path
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    candidates = [f"/mnt/{drive}/{rest}", f"/{drive}/{rest}"]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    # If the DB file does not exist yet but the parent does, still choose the
    # platform mount path instead of letting sqlite create a bogus C:... file in
    # the worker cwd.
    for cand in candidates:
        parent = os.path.dirname(cand)
        if parent and os.path.isdir(parent):
            return cand
    return path


def _db_path() -> str:
    p = os.environ.get("DSWARM_BLACKBOARD_DB", "")
    if not p:
        # fallback: a path file dropped in cwd by the coordinator
        for cand in (".dswarm_blackboard", "shared_graph.db"):
            if os.path.isfile(cand):
                return cand
        print("ERROR: no blackboard DB ($DSWARM_BLACKBOARD_DB unset and no "
              "shared_graph.db in cwd)", file=sys.stderr)
        sys.exit(2)
    if os.name != "nt":
        p = _windows_path_for_posix(p)
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _workspace_root_from_db() -> Path:
    """Infer the run workspace root from the configured graph DB path."""
    db = Path(_db_path()).expanduser().resolve()
    if db.parent.name == "graph":
        return db.parent.parent
    return db.parent


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_BRACE_FLAG_LITERAL = re.compile(r"[A-Za-z0-9_]{0,15}\{[^}]{1,200}\}")
_SECRET_LITERAL_RE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,})",
    re.IGNORECASE,
)


_CLEANUP_ACTION_TYPES = {
    "remove_artifact", "stop_listener", "close_session", "revoke_credential",
}
_CLEANUP_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")

def _parse_cleanup_spec(spec: str) -> tuple[str, str]:
    """Parse a typed cleanup spec; raw shell commands are intentionally rejected."""
    text = str(spec or "").strip()
    if ":" not in text:
        raise ValueError("cleanup must be <action_type>:<target>")
    action_type, target = text.split(":", 1)
    action_type = action_type.strip().lower()
    target = target.strip()
    if action_type not in _CLEANUP_ACTION_TYPES:
        raise ValueError("unsupported cleanup action type")
    if not _CLEANUP_TARGET_RE.fullmatch(target):
        raise ValueError("cleanup target contains unsupported characters")
    if action_type == "remove_artifact":
        parts = target.replace("\\", "/").split("/")
        if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target):
            raise ValueError("artifact target must be run-relative")
        if any(part in {"", ".", ".."} for part in parts) or parts[0] != "workers":
            raise ValueError("artifact target must stay under workers/")
    return action_type, target


def _validated_shared_artifact(path_text: str) -> tuple[str | None, str]:
    """Return a CAS digest only for a real shared/objects artifact.

    A worker-local file is not accepted by this helper itself.  ``write-fact`` may
    safely materialize a file from *its own worker directory* through
    ``_prepare_evidence_artifact`` below; callers outside that controlled path
    remain candidates rather than gaining verified provenance.
    """
    if not (path_text or "").strip():
        return None, "--artifact is required for --verified"
    try:
        resolved = Path(path_text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "artifact does not exist"
    if not resolved.is_file():
        return None, "artifact is not a regular file"

    cas_root = (_workspace_root_from_db() / "shared" / "objects").resolve()
    try:
        resolved.relative_to(cas_root)
    except ValueError:
        return None, "artifact is outside the run shared CAS"

    try:
        digest = _sha256_file(resolved)
    except OSError:
        return None, "artifact could not be read"
    expected = (cas_root / digest[:2] / digest[2:4] / digest).resolve()
    if resolved != expected:
        return None, "artifact is not a canonical shared CAS object"
    return digest, ""


def _worker_root_for_cwd(workspace: Path) -> Path:
    """Return the current worker's directory, never a sibling worker's tree."""
    cwd = Path.cwd().resolve()
    workers = (workspace / "workers").resolve()
    try:
        rel = cwd.relative_to(workers)
    except ValueError:
        # Old/local test layouts do not always use workspace/workers/<id>.  The
        # process cwd is still a safe boundary: it cannot name a parent or sibling.
        return cwd
    return workers / rel.parts[0] if rel.parts else cwd


def _sanitize_artifact_text(raw: bytes) -> tuple[bytes, bool]:
    """Redact flag-like and secret-like literals before shared-CAS persistence."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Binary evidence is preserved byte-for-byte.  It is not normally a PoC,
        # but changing opaque bytes would make its claimed digest dishonest.
        return raw, False
    sanitized = _BRACE_FLAG_LITERAL.sub("<PRIOR_FLAG>", text)
    sanitized = _SECRET_LITERAL_RE.sub("<SECRET>", sanitized)
    return sanitized.encode("utf-8"), sanitized != text


def _prepare_evidence_artifact(path_text: str) -> tuple[str | None, str, dict | None]:
    """Resolve a canonical artifact or materialize a file owned by this worker.

    This is the missing bridge between the worker-facing ``--artifact ./shared/x``
    convention and the coordinator's shared CAS.  A path outside the current
    worker's directory is never copied; that keeps a worker from laundering a
    sibling/host file into verified evidence.
    """
    digest, reason = _validated_shared_artifact(path_text)
    if digest:
        try:
            resolved = Path(path_text).expanduser().resolve(strict=True)
            rel = resolved.relative_to(_workspace_root_from_db()).as_posix()
        except (OSError, RuntimeError, ValueError):
            rel = ""
        return digest, "", {
            "name": Path(path_text).name,
            "path": rel,
            "sanitized": False,
        }

    try:
        src = Path(path_text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "artifact does not exist", None
    if not src.is_file():
        return None, "artifact is not a regular file", None

    workspace = _workspace_root_from_db().resolve()
    worker_root = _worker_root_for_cwd(workspace)
    try:
        src.relative_to(worker_root)
    except ValueError:
        return None, "artifact is outside this worker cwd", None

    try:
        raw = src.read_bytes()
        persisted, sanitized = _sanitize_artifact_text(raw)
        digest = hashlib.sha256(persisted).hexdigest()
        obj = workspace / "shared" / "objects" / digest[:2] / digest[2:4] / digest
        obj.parent.mkdir(parents=True, exist_ok=True)
        if not obj.exists():
            tmp = obj.parent / f".{obj.name}.staging.{os.getpid()}.{time.time_ns()}"
            try:
                tmp.write_bytes(persisted)
                os.replace(tmp, obj)
            except FileExistsError:
                pass
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        rel = obj.relative_to(workspace).as_posix()
        name = src.name
        links = workspace / "shared" / "links"
        links.mkdir(parents=True, exist_ok=True)
        link = links / name
        tmp_link = links / f".{name}.staging.{os.getpid()}.{time.time_ns()}"
        try:
            target = os.path.relpath(obj, start=links)
            tmp_link.symlink_to(target)
            os.replace(tmp_link, link)
        except OSError:
            try:
                tmp_link.unlink()
            except FileNotFoundError:
                pass
        row = {
            "ts": time.time(), "kind": "worker-evidence",
            "status": "quarantined" if sanitized else "directional",
            "name": name, "sha256": digest, "path": rel,
            "source_worker": _ACTOR,
        }
        with (workspace / "shared" / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return digest, "", {"name": name, "path": rel, "sanitized": sanitized}
    except OSError:
        return None, "artifact could not be materialized into the run shared CAS", None


def _register_artifact_poc(c: sqlite3.Connection, cid: str, *, artifact_id: str,
                           artifact: dict, intent_id: str | None) -> bool:
    """Register evidence artifacts as *directional* PoCs without inventing a command.

    ``POC_SAVE`` remains the preferred route for an executable PoC because it
    carries an actual entry command/status.  The direct blackboard command has no
    such fields, so it records an honest directional record rather than falsely
    claiming a script is runnable.  This keeps the artifact discoverable in the
    graph and prevents the pocs projection from silently staying empty.
    """
    if not _has_table(c, "pocs"):
        return False
    path = str(artifact.get("path") or "")
    if not path:
        return False
    name = str(artifact.get("name") or Path(path).name)
    sanitized = bool(artifact.get("sanitized"))
    status = "quarantined" if sanitized else "directional"
    note = ("Auto-registered from write-fact --artifact; inspect before reuse"
            + ("; flag/secret-like literals redacted" if sanitized else ""))
    entry = "(no entry command supplied; inspect artifact before reuse)"
    poc_id = f"poc-{artifact_id[:12]}"
    payload = json.dumps({
        "poc_id": poc_id, "intent_id": intent_id, "name": name, "path": path,
        "entry_command": entry, "status": status, "note": note,
    })
    # More than one fact may cite the same artifact.  The event is deduplicated,
    # but that must never roll back a newly inserted fact merely because its PoC
    # projection already exists.
    cur = c.execute(
        "INSERT OR IGNORE INTO events (ts, challenge_id, actor, kind, payload, artifact_id, "
        "verified, confidence, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (time.time(), cid, _ACTOR, "poc_saved", payload, artifact_id, 0, 1.0,
         f"poc::{poc_id}::{status}::{entry}::{note}"),
    )
    seq = int(cur.lastrowid or 0)
    c.execute(
        "INSERT INTO pocs "
        "(poc_id, challenge_id, intent_id, name, path, artifact_id, entry_command, "
        " status, note, created_seq) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(poc_id) DO UPDATE SET "
        " intent_id=excluded.intent_id, name=excluded.name, path=excluded.path, "
        " artifact_id=excluded.artifact_id, entry_command=excluded.entry_command, "
        " status=excluded.status, note=excluded.note",
        (poc_id, cid, intent_id, name, path, artifact_id, entry, status, note, seq),
    )
    return True


def _has_column(c: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return False
    return col in cols


def _has_table(c: sqlite3.Connection, table: str) -> bool:
    try:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _challenge_id(c: sqlite3.Connection) -> str:
    # Prefer the coordinator-provided scope. A fresh graph has no event or intent
    # row to infer it from, so writing with an empty challenge_id would hide the
    # fact from the challenge-scoped fact_effective view.
    explicit = (os.environ.get("DSWARM_CHALLENGE_ID") or "").strip()
    if explicit:
        return explicit

    # Pick the first NON-EMPTY challenge_id. Some events are written with an empty
    # challenge_id, and a bare `LIMIT 1` could grab one of those — then claim's
    # `WHERE challenge_id=?` matched nothing and always returned LOST even for an
    # open intent. Fall back to the intents table (those rows reliably carry the run
    # id), then to "" as a last resort.
    row = c.execute(
        "SELECT challenge_id FROM events "
        "WHERE challenge_id IS NOT NULL AND challenge_id != '' LIMIT 1"
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = c.execute(
        "SELECT challenge_id FROM intents "
        "WHERE challenge_id IS NOT NULL AND challenge_id != '' LIMIT 1"
    ).fetchone()
    return row[0] if row and row[0] else ""


def register_cleanup(spec: str) -> None:
    """Register one typed action in the append-only event log.

    The target is never executed by this worker script.  It is stored privately in
    the graph for the coordinator's run-scoped allowlisted executor.
    """
    c = _conn()
    try:
        action_type, target = _parse_cleanup_spec(spec)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    cid = _challenge_id(c)
    intent_id = _INTENT_ID
    idem = f"{_ACTOR}:{action_type}:{target}:{intent_id}"
    material = "|".join((cid, _ACTOR, action_type, target, intent_id, idem))
    action_id = "cleanup-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    # Keep this fallback compatible with a pre-migration graph while still using
    # the same append-only event contract as the coordinator implementation.
    c.execute("CREATE TABLE IF NOT EXISTS cleanup_actions ("
              "action_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, "
              "action_type TEXT NOT NULL, target TEXT NOT NULL, actor TEXT NOT NULL, "
              "owner_key TEXT NOT NULL, intent_id TEXT, poc_id TEXT, "
              "idempotency_key TEXT, registration_seq INTEGER NOT NULL, "
              "status TEXT NOT NULL DEFAULT 'registered', execution_seq INTEGER, "
              "failure_reason TEXT, result TEXT)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cleanup_idempotency "
              "ON cleanup_actions(challenge_id, idempotency_key) "
              "WHERE idempotency_key IS NOT NULL AND idempotency_key != ''")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cleanup_registration "
              "ON cleanup_actions(challenge_id, registration_seq)")
    row = c.execute("SELECT action_id, status FROM cleanup_actions "
                    "WHERE challenge_id=? AND idempotency_key=?", (cid, idem)).fetchone()
    if row:
        print(json.dumps({"action_id": row[0], "status": row[1], "action_type": action_type,
                          "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                          "target_length": len(target)}, sort_keys=True))
        return
    payload = json.dumps({"action_id": action_id, "action_type": action_type,
                          "target": target, "actor": _ACTOR, "owner_key": _ACTOR,
                          "intent_id": intent_id, "poc_id": "",
                          "idempotency_key": idem}, ensure_ascii=False)
    cur = c.execute("INSERT OR IGNORE INTO events "
                    "(ts, challenge_id, actor, kind, payload, verified, confidence, dedupe_key) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), cid, _ACTOR, "cleanup_action_registered", payload, 0, 1.0,
                     "cleanup-register::" + action_id))
    seq = int(cur.lastrowid or 0)
    c.execute("INSERT OR IGNORE INTO cleanup_actions "
              "(action_id, challenge_id, action_type, target, actor, owner_key, intent_id, "
              "poc_id, idempotency_key, registration_seq, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (action_id, cid, action_type, target, _ACTOR, _ACTOR, intent_id or None,
               None, idem, seq, "registered"))
    c.commit()
    print(json.dumps({"action_id": action_id, "status": "registered",
                      "action_type": action_type,
                      "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                      "target_length": len(target)}, sort_keys=True))


def read_cleanups() -> None:
    c = _conn()
    if not _table_exists(c, "cleanup_actions"):
        print("(no cleanup actions registered)")
        return
    cid = _challenge_id(c)
    rows = c.execute("SELECT action_id, action_type, actor, status, registration_seq, "
                     "execution_seq, failure_reason, target FROM cleanup_actions "
                     "WHERE challenge_id=? ORDER BY registration_seq", (cid,)).fetchall()
    if not rows:
        print("(no cleanup actions registered)")
        return
    for action_id, action_type, actor, status, reg, exe, reason, target in rows:
        digest = hashlib.sha256(str(target).encode()).hexdigest()[:16]
        if reason:
            reason_text = str(reason)[:512]
            failure = (f" failure_digest={hashlib.sha256(reason_text.encode()).hexdigest()[:16]}"
                       f" failure_length={len(reason_text)}")
        else:
            failure = ""
        print(f"{action_id} {action_type} actor={actor} status={status} "
              f"registered_seq={reg} execution_seq={exe or ''} target_digest={digest}{failure}")


def read_facts(verified_only: bool) -> None:
    c = _conn()
    # M3 makes fact_effective the only safe read model: raw events contain the
    # immutable candidate row while promotion/lifecycle state lives in later
    # events. Never silently fall back to events.verified on an un-migrated DB.
    view = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
    ).fetchone()
    if view is None:
        print("(blackboard requires explicit M3 database migration before facts can be read)")
        return
    cid = _challenge_id(c)
    sql = (
        "SELECT fact_text, fact_source, verified, confidence, retired, summary, state "
        "FROM fact_effective WHERE challenge_id=?"
    )
    args: list[object] = [cid]
    if verified_only:
        sql += " AND verified=1"
    sql += " ORDER BY fact_seq"
    try:
        rows = c.execute(sql, args).fetchall()
    except sqlite3.DatabaseError as exc:
        print(f"ERROR: effective fact view unavailable: {exc}", file=sys.stderr)
        return
    out = []
    for fact, source, verified, conf, retired, summary, state in rows:
        if int(retired or 0):
            continue
        out.append({
            "fact": str(fact or ""), "source": str(source or ""),
            "verified": bool(verified), "confidence": float(conf or 0.0),
            "summary": str(summary or ""), "state": str(state or ""),
        })
    if not out:
        print("(no facts on the board yet)")
        return
    for f in out:
        tag = "VERIFIED" if f["verified"] else f"candidate({f['confidence']:.1f})"
        summary = f" — {f['summary']}" if f["summary"] else ""
        print(f"[{tag}] ({f['source']}) {f['fact']}{summary}")


def read_flags() -> None:
    """Flags teammates have already recovered. On a MULTI-FLAG challenge, read
    this before submitting so you don't re-hunt one a teammate already found —
    go after the ones NOT listed here."""
    c = _conn()
    rows = c.execute(
        "SELECT payload, kind FROM events "
        "WHERE kind IN ('flag_found','flag_invalidated') ORDER BY seq").fetchall()
    found: list[str] = []
    for payload, kind in rows:
        f = (json.loads(payload) or {}).get("flag")
        if not f:
            continue
        if kind == "flag_found" and f not in found:
            found.append(f)
        elif kind == "flag_invalidated" and f in found:
            found.remove(f)  # a false positive was retracted
    if not found:
        print("(no flags recovered yet — you may be the first)")
        return
    print("# Flags already recovered by the team — do NOT re-submit these:")
    for f in found:
        print(f"- {f}")


def read_deadends() -> None:
    c = _conn()
    rows = c.execute(
        "SELECT payload FROM events WHERE kind='dead_end' ORDER BY seq").fetchall()
    if not rows:
        print("(no dead-ends recorded — nothing ruled out yet)")
        return
    print("# Dead-ends — directions already ruled out, DO NOT retry these:")
    for (payload,) in rows:
        d = json.loads(payload)
        print(f"- {d.get('reason', '')}")


def _table_exists(c: sqlite3.Connection, table: str) -> bool:
    row = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _event_payload_by_seq(c: sqlite3.Connection, seq: int) -> dict:
    row = c.execute("SELECT payload FROM events WHERE seq=?", (int(seq),)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0]) or {}
    except Exception:
        return {}


def read_routes() -> None:
    c = _conn()
    if not _table_exists(c, "routes"):
        print("(this board has no route review table yet)")
        return
    rows = c.execute(
        "SELECT route_hash, label, status, reason, until_policy "
        "FROM routes ORDER BY COALESCE(suppressed_seq, reopened_seq, 0), route_hash"
    ).fetchall()
    if not rows:
        print("(no reviewed routes)")
        return
    print("# Reviewed routes")
    for route_hash, label, status, reason, until_policy in rows:
        tag = "SUPPRESSED" if status == "suppressed" else "OPEN"
        extra = f" until={until_policy}" if until_policy else ""
        print(f"[{tag}] {route_hash} ({label or route_hash}){extra}: {reason or ''}")


def read_branches() -> None:
    c = _conn()
    if not _table_exists(c, "branches"):
        print("(this board has no branch review table yet)")
        return
    rows = c.execute(
        "SELECT branch_id, parent_id, title, assumption, prove_or_disprove, status "
        "FROM branches ORDER BY created_seq, branch_id"
    ).fetchall()
    if not rows:
        print("(no branch hypotheses)")
        return
    print("# Review branches — prove/disprove separately")
    for branch_id, parent_id, title, assumption, pod, status in rows:
        parent = f" parent={parent_id}" if parent_id else ""
        print(f"- [{status or 'open'}] {branch_id}{parent}: {title or assumption}")
        if assumption:
            print(f"  assumption: {assumption}")
        if pod:
            print(f"  prove/disprove: {pod}")


def read_review() -> None:
    c = _conn()
    print("# Review-Arbiter state")

    rows = c.execute(
        "SELECT seq, actor, payload FROM events "
        "WHERE kind='review_finding' ORDER BY seq DESC LIMIT 12"
    ).fetchall()
    if rows:
        print("\n## Findings")
        for seq, actor, payload in reversed(rows):
            d = json.loads(payload)
            sev = d.get("severity", "info")
            kind = d.get("kind", "finding")
            route = f" route={d.get('route_hash')}" if d.get("route_hash") else ""
            print(f"- #{seq} [{sev}/{kind}] {actor}:{route} {d.get('summary', '')}")

    challenged: list[tuple] = []
    view = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
    ).fetchone()
    if view is not None:
        cid = _challenge_id(c)
        challenged = c.execute(
            "SELECT f.fact_seq, f.fact_text, "
            "json_extract(e.payload, '$.reason'), "
            "json_extract(e.payload, '$.verification_intent_id') "
            "FROM fact_effective f "
            "LEFT JOIN events e ON e.seq=("
            "SELECT MAX(e2.seq) FROM events e2 "
            "WHERE e2.challenge_id=f.challenge_id "
            "AND e2.kind='fact_challenged' "
            "AND json_extract(e2.payload, '$.fact_seq')=f.fact_seq) "
            "WHERE f.challenge_id=? AND f.state='challenged' AND f.retired=0 "
            "ORDER BY e.seq",
            (cid,),
        ).fetchall()
    if challenged:
        print("\n## Challenged facts — do NOT rely on these until verified")
        for fact_seq, fact, reason, verification_intent_id in challenged:
            print(f"- fact #{fact_seq}: {fact or ''}")
            print(f"  reason: {reason or ''}")
            if verification_intent_id:
                print(f"  verify intent: {verification_intent_id}")

    dirs = c.execute(
        "SELECT seq, actor, payload FROM events "
        "WHERE kind='coordinator_directive' ORDER BY seq DESC LIMIT 8"
    ).fetchall()
    if dirs:
        print("\n## Coordinator directives")
        for seq, actor, payload in reversed(dirs):
            d = json.loads(payload)
            print(f"- #{seq} {actor} {d.get('action', 'note')}: {d.get('directive', '')}")

    print("\n## Routes")
    read_routes()
    print("\n## Branches")
    read_branches()


def list_intents() -> None:
    c = _conn()
    cols = {row[1] for row in c.execute("PRAGMA table_info(intents)").fetchall()}
    select_cols = ["intent_id", "goal"]
    for optional in ("worker_class", "route_hash", "branch_id"):
        select_cols.append(optional if optional in cols else "''")
    # only dispatch_state='active' intents are claimable; resume/retired/closed are
    # held back (the column is absent on old DBs → no filter, same as before).
    where = "status='open'"
    if "dispatch_state" in cols:
        where += " AND dispatch_state='active'"
    rows = c.execute(
        "SELECT " + ",".join(select_cols) +
        f" FROM intents WHERE {where} ORDER BY created_seq"
    ).fetchall()
    if not rows:
        print("(no open intents)")
        return
    print("# Open intents you can claim:")
    for iid, goal, worker_class, route_hash, branch_id in rows:
        meta = []
        if worker_class:
            meta.append(f"class={worker_class}")
        if route_hash:
            meta.append(f"route={route_hash}")
        if branch_id:
            meta.append(f"branch={branch_id}")
        suffix = f" [{' '.join(meta)}]" if meta else ""
        print(f"- {iid}: {goal}{suffix}")



def write_fact(text: str, verified: bool, artifact: str = "") -> None:
    c = _conn()
    cid = _challenge_id(c)
    requested_verified = bool(verified)
    artifact_id: str | None = None
    artifact_meta: dict | None = None
    downgrade_reason = ""
    if artifact:
        artifact_id, downgrade_reason, artifact_meta = _prepare_evidence_artifact(artifact)
    if verified and not artifact_id:
        verified = False
        if not downgrade_reason:
            downgrade_reason = "--artifact is required for --verified"

    payload_obj = {"source": _ACTOR, "fact": text, "source_solver": _ACTOR,
                   "witness": None, "verifier": _ACTOR if verified else ""}
    iid = _INTENT_ID
    if iid:
        intent_row = c.execute(
            "SELECT 1 FROM intents WHERE challenge_id=? AND intent_id=? LIMIT 1",
            (cid, iid),
        ).fetchone()
        if intent_row:
            payload_obj["intent_id"] = iid
        else:
            payload_obj["orphan_intent_id"] = iid
            iid = ""
    payload = json.dumps(payload_obj)
    # dedupe on fact IDENTITY, matching SQLiteSharedGraph.add_evidence exactly so a
    # bare skill fact and its "[engine] <text>" VERIFIED_FACT marker echo collide on
    # one key (strip a leading "[engine] " tag, fold whitespace, lowercase; artifact
    # is provenance, not identity). Keep this in lockstep with _normalize_fact_identity.
    _norm = re.sub(r"^\[[a-z0-9 _.-]{1,40}\]\s*", "", text, flags=re.IGNORECASE)
    _norm = " ".join(_norm.split()).lower()
    dk = f"fact::{_ACTOR}::{_norm}"
    try:
        cur = c.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, "
            "artifact_id, verified, confidence, dedupe_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), cid, _ACTOR, "fact_added", payload, artifact_id,
             int(verified), 1.0 if verified else 0.4, dk))
        fact_seq = int(cur.lastrowid or 0)
        if iid and fact_seq > 0 and _has_table(c, "intent_products"):
            c.execute(
                "INSERT OR IGNORE INTO intent_products (intent_id, fact_seq) VALUES (?,?)",
                (iid, fact_seq))
        poc_registered = bool(artifact_id and artifact_meta and _register_artifact_poc(
            c, cid, artifact_id=artifact_id, artifact=artifact_meta, intent_id=iid or None))
        c.commit()
        suffix = "; registered directional PoC" if poc_registered else ""
        if requested_verified and not verified:
            print(f"OK wrote candidate fact (downgraded: {downgrade_reason}){suffix}")
        else:
            print(f"OK wrote {'verified' if verified else 'candidate'} fact{suffix}")
    except sqlite3.IntegrityError:
        print("OK (duplicate fact, already on board)")


def mark_deadend(reason: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    payload = json.dumps({"reason": reason})
    try:
        c.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, "
            "verified, confidence, dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), cid, _ACTOR, "dead_end", payload, 0, 1.0,
             f"deadend::{reason}"))
        c.commit()
        print("OK marked dead-end")
    except sqlite3.IntegrityError:
        print("OK (dead-end already recorded)")


def claim(intent_id: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    now = time.time()
    # a resume/retired/closed intent is NOT claimable even while status='open'
    # (the column is absent on old DBs → no extra fence, same as before).
    active_fence = " AND dispatch_state='active'" if _has_column(c, "intents", "dispatch_state") else ""
    cur = c.execute(
        "UPDATE intents SET worker=?, status='claimed', lease_until=? "
        "WHERE intent_id=? AND challenge_id=?" + active_fence +
        "  AND (status='open' OR (status='claimed' AND lease_until < ?))",
        (_ACTOR, now + 300.0, intent_id, cid, now))
    c.commit()
    if cur.rowcount == 1:
        c.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, "
            "verified, confidence) VALUES (?,?,?,?,?,?,?)",
            (now, cid, _ACTOR, "intent_claimed",
             json.dumps({"intent_id": intent_id}), 0, 1.0))
        c.commit()
        print("WON")
    else:
        print("LOST")


def _norm_activity_key(key: str) -> str:
    import re
    k = (key or "").strip().lower()
    k = re.sub(r"[\s/]+", ":", k)
    k = re.sub(r":+", ":", k).strip(":")
    return k


def claim_activity(key: str, lease_s: float = 600.0) -> None:
    """P4: claim a high-cost activity (e.g. 'nmap:8.130.96.176'). WON = go ahead;
    LOST = a teammate is already doing it, AVOID redoing."""
    c = _conn()
    cid = _challenge_id(c)
    nkey = _norm_activity_key(key)
    now = time.time()
    if not nkey:
        print("WON")
        return
    # the table may not exist on an old DB — create-if-missing, best-effort.
    c.execute(
        "CREATE TABLE IF NOT EXISTS activity_locks ("
        "activity_key TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, "
        "worker TEXT NOT NULL, lease_until REAL NOT NULL, claimed_ts REAL NOT NULL)")
    cur = c.execute(
        "INSERT INTO activity_locks "
        "(activity_key, challenge_id, worker, lease_until, claimed_ts) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(activity_key) DO UPDATE SET "
        "  worker=excluded.worker, lease_until=excluded.lease_until, "
        "  claimed_ts=excluded.claimed_ts "
        "WHERE activity_locks.lease_until < ?",
        (nkey, cid, _ACTOR, now + lease_s, now, now))
    c.commit()
    print("WON" if cur.rowcount == 1 else "LOST")


def list_activities() -> None:
    """P4: in-progress activities (lease not expired) a teammate is doing now."""
    c = _conn()
    cid = _challenge_id(c)
    now = time.time()
    try:
        rows = c.execute(
            "SELECT activity_key, worker FROM activity_locks "
            "WHERE challenge_id=? AND lease_until > ? ORDER BY claimed_ts",
            (cid, now)).fetchall()
    except Exception:
        rows = []
    if not rows:
        print("(no activities in progress)")
        return
    for key, worker in rows:
        print(f"{key}  [{worker}]")


def _normalize_resource_key(key: str) -> str:
    import re
    raw = (key or "").strip().lower()
    raw = re.sub(r"\s+", "", raw)
    raw = re.sub(r"[^a-z0-9_:@.*/-]+", "-", raw).strip("-")
    return raw[:180]


def claim_resource(resource_key: str, scope: str = "activity",
                   risk_class: str = "", lease_s: float = 600.0) -> None:
    """E: claim a shared RESOURCE (exclusive site/account/listener). WON = exclusive
    access granted; LOST = a teammate holds it — do not run conflicting work."""
    c = _conn()
    cid = _challenge_id(c)
    rkey = _normalize_resource_key(resource_key)
    now = time.time()
    if not rkey:
        print("WON")
        return
    c.execute(
        "CREATE TABLE IF NOT EXISTS resource_locks ("
        "lock_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, resource_key TEXT NOT NULL, "
        "scope TEXT NOT NULL, risk_class TEXT, status TEXT NOT NULL DEFAULT 'requested', "
        "owner_worker TEXT, owner_intent TEXT, lease_until REAL, created_seq INTEGER, "
        "released_seq INTEGER, conflict_policy TEXT NOT NULL DEFAULT 'exclusive', "
        "cooldown_s REAL NOT NULL DEFAULT 0)")
    lock_id = f"rl-{rkey}"
    # take over only if free, owned by us, or the existing lease expired (self-heal).
    cur = c.execute(
        "INSERT INTO resource_locks "
        "(lock_id, challenge_id, resource_key, scope, risk_class, status, owner_worker, lease_until) "
        "VALUES (?,?,?,?,?,'active',?,?) "
        "ON CONFLICT(lock_id) DO UPDATE SET "
        "  status='active', owner_worker=excluded.owner_worker, "
        "  scope=excluded.scope, risk_class=excluded.risk_class, lease_until=excluded.lease_until "
        "WHERE resource_locks.owner_worker=excluded.owner_worker "
        "   OR resource_locks.lease_until IS NULL OR resource_locks.lease_until < ?",
        (lock_id, cid, rkey, scope or "activity", risk_class or None, _ACTOR,
         now + lease_s, now))
    c.commit()
    if cur.rowcount == 1:
        c.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, verified, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, cid, _ACTOR, "resource_locked",
             json.dumps({"resource_key": rkey, "scope": scope, "lock_id": lock_id}), 0, 1.0))
        c.commit()
        print("WON")
    else:
        print("LOST")


def release_resource(resource_key: str) -> None:
    """E: release a resource lock this worker holds (owner-fenced, best-effort)."""
    c = _conn()
    cid = _challenge_id(c)
    rkey = _normalize_resource_key(resource_key)
    now = time.time()
    if not _has_table(c, "resource_locks") or not rkey:
        print("OK")
        return
    cur = c.execute(
        "UPDATE resource_locks SET status='released', owner_worker=NULL, lease_until=NULL "
        "WHERE challenge_id=? AND resource_key=? AND owner_worker=?",
        (cid, rkey, _ACTOR))
    c.commit()
    if cur.rowcount >= 1:
        c.execute(
            "INSERT INTO events (ts, challenge_id, actor, kind, payload, verified, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, cid, _ACTOR, "resource_released",
             json.dumps({"resource_key": rkey}), 0, 1.0))
        c.commit()
    print("OK")


def read_resource_locks() -> None:
    """E: active resource locks a teammate holds now (avoid conflicting work)."""
    c = _conn()
    cid = _challenge_id(c)
    now = time.time()
    if not _has_table(c, "resource_locks"):
        print("(no resource locks)")
        return
    rows = c.execute(
        "SELECT resource_key, scope, risk_class, owner_worker FROM resource_locks "
        "WHERE challenge_id=? AND status='active' AND owner_worker IS NOT NULL "
        "AND (lease_until IS NULL OR lease_until > ?) ORDER BY created_seq",
        (cid, now)).fetchall()
    if not rows:
        print("(no resource locks held)")
        return
    print("# Resource locks held by teammates (do NOT duplicate):")
    for rkey, scope, risk, owner in rows:
        risk_s = f" risk={risk}" if risk else ""
        print(f"- {rkey} (scope={scope}{risk_s}) [{owner}]")


def read_directives() -> None:
    """B: operator directives the swarm must respect (highest priority guidance)."""
    c = _conn()
    cid = _challenge_id(c)
    if not _has_table(c, "operator_directives"):
        print("(no operator directives)")
        return
    rows = c.execute(
        "SELECT directive_id, action, text, status, priority FROM operator_directives "
        "WHERE challenge_id=? AND status NOT IN ('superseded','expired','rejected') "
        "ORDER BY priority DESC, received_seq",
        (cid,)).fetchall()
    if not rows:
        print("(no active operator directives)")
        return
    print("# Operator directives (must respect — guidance, not evidence):")
    for did, action, text, status, priority in rows:
        print(f"- [{action}/{status}] {text}  (id={did})")


def directive_status(directive_id: str) -> None:
    """B: delivery status of one operator directive."""
    c = _conn()
    cid = _challenge_id(c)
    if not _has_table(c, "operator_directives"):
        print("(unknown)")
        return
    row = c.execute(
        "SELECT action, text, status, bound_worker FROM operator_directives "
        "WHERE challenge_id=? AND directive_id=?",
        (cid, directive_id)).fetchone()
    if not row:
        print("(unknown directive)")
        return
    action, text, status, bound = row
    bound_s = f" bound={bound}" if bound else ""
    print(f"{directive_id}: {action} status={status}{bound_s} :: {text}")


def main() -> None:
    if _http_url():
        _http_run([str(a) for a in sys.argv[1:]])
        return
    ap = argparse.ArgumentParser(prog="blackboard.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("read-facts")
    p.add_argument("--verified-only", action="store_true")
    sub.add_parser("read-review")
    sub.add_parser("read-routes")
    sub.add_parser("read-branches")
    sub.add_parser("read-deadends")
    sub.add_parser("read-flags")
    sub.add_parser("list-intents")
    p = sub.add_parser("register-cleanup")
    p.add_argument("spec")
    sub.add_parser("read-cleanups")
    p = sub.add_parser("write-fact")
    p.add_argument("text")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--artifact", default="")
    p = sub.add_parser("mark-deadend")
    p.add_argument("reason")
    p = sub.add_parser("claim")
    p.add_argument("intent_id")
    p = sub.add_parser("claim-activity")
    p.add_argument("key")
    sub.add_parser("list-activities")
    p = sub.add_parser("claim-resource")
    p.add_argument("resource_key")
    p.add_argument("--scope", default="activity")
    p.add_argument("--risk-class", default="")
    p = sub.add_parser("release-resource")
    p.add_argument("resource_key")
    sub.add_parser("read-resource-locks")
    sub.add_parser("read-directives")
    p = sub.add_parser("directive-status")
    p.add_argument("directive_id")
    args = ap.parse_args()

    if args.cmd == "read-facts":
        read_facts(args.verified_only)
    elif args.cmd == "read-review":
        read_review()
    elif args.cmd == "read-routes":
        read_routes()
    elif args.cmd == "read-branches":
        read_branches()
    elif args.cmd == "read-deadends":
        read_deadends()
    elif args.cmd == "read-flags":
        read_flags()
    elif args.cmd == "list-intents":
        list_intents()
    elif args.cmd == "register-cleanup":
        register_cleanup(args.spec)
    elif args.cmd == "read-cleanups":
        read_cleanups()
    elif args.cmd == "write-fact":
        write_fact(args.text, args.verified, args.artifact)
    elif args.cmd == "mark-deadend":
        mark_deadend(args.reason)
    elif args.cmd == "claim":
        claim(args.intent_id)
    elif args.cmd == "claim-activity":
        claim_activity(args.key)
    elif args.cmd == "list-activities":
        list_activities()
    elif args.cmd == "claim-resource":
        claim_resource(args.resource_key, scope=args.scope, risk_class=args.risk_class)
    elif args.cmd == "release-resource":
        release_resource(args.resource_key)
    elif args.cmd == "read-resource-locks":
        read_resource_locks()
    elif args.cmd == "read-directives":
        read_directives()
    elif args.cmd == "directive-status":
        directive_status(args.directive_id)


if __name__ == "__main__":
    main()
