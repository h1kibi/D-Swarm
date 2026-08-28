"""The dswarm-blackboard skill's CLI (blackboard.py) round-trips against a real
SQLiteSharedGraph DB: read facts/dead-ends, write fact, mark dead-end, claim intent.

This is what a swarm worker (claude/codex) actually runs inside its container to
coordinate through the shared board (stigmergy). We drive it as a subprocess with
DSWARM_BLACKBOARD_DB pointed at a freshly-built graph, exactly like a worker.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver import blackboard_skill
from dswarm.solver.cli_solver import CliSolver
from dswarm.solver.workspace import materialize_shared_artifact, link_shared_into_worker
from dswarm.swarm.shared_graph import SQLiteSharedGraph, _normalize_fact_identity

_SKILL = Path(__file__).resolve().parents[1] / "skills" / "dswarm-blackboard" / "blackboard.py"


def _board(tmp_path):
    ch = Challenge(id="c1", name="t", category="web")
    return SQLiteSharedGraph.open(db_path=tmp_path / "shared_graph.db", challenge=ch)


def _run(db, *args, worker="cli-pi", intent_id="", cwd=None):
    # PYTHONUTF8=1: the skill subprocess must emit UTF-8 on every host — the test
    # parent runs in UTF-8 mode (pytest -X utf8) and would otherwise try to
    # decode a GBK-cp936 console stream on a Chinese-locale Windows host.
    env = {**os.environ, "PYTHONUTF8": "1",
           "DSWARM_BLACKBOARD_DB": str(db), "DSWARM_WORKER_ID": worker,
           "DSWARM_CHALLENGE_ID": "c1"}
    # The SQLite skill tests must not inherit a developer's HTTP-mode settings.
    for key in ("DSWARM_BLACKBOARD_URL", "DSWARM_BLACKBOARD_RUN_ID",
                "DSWARM_BLACKBOARD_TOKEN"):
        env.pop(key, None)
    if intent_id:
        env["DSWARM_INTENT_ID"] = intent_id
    r = subprocess.run([sys.executable, str(_SKILL), *args],
                       capture_output=True, text=True, env=env, timeout=30, cwd=cwd,
                       # explicit utf-8: the parent may run on a GBK-locale host
                       # (pytest without -X utf8); the skill subprocess always
                       # emits UTF-8 (PYTHONUTF8=1 above).
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"blackboard.py {args} failed: {r.stderr}"
    return r.stdout


def test_skill_file_exists():
    assert _SKILL.exists(), "blackboard.py skill script missing"
    skill_md = _SKILL.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text()
    assert "dswarm-blackboard" in text and "read-deadends" in text
    assert "read-review" in text


def test_read_empty_board(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    assert "no facts" in _run(db, "read-facts").lower()
    assert "no dead-ends" in _run(db, "read-deadends").lower()


def _run_blackboard_raw(db, *args, worker="cli-pi", intent_id="", cwd=None):
    env = {**os.environ, "PYTHONUTF8": "1",
           "DSWARM_BLACKBOARD_DB": str(db), "DSWARM_WORKER_ID": worker,
           "DSWARM_CHALLENGE_ID": "c1"}
    for key in ("DSWARM_BLACKBOARD_URL", "DSWARM_BLACKBOARD_RUN_ID",
                "DSWARM_BLACKBOARD_TOKEN"):
        env.pop(key, None)
    if intent_id:
        env["DSWARM_INTENT_ID"] = intent_id
    return subprocess.run(
        [sys.executable, str(_SKILL), *args], capture_output=True, text=True,
        env=env, timeout=30, cwd=cwd, encoding="utf-8", errors="replace",
    )


def test_cleanup_cli_rejects_raw_commands_and_redacts_readback(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()

    for spec in (
        "rm -f workers/cli-pi/output.txt",
        "remove_artifact:workers/cli-pi/output.txt; whoami",
        "unknown_action:resource-1",
    ):
        result = _run_blackboard_raw(db, "register-cleanup", spec)
        assert result.returncode == 2
        assert "ERROR:" in result.stderr

    target = "workers/cli-pi/private-output.txt"
    registered = _run_blackboard_raw(
        db, "register-cleanup", f"remove_artifact:{target}", intent_id="I-cleanup",
    )
    assert registered.returncode == 0, registered.stderr
    assert target not in registered.stdout
    assert "target_digest" in registered.stdout
    assert '"target"' not in registered.stdout

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE cleanup_actions SET status='failed', failure_reason=? "
            "WHERE challenge_id=?",
            ("adapter secret token leaked in internal reason", "c1"),
        )
        conn.commit()

    readback = _run_blackboard_raw(db, "read-cleanups")
    assert readback.returncode == 0, readback.stderr
    assert target not in readback.stdout
    assert "adapter secret token leaked in internal reason" not in readback.stdout
    assert "target_digest=" in readback.stdout
    assert "failure_digest=" in readback.stdout
    assert "failure_length=" in readback.stdout


def test_write_fact_verified_without_artifact_is_downgraded(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    out = _run(db, "write-fact", "admin:admin works on /login", "--verified")
    assert "candidate fact" in out
    facts = _run(db, "read-facts")
    assert "admin:admin works on /login" in facts
    assert "candidate" in facts.lower()
    assert "admin:admin" not in _run(db, "read-facts", "--verified-only")


def test_write_fact_foreign_or_missing_artifacts_are_downgraded(tmp_path):
    workspace = tmp_path / "workspace"
    graph_dir = workspace / "graph"
    graph_dir.mkdir(parents=True)
    g = _board(graph_dir)
    db = g.db_path
    g.close()

    worker = workspace / "workers" / "cli-pi"
    worker.mkdir(parents=True)
    sibling = workspace / "workers" / "cli-codex" / "secret.txt"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("sibling scratch", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    cases = [
        (workspace / "missing.txt", "missing artifact"),
        (sibling, "sibling artifact"),
        (outside, "outside artifact"),
    ]
    for artifact, fact in cases:
        out = _run(
            db, "write-fact", fact, "--verified", "--artifact", str(artifact),
            cwd=worker,
        )
        assert "candidate fact" in out
        assert fact not in _run(db, "read-facts", "--verified-only")


def test_write_fact_materializes_own_worker_artifact_and_registers_poc(tmp_path):
    workspace = tmp_path / "workspace"
    graph_dir = workspace / "graph"
    graph_dir.mkdir(parents=True)
    g = _board(graph_dir)
    db = g.db_path
    g.close()

    worker = workspace / "workers" / "cli-pi"
    proof = worker / "shared" / "flag-poc.sh"
    proof.parent.mkdir(parents=True)
    proof.write_text("#!/bin/sh\necho proof\n", encoding="utf-8")

    out = _run(
        db, "write-fact", "proof-backed fact", "--verified", "--artifact", str(proof),
        cwd=worker,
    )
    assert "verified fact" in out
    assert "registered directional PoC" in out
    assert "proof-backed fact" in _run(db, "read-facts", "--verified-only")
    with sqlite3.connect(db) as conn:
        fact = conn.execute(
            "SELECT artifact_id, verified FROM events WHERE kind='fact_added' "
            "AND payload LIKE '%proof-backed fact%'"
        ).fetchone()
        poc = conn.execute(
            "SELECT artifact_id, path, status, entry_command FROM pocs"
        ).fetchone()
    assert fact is not None and fact[1] == 1
    assert poc == (
        fact[0],
        f"shared/objects/{fact[0][:2]}/{fact[0][2:4]}/{fact[0]}",
        "directional",
        "(no entry command supplied; inspect artifact before reuse)",
    )
    assert (workspace / poc[1]).read_text(encoding="utf-8") == "#!/bin/sh\necho proof\n"

    # The same materialized evidence may back a distinct fact.  Its PoC event is
    # deduplicated, but the second fact must still be durable.
    again = _run(
        db, "write-fact", "second proof-backed fact", "--verified", "--artifact", str(proof),
        cwd=worker,
    )
    assert "verified fact" in again
    assert "second proof-backed fact" in _run(db, "read-facts", "--verified-only")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pocs").fetchone()[0] == 1


def test_write_fact_materialization_redacts_flag_like_literals(tmp_path):
    workspace = tmp_path / "workspace"
    graph_dir = workspace / "graph"
    graph_dir.mkdir(parents=True)
    g = _board(graph_dir)
    db = g.db_path
    g.close()
    worker = workspace / "workers" / "cli-pi"
    proof = worker / "poc.sh"
    proof.parent.mkdir(parents=True)
    proof.write_text("echo flag{do_not_persist}\n", encoding="utf-8")

    _run(db, "write-fact", "redacted proof", "--verified", "--artifact", str(proof), cwd=worker)
    with sqlite3.connect(db) as conn:
        artifact_id = conn.execute(
            "SELECT artifact_id FROM events WHERE kind='fact_added' AND payload LIKE '%redacted proof%'"
        ).fetchone()[0]
        status = conn.execute("SELECT status FROM pocs").fetchone()[0]
    body = (workspace / "shared" / "objects" / artifact_id[:2] / artifact_id[2:4] / artifact_id).read_text()
    assert "flag{do_not_persist}" not in body
    assert "<PRIOR_FLAG>" in body
    assert status == "quarantined"


def test_write_fact_verified_requires_real_shared_cas_artifact(tmp_path):
    workspace = tmp_path / "workspace"
    graph_dir = workspace / "graph"
    graph_dir.mkdir(parents=True)
    g = _board(graph_dir)
    db = g.db_path
    g.close()

    source = tmp_path / "proof.txt"
    source.write_text("curl output proving the fact", encoding="utf-8")
    materialized = materialize_shared_artifact(
        workspace, source, name="proof.txt", kind="test-proof")
    worker = workspace / "workers" / "cli-pi"
    worker.mkdir(parents=True)
    linked = link_shared_into_worker(
        workspace, worker, "proof.txt", materialized["sha256"])

    out = _run(
        db, "write-fact", "proof-backed fact", "--verified", "--artifact", str(linked),
        cwd=worker,
    )
    assert "verified fact" in out
    assert "proof-backed fact" in _run(db, "read-facts", "--verified-only")
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT artifact_id, verified FROM events WHERE kind='fact_added' "
            "AND payload LIKE '%proof-backed fact%'"
        ).fetchone()
    assert row == (materialized["sha256"], 1)


def test_write_fact_claimed_persistence_without_file_stays_candidate(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    out = _run(
        db, "write-fact", "persisted to ./shared/missing.md", "--verified",
        "--artifact", "./shared/missing.md", cwd=tmp_path,
    )
    assert "candidate fact" in out
    assert "persisted to" not in _run(db, "read-facts", "--verified-only")

def test_candidate_fact_excluded_by_verified_only(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    _run(db, "write-fact", "maybe an IDOR on /api/user")  # no --verified
    assert "maybe an IDOR" in _run(db, "read-facts")
    assert "maybe an IDOR" not in _run(db, "read-facts", "--verified-only")


def test_mark_and_read_deadend(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    _run(db, "mark-deadend", "no SQLi on /search — parameterized")
    out = _run(db, "read-deadends")
    assert "no SQLi on /search" in out
    assert "DO NOT retry" in out


def test_claim_intent_won_then_lost(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    # propose an intent via the graph API (the coordinator does this)
    g.propose_intent(actor="reason", intent_id="I1", goal="try default creds")
    g.close()
    # first worker wins
    assert "WON" in _run(db, "claim", "I1", worker="cli-pi")
    # second worker loses (already claimed, lease valid)
    assert "LOST" in _run(db, "claim", "I1", worker="cli-pi")


def test_list_intents(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.propose_intent(
        actor="reason", intent_id="I7", goal="decode the JWT",
        payload={"worker_class": "verifier", "route_hash": "web:jwt"},
    )
    g.close()
    out = _run(db, "list-intents")
    assert "I7" in out and "decode the JWT" in out
    assert "class=verifier" in out and "route=web:jwt" in out


def test_read_review_state(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    fseq = g.add_evidence(actor="cli-a", source="curl", fact="JWT uses HS256",
                          verified=True, artifact_id="a1")
    g.add_review_finding(actor="reviewer", kind="route_loop", severity="blocker",
                         summary="login SQLi repeated", route_hash="web:login:sqli")
    g.challenge_fact(actor="reviewer", fact_seq=fseq, reason="no raw JWT header",
                     verification_goal="Decode a real JWT header.")
    g.suppress_route(actor="reviewer", route_hash="web:login:sqli",
                     label="login SQLi", reason="three repeated dead ends")
    g.split_branch(actor="reviewer", title="CRM branch", branches=[
        {"id": "crm-public", "assumption": "public CRM reachable",
         "prove_or_disprove": "curl /admin from current pivot"},
    ])
    g.add_coordinator_directive(actor="reviewer", action="rebootstrap",
                                directive="Stop repeating login SQLi.")
    g.close()

    out = _run(db, "read-review")
    assert "login SQLi repeated" in out
    assert "Challenged facts" in out and "JWT uses HS256" in out
    assert "web:login:sqli" in out
    assert "CRM branch" in out
    assert "Stop repeating login SQLi" in out

    assert "SUPPRESSED" in _run(db, "read-routes")
    assert "crm-public" in _run(db, "read-branches")


def test_claim_succeeds_despite_empty_challenge_id_event(tmp_path):
    """run-7349 regression: the DB had an event with an EMPTY challenge_id, and
    _challenge_id used `SELECT challenge_id FROM events LIMIT 1` — which could grab
    that empty row, making claim's `WHERE challenge_id=?` match nothing → an open
    intent always returned LOST (so no worker could pick up Reason's intents). The
    fix skips empty challenge_ids; claim must WON here."""
    import sqlite3
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    # The empty-challenge_id event must come FIRST (lowest rowid) so a naive
    # `SELECT challenge_id FROM events LIMIT 1` returns "" — that's the exact shape
    # of run-7349's DB. Insert it before the real intent.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO events (ts, challenge_id, actor, kind, payload, verified, "
        "confidence) VALUES (0, '', 'coordinator', 'note', '{}', 0, 1.0)")
    conn.commit()
    conn.close()
    # sanity: the naive query the old code used would indeed return "" here
    conn = sqlite3.connect(db)
    naive = conn.execute("SELECT challenge_id FROM events LIMIT 1").fetchone()[0]
    conn.close()
    assert naive == "", "test must reproduce the empty-challenge_id-first condition"
    # now propose the intent (challenge_id = c1) and claim it via the skill
    g = SQLiteSharedGraph.open(db_path=db,
                               challenge=Challenge(id="c1", name="t", category="web"))
    g.propose_intent(actor="reason", intent_id="I1", goal="enumerate redis")
    g.close()
    assert "WON" in _run(db, "claim", "I1", worker="cli-pi")


def test_fact_written_by_skill_is_visible_to_graph(tmp_path):
    """A fact the worker writes via the skill must be readable through the graph
    API (so Reason's to_summary sees it)."""
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    _run(db, "write-fact", "service is nginx 1.18.0", "--verified")
    # reopen the graph and check the materialized view
    ch = Challenge(id="c1", name="t", category="web")
    g2 = SQLiteSharedGraph.open(db_path=db, challenge=ch)
    summary = g2.to_summary()
    g2.close()
    assert "nginx 1.18.0" in summary


def test_skill_write_fact_links_product_to_current_intent(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.propose_intent(actor="reason", intent_id="I-skill", goal="enumerate /admin")
    g.claim_intent(worker="cli-pi", intent_id="I-skill")
    g.close()

    _run(db, "write-fact", "admin panel exists", "--verified", intent_id="I-skill")

    g2 = SQLiteSharedGraph.open(db_path=db, challenge=Challenge(id="c1", name="t", category="web"))
    products = g2.intent_products("I-skill")
    g2.close()
    assert len(products) == 1


# ── dedupe_key parity: skill ↔ shared_graph._normalize_fact_identity ──────────
#
# The write-time fact dedupe lives in TWO places that MUST stay in lockstep: the
# coordinator's SQLiteSharedGraph.add_evidence (keyed off _normalize_fact_identity)
# and the standalone skill's write_fact. run-75378 showed what happens when they
# diverge — a deployed skill still using the old `fact::{actor}::None::{text}` key
# echo-collided NOTHING, so the same fact appended once as a bare skill write and
# again as its "[engine] <text>" VERIFIED_FACT marker, half-defeating the
# run-75377 echo-dedup fix. These tests run the ACTUAL skill against a real DB and
# assert the persisted dedupe_key equals what _normalize_fact_identity would
# produce, across the echo-dedup battery (engine prefix, whitespace, case).

def _dedupe_key_for(db, text, *, worker="cli-pi", script=None):
    """Write `text` via the skill and return the dedupe_key it persisted."""
    skill = str(script) if script is not None else None
    if skill is None:
        _run(db, "write-fact", text, worker=worker)
    else:
        env = {**os.environ, "DSWARM_BLACKBOARD_DB": str(db), "DSWARM_WORKER_ID": worker}
        r = subprocess.run([sys.executable, skill, "write-fact", text],
                           capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, f"{skill} write-fact failed: {r.stderr}"
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT dedupe_key FROM events WHERE kind='fact_added' "
            "AND actor=? ORDER BY seq DESC LIMIT 1", (worker,)).fetchone()
    finally:
        con.close()
    assert row is not None, "skill did not persist a fact_added event"
    return row[0]


@pytest.mark.parametrize("text", [
    "service is nginx 1.18.0",
    "[pi] service is nginx 1.18.0",            # engine prefix stripped
    "[Pi] service is nginx 1.18.0",             # case-insensitive prefix
    "service   is\tnginx\n1.18.0",                 # whitespace folded
    "SERVICE is NGINX 1.18.0",                      # lowercased
    "admin:admin works on /login",
    "flag candidate: FLAG{not_a_real_flag}",
])
def test_skill_dedupe_key_matches_normalize_fact_identity(tmp_path, text):
    """The repo skill's persisted dedupe_key == coordinator's identity key for the
    same actor+text. This is the regression-catcher for run-75378 drift."""
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    got = _dedupe_key_for(db, text, worker="cli-pi")
    expected = f"fact::cli-pi::{_normalize_fact_identity(text)}"
    assert got == expected


def test_skill_echo_dedupe_collides_engine_prefixed_marker(tmp_path):
    """A bare skill fact and its "[engine] <text>" VERIFIED_FACT echo must collide on
    ONE dedupe_key (the exact run-75377/75378 echo the normalized key exists to kill).
    Same actor + same identity ⇒ second write is a no-op."""
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    k1 = _dedupe_key_for(db, "service is nginx 1.18.0", worker="cli-pi")
    k2 = _dedupe_key_for(db, "[pi] service is nginx 1.18.0", worker="cli-pi")
    assert k1 == k2
    # and the board carries exactly one such fact, not two
    con = sqlite3.connect(str(db))
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM events WHERE kind='fact_added' AND actor='cli-pi'"
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_resolved_blackboard_script_dedupe_matches_repo(tmp_path):
    """Whatever _blackboard_script_path() RESOLVES to for a non-containerized run must
    itself produce a dedupe_key matching _normalize_fact_identity — i.e. the path the
    swarm actually hands workers is never a drifted copy. (Source runs resolve to the
    repo skill; this guards the resolution wiring + the resolved file's logic.)"""
    ch = Challenge(id="resolve", name="t", category="web")
    resolved = CliSolver(None, ch, engine="pi")._blackboard_script_path()
    assert resolved != "/usr/local/bin/blackboard.py"  # not the container path here
    assert Path(resolved).is_file()

    g = _board(tmp_path)
    db = g.db_path
    g.close()
    text = "[pi]   ADMIN panel   at /admin"
    got = _dedupe_key_for(db, text, worker="cli-pi", script=resolved)
    expected = f"fact::cli-pi::{_normalize_fact_identity(text)}"
    assert got == expected


# ── safety-net: sync_deployed_blackboard_skills reconciles deployed copies ────

def test_sync_deployed_blackboard_skills_resyncs_stale_and_missing(tmp_path, monkeypatch):
    """The launch-time safety net overwrites a stale/missing deployed copy from the
    repo source and leaves a fresh one alone — closing the run-75378 drift gap for the
    auto-discovered user-scope copies."""
    claude = tmp_path / ".claude" / "skills" / "dswarm-blackboard" / "blackboard.py"
    agents = tmp_path / ".agents" / "skills" / "dswarm-blackboard" / "blackboard.py"
    monkeypatch.setattr(blackboard_skill, "_DEPLOYED_BLACKBOARD_SCRIPTS",
                        (str(claude), str(agents)))
    src = Path(blackboard_skill._repo_blackboard_script())

    # First run: both missing → both synced from repo (and SKILL.md moves too).
    rows = blackboard_skill.sync_deployed_blackboard_skills()
    assert {r["status"] for r in rows} == {"synced"}
    assert claude.read_bytes() == src.read_bytes()
    assert agents.read_bytes() == src.read_bytes()
    assert (claude.parent / "SKILL.md").is_file()

    # Second run: identical → no action.
    rows = blackboard_skill.sync_deployed_blackboard_skills()
    assert {r["status"] for r in rows} == {"ok"}

    # Drift ONE copy → only it is re-synced; the fresh one is left untouched.
    claude.write_text("# drifted out of sync\n")
    rows = blackboard_skill.sync_deployed_blackboard_skills()
    by_path = {r["path"]: r["status"] for r in rows}
    assert by_path[str(claude)] == "synced"
    assert by_path[str(agents)] == "ok"
    assert claude.read_bytes() == src.read_bytes()  # restored


def test_sync_deployed_blackboard_skills_no_source_is_noop(monkeypatch):
    """An installed deployment (no repo skill adjacent to the package) reports
    'no-source' and touches nothing — the deployed copy IS the source of truth there."""
    monkeypatch.setattr(blackboard_skill, "_repo_blackboard_script", lambda: None)
    rows = blackboard_skill.sync_deployed_blackboard_skills()
    assert rows and all(r["status"] == "no-source" for r in rows)


def test_blackboard_db_path_accepts_windows_path_from_posix_shell(monkeypatch):
    """A worker launched by WSL/Git Bash can inherit a Windows DB path.

    sqlite3 would otherwise treat ``C:\\...`` as a literal relative filename and
    open/create an empty DB, making read-facts crash with "no such table: events".
    """
    spec = importlib.util.spec_from_file_location("bb_skill_path_test", _SKILL)
    assert spec and spec.loader
    bb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bb)

    raw = r"C:\Projects\Agent-projects\ctf-swarm\sessions\run-x\workspace\graph\shared_graph.db"
    converted = "/mnt/c/Projects/Agent-projects/ctf-swarm/sessions/run-x/workspace/graph/shared_graph.db"
    monkeypatch.setattr(bb.os, "name", "posix", raising=False)
    monkeypatch.setenv("DSWARM_BLACKBOARD_DB", raw)
    monkeypatch.setattr(bb.os.path, "exists", lambda p: p == converted)

    assert bb._db_path() == converted


def test_skill_write_fact_does_not_link_missing_intent_product(tmp_path):
    g = _board(tmp_path)
    db = g.db_path
    g.close()
    _run(db, "write-fact", "orphan result", intent_id="missing-intent")
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM intent_products WHERE intent_id=?",
            ("missing-intent",),
        ).fetchone()[0]
        payload = conn.execute(
            "SELECT payload FROM events WHERE kind='fact_added' "
            "AND payload LIKE '%orphan result%'"
        ).fetchone()[0]
    assert count == 0
    assert '"orphan_intent_id": "missing-intent"' in payload


def test_skill_http_connection_failure_is_not_reported_as_success(tmp_path):
    env = {**os.environ, "PYTHONUTF8": "1",
           "DSWARM_BLACKBOARD_URL": "http://127.0.0.1:1/api/blackboard",
           "DSWARM_BLACKBOARD_RUN_ID": "run-test",
           "DSWARM_BLACKBOARD_TOKEN": "test-token",
           "DSWARM_WORKER_ID": "cli-pi"}
    env.pop("DSWARM_BLACKBOARD_DB", None)
    result = subprocess.run(
        [sys.executable, str(_SKILL), "read-facts"],
        capture_output=True, text=True, env=env, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode != 0
    assert "blackboard HTTP call failed" in result.stderr
    assert "OK" not in result.stdout
