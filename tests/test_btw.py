"""BTW side-query observer — isolation guarantees.

Verifies the 10 invariants from docs/internal-design/BTW_SIDEQUERY_DESIGN.md hold:
open_readonly is truly read-only (no mkdir / no WAL / no schema write / file mtime
unchanged), recent_events is bounded + challenge-scoped, sanitize_transcript caps
runaway input, and btw_messages assembles the multi-turn prompt correctly.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph
from dswarm.solver.btw import (
    BTW_MODEL,
    apply_btw_runtime_argv,
    BtwWorkerPaths,
    btw_messages,
    btw_evidence_messages,
    build_btw_evidence_pack_sync,
    build_btw_context_sync,
    build_btw_worker_prompt,
    sanitize_transcript,
    stream_btw_worker_deltas,
    parse_btw_structured_answer,
)


def _chal() -> Challenge:
    return Challenge(id="t1", name="t", category="crypto")


def test_open_readonly_does_not_create_parent_dir(tmp_path):
    """open_readonly must NOT mkdir the parent — a missing DB is the caller's
    problem, never a reason to write disk."""
    missing = tmp_path / "nope" / "does_not_exist.db"
    with pytest.raises(Exception):
        SQLiteSharedGraph.open_readonly(db_path=missing, challenge=_chal())
    # the parent directory must not have been created
    assert not (tmp_path / "nope").exists()


def test_open_readonly_is_truly_read_only(tmp_path):
    """A graph opened via open_readonly must reject every write — proving the
    btw path cannot mutate the blackboard even if a caller tried."""
    db = tmp_path / "g.db"
    g = SQLiteSharedGraph.open(db_path=db, challenge=_chal())
    g.add_evidence(actor="s1", source="run", fact="k=1", verified=True)
    g.close()

    ro = SQLiteSharedGraph.open_readonly(db_path=db, challenge=_chal())
    try:
        with pytest.raises(Exception):
            ro._conn.execute("INSERT INTO events (ts, challenge_id, actor, kind, payload) "
                             "VALUES (1,'t1','x','x','{}')")
            ro._conn.commit()
    finally:
        ro.close()


def test_open_readonly_leaves_file_mtime_unchanged(tmp_path):
    """The graph DB file's mtime must not change from a readonly open+close —
    no WAL checkpoint, no migration, no commit."""
    db = tmp_path / "g.db"
    g = SQLiteSharedGraph.open(db_path=db, challenge=_chal())
    g.add_evidence(actor="s1", source="run", fact="k=1", verified=True)
    g.close()
    # settle WAL so the .db file is stable; then snapshot mtime
    time.sleep(0.05)
    m0 = os.path.getmtime(db)
    ro = SQLiteSharedGraph.open_readonly(db_path=db, challenge=_chal())
    try:
        _ = ro.to_summary()
        _ = ro.recent_events(40)
    finally:
        ro.close()
    m1 = os.path.getmtime(db)
    assert m1 == m0, f"readonly open mutated the DB file: {m0} -> {m1}"


def test_recent_events_is_bounded_and_challenge_scoped(tmp_path):
    """recent_events must (a) cap at `limit` at the SQL layer and (b) filter by
    challenge_id — not the old events()[-limit:] full-table scan."""
    db = tmp_path / "g.db"
    g = SQLiteSharedGraph.open(db_path=db, challenge=_chal())
    for i in range(60):
        g.add_evidence(actor="s", source="run", fact=f"f{i}", verified=False)
    g.close()

    ro = SQLiteSharedGraph.open_readonly(db_path=db, challenge=_chal())
    try:
        recent = ro.recent_events(limit=10)
        assert len(recent) == 10
        # oldest-first, and these are the LAST 10 (highest seq)
        seqs = [e["seq"] for e in recent]
        assert seqs == sorted(seqs)
        assert max(seqs) > 50
        # challenge-scoped: a different challenge id sees nothing
        other = Challenge(id="other", name="o", category="misc")
        ro2 = SQLiteSharedGraph.open_readonly(db_path=db, challenge=other)
        try:
            assert ro2.recent_events(40) == []
        finally:
            ro2.close()
    finally:
        ro.close()


def test_sanitize_transcript_caps_and_drops_garbage():
    """sanitize_transcript must reject non-user/assistant roles, cap total
    turns, and never raise on malformed input."""
    raw = [
        {"role": "user", "content": "first question"},
        {"role": "system", "content": "should be dropped"},
        {"role": "assistant", "content": "answer"},
        {"role": "evil", "content": "drop me"},
        {"content": "no role"},
        {"role": "user"},  # no content
        "not a dict",
        None,
    ]
    out = sanitize_transcript(raw)
    roles = [t["role"] for t in out]
    assert roles == ["user", "assistant"]
    # never raises on total garbage
    assert sanitize_transcript(None) == []
    assert sanitize_transcript("string") == []
    assert sanitize_transcript(123) == []


def test_sanitize_transcript_truncates_long_messages():
    long = "x" * 100000
    out = sanitize_transcript([{"role": "user", "content": long}])
    assert len(out) == 1
    assert len(out[0]["content"]) < 100000  # capped


def test_btw_messages_assembles_system_transcript_question():
    transcript = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    msgs = btw_messages("q2", "CTX_SNAPSHOT", transcript, context_hint="web worker")
    assert msgs[0]["role"] == "system"
    assert "CTX_SNAPSHOT" in msgs[0]["content"]
    assert "web worker" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "q2"}
    assert msgs[1:3] == transcript


def test_build_btw_worker_prompt_is_readonly_and_multiturn():
    paths = BtwWorkerPaths(
        workspace="/run/ws",
        jsonl="/run/r.jsonl",
        graph_db="/run/ws/graph/shared_graph.db",
        board="/run/ws/.dswarm_board.md",
        winner="/run/ws/winner.json",
        arts="/run/ws/arts",
        uploads="/run/uploads",
    )
    prompt = build_btw_worker_prompt(
        question="总结整体路径",
        paths=paths,
        challenge_id="run-1",
        challenge_name="demo",
        challenge_category="web",
        run_state="finished",
        context_hint="只看 verified",
        transcript=[
            {"role": "user", "content": "上一问"},
            {"role": "assistant", "content": "上一答"},
        ],
    )
    assert "/run/ws/graph/shared_graph.db" in prompt
    assert "/run/r.jsonl" in prompt
    assert "上一问" in prompt
    assert "上一答" in prompt
    assert "不要调用或写入 blackboard" in prompt
    assert "不要修改 `shared_graph.db`" in prompt
    assert "不要输出 `FOUND_FLAG=`" in prompt
    # The observer must not wander into the installed source tree for a run-file
    # audit: that made bug questions spend the whole BTW timeout in exploration.
    assert "Inspect only the run files listed below" in prompt
    assert "Do NOT search or read the D-Swarm source tree" in prompt
    assert "Do one focused evidence pass, then answer" in prompt


def test_apply_btw_runtime_argv_honors_worker_provider_and_model():
    argv = ["pi", "--mode", "json", "PROMPT"]
    out = apply_btw_runtime_argv(
        argv,
        {
            "DSWARM_PI_PROVIDER": "ctf-gateway",
            "DSWARM_WORKER_MODEL": "deepseek-v4-flash",
        },
    )
    assert out[-1] == "PROMPT"
    assert out[out.index("--provider") + 1] == "ctf-gateway"
    assert out[out.index("--model") + 1] == "deepseek-v4-flash"


def test_apply_btw_runtime_argv_overwrites_provider_without_duplicate_model():
    argv = ["pi", "--provider", "anthropic", "--model", "old-model", "PROMPT"]
    out = apply_btw_runtime_argv(
        argv,
        {
            "DSWARM_PI_PROVIDER": "ctf-gateway",
            "DSWARM_WORKER_MODEL": "deepseek-v4-flash",
        },
    )
    assert out.count("--provider") == 1
    assert out.count("--model") == 1
    assert out[out.index("--provider") + 1] == "ctf-gateway"
    assert out[out.index("--model") + 1] == "old-model"


def test_apply_btw_runtime_argv_inserts_before_double_dash():
    argv = ["pi", "--mode", "json", "--", "PROMPT"]
    out = apply_btw_runtime_argv(
        argv,
        {
            "DSWARM_PI_PROVIDER": "ctf-gateway",
            "DSWARM_WORKER_MODEL": "deepseek-v4-flash",
        },
    )
    assert out.index("--provider") < out.index("--")
    assert out.index("--model") < out.index("--")
    assert out[-1] == "PROMPT"

@pytest.mark.asyncio
async def test_stream_btw_worker_deltas_uses_one_cli_run(monkeypatch, tmp_path):
    from dswarm.solver.cli_driver import CliResult, StreamStep

    calls: list[dict] = []

    class FakeDriver:
        name = "fake"

        def new_session(self):
            return "sid"

        def build_execute(self, prompt, session, *, web_access=True, kb_access=True, stream=False):
            calls.append({
                "prompt": prompt,
                "session": session,
                "web_access": web_access,
                "kb_access": kb_access,
                "stream": stream,
            })
            return ["fake", "run"]

    def fake_run_cli_streaming(driver, argv, *, cwd, timeout, on_step, env=None,
                               cancel_event=None, container=None, **_kw):
        calls.append({
            "argv": argv,
            "cwd": cwd,
            "timeout": timeout,
            "env": env,
            "cancel_event": cancel_event,
            "container": container,
        })
        on_step(StreamStep("tool", tool="shell", text="sqlite3 graph"))
        on_step(StreamStep("reasoning", text="答案片段"))
        on_step(StreamStep("tool_result", text="hidden tool output", raw="hidden tool output"))
        return CliResult(text="final text")

    monkeypatch.setattr(
        "dswarm.solver.cli_driver.run_cli_streaming",
        fake_run_cli_streaming,
    )

    chunks = []
    async for chunk in stream_btw_worker_deltas(
        driver=FakeDriver(),
        prompt="PROMPT",
        cwd=str(tmp_path),
        timeout=7,
        env={"DSWARM_BTW_WORKER": "1", "DSWARM_PI_PROVIDER": "ctf-gateway",
              "DSWARM_WORKER_MODEL": "deepseek-v4-flash"},
        web_access=False,
        kb_access=False,
    ):
        chunks.append(chunk)

    assert chunks == ["答案片段"]
    assert calls[0]["session"] == "sid"
    assert calls[0]["web_access"] is False
    assert calls[0]["kb_access"] is False
    assert calls[0]["stream"] is True
    assert calls[1]["cwd"] == str(tmp_path)
    assert calls[1]["timeout"] == 7
    assert calls[1]["env"]["DSWARM_BTW_WORKER"] == "1"
    assert calls[1]["argv"][calls[1]["argv"].index("--provider") + 1] == "ctf-gateway"
    assert calls[1]["argv"][calls[1]["argv"].index("--model") + 1] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_stream_btw_worker_deltas_deduplicates_pi_complete_messages(monkeypatch, tmp_path):
    """Pi may repeat one complete assistant message at message_end/turn_end.

    BTW receives complete reasoning blocks, not text_delta events, so it must
    not append lifecycle echoes as if they were fresh answer text.
    """
    from dswarm.solver.cli_driver import CliResult, StreamStep

    class FakeDriver:
        name = "fake"

        def new_session(self):
            return "sid"

        def build_execute(self, prompt, session, **kwargs):
            return ["fake", "run"]

    def fake_run_cli_streaming(driver, argv, *, cwd, timeout, on_step, **_kw):
        on_step(StreamStep("reasoning", text="完整答案"))
        on_step(StreamStep("reasoning", text="完整答案"))
        on_step(StreamStep("reasoning", text="完整答案追加"))
        on_step(StreamStep("reasoning", text="完整答案追加"))
        on_step(StreamStep("reasoning", text="完整答案"))
        return CliResult(text="完整答案追加")

    monkeypatch.setattr(
        "dswarm.solver.cli_driver.run_cli_streaming",
        fake_run_cli_streaming,
    )

    chunks = [
        chunk
        async for chunk in stream_btw_worker_deltas(
            driver=FakeDriver(),
            prompt="PROMPT",
            cwd=str(tmp_path),
            timeout=7,
        )
    ]

    assert chunks == ["完整答案", "追加"]


@pytest.mark.asyncio
async def test_stream_btw_worker_deltas_surfaces_auth_failure_without_secret(
        monkeypatch, tmp_path):
    from dswarm.solver.cli_driver import CliResult

    class FakeDriver:
        name = "fake"

        def new_session(self):
            return "sid"

        def build_execute(self, prompt, session, **kwargs):
            return ["fake", "run"]

    def fake_run_cli_streaming(*_args, **_kwargs):
        return CliResult(
            text='{"stopReason":"error","errorMessage":"401 authentication_error '
                 'invalid x-api-key SECRET_VALUE"}',
            raw_stderr="provider rejected token SECRET_VALUE",
        )

    monkeypatch.setattr(
        "dswarm.solver.cli_driver.run_cli_streaming", fake_run_cli_streaming)

    chunks = []
    with pytest.raises(RuntimeError) as exc_info:
        async for chunk in stream_btw_worker_deltas(
            driver=FakeDriver(), prompt="PROMPT", cwd=str(tmp_path), timeout=7,
        ):
            chunks.append(chunk)

    message = str(exc_info.value)
    assert chunks == []
    assert "认证失败" in message
    assert "SECRET_VALUE" not in message
    assert "stopReason" not in message


@pytest.mark.asyncio
async def test_stream_btw_worker_deltas_surfaces_generic_cli_error(monkeypatch, tmp_path):
    from dswarm.solver.cli_driver import CliResult

    class FakeDriver:
        name = "fake"

        def new_session(self):
            return "sid"

        def build_execute(self, prompt, session, **kwargs):
            return ["fake", "run"]

    monkeypatch.setattr(
        "dswarm.solver.cli_driver.run_cli_streaming",
        lambda *_args, **_kwargs: CliResult(
            text='{"stopReason":"error","errorMessage":"provider unavailable"}'),
    )

    with pytest.raises(RuntimeError, match="CLI 返回 error 状态"):
        async for _chunk in stream_btw_worker_deltas(
            driver=FakeDriver(), prompt="PROMPT", cwd=str(tmp_path), timeout=7,
        ):
            pass


@pytest.mark.asyncio
async def test_stream_btw_worker_deltas_rejects_empty_result(monkeypatch, tmp_path):
    from dswarm.solver.cli_driver import CliResult

    class FakeDriver:
        name = "fake"

        def new_session(self):
            return "sid"

        def build_execute(self, prompt, session, **kwargs):
            return ["fake", "run"]

    monkeypatch.setattr(
        "dswarm.solver.cli_driver.run_cli_streaming",
        lambda *_args, **_kwargs: CliResult(text=""),
    )

    with pytest.raises(RuntimeError, match="未返回任何回答"):
        async for _chunk in stream_btw_worker_deltas(
            driver=FakeDriver(), prompt="PROMPT", cwd=str(tmp_path), timeout=7,
        ):
            pass



def test_build_btw_context_sync_degrades_when_graph_missing(tmp_path):
    """A run with no graph DB yet must produce a minimal context, not raise."""
    ctx = build_btw_context_sync(
        graph_db_path=str(tmp_path / "missing.db"),
        challenge_id="r1",
        challenge_name="demo",
        challenge_category="web",
        run_meta={"state": "running"},
        context_hint="",
    )
    assert "demo" in ctx
    assert "尚未建立" in ctx or "不可读" in ctx


def test_build_btw_context_sync_reads_existing_graph(tmp_path):
    db = tmp_path / "g.db"
    g = SQLiteSharedGraph.open(db_path=db, challenge=_chal())
    g.add_evidence(actor="s1", source="run", fact="flag is FLAG{xyz}", verified=True)
    g.close()
    ctx = build_btw_context_sync(
        graph_db_path=str(db),
        challenge_id="t1",
        challenge_name="t",
        challenge_category="crypto",
        run_meta={"state": "finished", "solved": True, "flags": ["FLAG{xyz}"]},
        context_hint="",
    )
    assert "已解出 flag" in ctx
    # the verified fact shows up in the snapshot
    assert "FLAG{xyz}" in ctx or "flag is" in ctx


def test_btw_model_is_flash_not_pro():
    """btw must use the cheap flash tier, hardcoded — not the configurable
    Reason planner profile. Predictable cost for an observation task."""
    assert BTW_MODEL == "deepseek-v4-flash"


def test_build_btw_run_stats_sync_aggregates_cost_and_timing(tmp_path):
    """The JSONL scan must aggregate COST_UPDATE (usd/tokens) and timing
    (RUN_STARTED → RUN_FINISHED) so the btw model can answer 'how long /
    how much / how many tokens'. This is the ONLY reliable source on a
    historical run after a server restart (in-memory CostController is empty)."""
    import json as _json
    from dswarm.solver.btw import build_btw_run_stats_sync

    jsonl = tmp_path / "run-stats.jsonl"
    events = [
        {"event_type": "run.started", "ts": 1000.0, "run_id": "r", "payload": {}},
        {"event_type": "worker.status", "ts": 1001.0, "run_id": "r", "solver_id": "cli-pi",
         "payload": {"online": True, "status": "running", "engine": "claude"}},
        {"event_type": "cost.update", "ts": 1002.0, "run_id": "r", "solver_id": "cli-pi",
         "payload": {"scope": "solver", "usd": 0.12, "tokens": 5000, "input_tokens": 3000, "output_tokens": 2000}},
        {"event_type": "cost.update", "ts": 1003.0, "run_id": "r", "solver_id": "cli-pi",
         "payload": {"scope": "solver", "usd": 0.08, "tokens": 4000, "input_tokens": 2500, "output_tokens": 1500}},
        {"event_type": "worker.finished", "ts": 1010.0, "run_id": "r", "solver_id": "cli-pi",
         "payload": {"reason": "solved", "flag": "flag{xyz}"}},
        {"event_type": "run.finished", "ts": 1015.0, "run_id": "r", "payload": {}},
    ]
    with jsonl.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(_json.dumps(ev) + "\n")

    stats = build_btw_run_stats_sync(str(jsonl))
    # timing: 1015 - 1000 = 15 seconds
    assert "15秒" in stats
    # cost: 0.12 + 0.08 = 0.20
    assert "0.2000" in stats or "0.2" in stats
    # tokens: 5000 + 4000 = 9000
    assert "9000" in stats
    # per-solver breakdown
    assert "cli-pi" in stats
    assert "cli-pi" in stats
    # worker roster with found-flag marker
    assert "cli-pi" in stats
    assert "找到flag" in stats


def test_build_btw_run_stats_sync_missing_file_returns_empty(tmp_path):
    """A missing JSONL must degrade to empty string, never raise."""
    from dswarm.solver.btw import build_btw_run_stats_sync
    assert build_btw_run_stats_sync(str(tmp_path / "nope.jsonl")) == ""


def test_build_btw_context_sync_includes_run_stats(tmp_path):
    """The full context must include the run-stats block when jsonl_path is given."""
    import json as _json
    from dswarm.solver.btw import build_btw_context_sync

    jsonl = tmp_path / "r.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(_json.dumps({"event_type": "run.started", "ts": 100.0, "run_id": "r", "payload": {}}) + "\n")
        f.write(_json.dumps({"event_type": "cost.update", "ts": 101.0, "run_id": "r",
                             "solver_id": "s1", "payload": {"usd": 0.5, "tokens": 100}}) + "\n")
        f.write(_json.dumps({"event_type": "run.finished", "ts": 130.0, "run_id": "r", "payload": {}}) + "\n")
    ctx = build_btw_context_sync(
        graph_db_path=str(tmp_path / "missing.db"),
        challenge_id="r", challenge_name="r", challenge_category="misc",
        run_meta={"state": "finished"},
        jsonl_path=str(jsonl),
    )
    assert "Run 维度统计" in ctx
    assert "30秒" in ctx  # 130 - 100
    assert "0.5" in ctx



def test_btw_evidence_pack_is_bounded_deduplicated_and_citable(tmp_path):
    db = tmp_path / "graph.db"
    graph = SQLiteSharedGraph.open(db_path=db, challenge=_chal())
    graph.add_evidence(actor="worker", source="stdout", fact="verified route exists", verified=True)
    graph.add_evidence(actor="worker", source="stdout", fact="candidate route exists", verified=False)
    graph.close()
    arts = tmp_path / "arts"
    arts.mkdir()
    (arts / "recon.txt").write_text("line one\nline two", encoding="utf-8")

    pack = build_btw_evidence_pack_sync(
        graph_db_path=str(db), jsonl_path=str(tmp_path / "missing.jsonl"),
        challenge_id="t1", challenge_name="demo", challenge_category="web",
        run_meta={"state": "finished", "workers": [{"id": "worker"}]},
        arts_path=str(arts), max_events=10,
    )
    ids = {item["id"] for key in ("facts", "events", "dead_ends", "artifacts") for item in pack[key]}
    assert "fact:1" in ids
    assert any(item["kind"] == "verified" for item in pack["facts"])
    assert pack["artifacts"][0]["id"] == "artifact:recon.txt"
    assert len(pack["facts"]) == len({(x["kind"], x["text"]) for x in pack["facts"]})

    messages = btw_evidence_messages("有哪些事实？", pack, [])
    assert "EVIDENCE_PACK" in messages[0]["content"]
    answer = parse_btw_structured_answer(
        '{"answer_markdown":"结论 **明确**","evidence_refs":["fact:1","fact:999"],"uncertainties":[],"answer_type":"bug_audit"}',
        pack,
    )
    assert answer["answer_markdown"] == "结论 **明确**"
    assert answer["evidence_refs"] == ["fact:1"]
    assert answer["answer_type"] == "bug_audit"


def test_parse_btw_structured_answer_degrades_invalid_json():
    result = parse_btw_structured_answer("不是 JSON", {"facts": [], "events": [], "dead_ends": [], "artifacts": []})
    assert result["answer_markdown"] == "不是 JSON"
    assert result["answer_type"] == "insufficient"
    assert result["evidence_refs"] == []


def test_btw_model_for_chooses_cheap_tier_per_upstream():
    from dswarm.solver.btw import btw_model_for

    bigmodel = "https://open.bigmodel.cn/api/paas/v4"
    deepseek = "https://api.deepseek.com/v1"
    relay = "https://relay.example/v1"
    # known hosts: cheap tier regardless of the planner model
    assert btw_model_for(bigmodel, fallback_model="glm-5.3-pro") == "glm-5.3-flash"
    assert btw_model_for(deepseek, fallback_model="deepseek-v4-pro") == "deepseek-v4-flash"
    # an unknown relay: the planner's own model is proven available there
    assert btw_model_for(relay, fallback_model="glm-5.3-flash") == "glm-5.3-flash"
    assert btw_model_for(relay, fallback_model="") == "deepseek-v4-flash"
    # explicit env override wins everywhere
    assert btw_model_for(deepseek, fallback_model="x", env={"DSWARM_BTW_MODEL": "m"}) == "m"
