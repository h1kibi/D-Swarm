"""TargetStore and Target API regression coverage."""

from __future__ import annotations

import asyncio
import httpx
import json
import pytest

from apps.web.run_manager import RunManager
from apps.web.auth import AuthConfig
from apps.web.server import create_app
from dswarm.core.events import Event, EventType


@pytest.mark.asyncio
async def test_target_store_persists_card_and_relationship(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    target = mgr.target_store.create({
        "name": "Portal", "category": "web", "description": "login challenge",
        "expected_flags": 2,
    })
    assert target["id"].startswith("target-")
    run = mgr.create_new()
    mgr.target_store.attach_run(target["id"], run.run_id)
    mgr.target_store.add_hint(target["id"], "try the upload path")
    mgr.target_store.add_direction(target["id"], "Authentication")

    mgr2 = RunManager(sessions_root=tmp_path)
    restored = mgr2.target_store.get(target["id"])
    assert restored is not None
    assert restored["run_ids"] == [run.run_id]
    assert restored["hints"][0]["text"] == "try the upload path"
    assert restored["directions"][0]["label"] == "Authentication"

    # A Target-bound draft is intentional durable state, unlike an ordinary
    # empty SSE stub. It must remain selectable after a backend restart.
    restored_run = mgr2.get(run.run_id)
    assert restored_run is not None
    assert restored_run.started is False
    assert restored_run.target_id == target["id"]
    assert restored_run.target_name == "Portal"



def test_rehydrated_target_run_exposes_dispatch_mismatch_without_rewriting_history(tmp_path):
    """A stale Target relationship is visible as metadata, not silently migrated."""
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Bugku target", "category": "web"})
    run = manager.create("run-6428")
    manager.target_store.attach_run(target["id"], run.run_id)
    manager.remember_dispatch(run.run_id, {
        "challenge": {"name": "worker-startup-test", "category": "misc"},
        "worker_backend": "local",
    })

    restored = RunManager(sessions_root=tmp_path).get(run.run_id)

    assert restored is not None
    assert restored.target_id == target["id"]
    assert "target_challenge_mismatch" in restored.dispatch_diagnostics
    assert restored.dispatch_body["challenge"]["name"] == "worker-startup-test"

@pytest.mark.asyncio
async def test_target_dispatch_mismatch_rejects_resolve_before_reopen(tmp_path):
    """A stale Target Run stays terminal and receives no reopen marker."""
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Bugku target", "category": "web"})
    run = manager.create("run-target-mismatch")
    run.target_id = target["id"]
    run.target_name = target["name"]
    run.target_category = target["category"]
    manager.target_store.attach_run(target["id"], run.run_id)
    manager.remember_dispatch(run.run_id, {
        "challenge": {"name": "worker-startup-test", "category": "misc", "target_id": "other-target"},
        "target_id": "other-target",
        "worker_backend": "local",
    })
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED,
        run_id=run.run_id,
        payload={"solved": False, "reason": "runtime_failure", "detail": "old failure"},
    ))

    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/resolve", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "target_dispatch_mismatch: historical dispatch binding does not match the current Target; "
        "create a new Target Run"
    )
    assert run.finished is True
    events = run.store.load_all(run.run_id)
    assert not any(item["event_type"] == "run.reopened" for item in events)
    assert events[-1]["event_type"] == "run.finished"

@pytest.mark.asyncio
async def test_target_projection_keeps_failed_run_as_failed_target(tmp_path):
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Broken target", "category": "web"})
    run = manager.create("run-failed-target")
    run.started = False
    run.finished = True
    run.failure_reason = "runtime_failure"
    manager.target_store.attach_run(target["id"], run.run_id)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/targets/{target['id']}")
    assert response.json()["target"]["status"] == "failed"
@pytest.mark.asyncio
async def test_target_audit_persists_provenance_record_for_terminal_run(tmp_path):
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Audit target", "category": "web"})
    run = manager.create_new()
    run.started = True
    run.finished = True
    run.solved = False
    run.target_id = target["id"]
    manager.target_store.attach_run(target["id"], run.run_id)
    graph_dir = manager.workspace_dir(run.run_id) / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    from dswarm.models.solve_graph import Challenge
    graph = SQLiteSharedGraph.open(db_path=graph_dir / "shared_graph.db", challenge=Challenge(id=run.run_id, name="Audit target", category="web"))
    fact_seq = graph.add_evidence(actor="worker", source="curl", fact="admin endpoint exists", verified=False, review_required=True, artifact_id="artifact-1")
    graph.decide_evidence_review(actor="reviewer", decision="verified", fact_seq=fact_seq, evidence_refs=["artifact:artifact-1"])
    graph.close()

    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/targets/{target['id']}/audit-review", json={
            "run_id": run.run_id, "auditor": "operator-1", "facts": [{"fact_seq": fact_seq}],
        })
    assert response.status_code == 200
    record = response.json()["audit_records"][0]
    assert record["source_run_id"] == run.run_id
    assert record["fact_seq"] == fact_seq
    assert record["evidence_refs"] == ["artifact:artifact-1"]
    assert record["auditor"] == "operator-1"
    assert manager.target_store.get(target["id"])["audit_records"]

@pytest.mark.asyncio
async def test_target_audit_accepts_reason_blocked_terminal_run(tmp_path):
    from dswarm.swarm.shared_graph import SQLiteSharedGraph
    from dswarm.models.solve_graph import Challenge
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Blocked target", "category": "web"})
    run = manager.create_new()
    run.started = True
    run.finished = False
    run.reason_blocked = True
    run.reason_blocked_reason = "default provider probe failed"
    run.target_id = target["id"]
    manager.target_store.attach_run(target["id"], run.run_id)
    graph_dir = manager.workspace_dir(run.run_id) / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = SQLiteSharedGraph.open(db_path=graph_dir / "shared_graph.db", challenge=Challenge(id=run.run_id, name="Blocked target", category="web"))
    fact_seq = graph.add_evidence(actor="worker", source="stdout", fact="route remains reachable", verified=False, review_required=True, artifact_id="artifact-blocked")
    graph.decide_evidence_review(actor="reviewer", decision="verified", fact_seq=fact_seq, evidence_refs=["artifact:artifact-blocked"])
    graph.close()
    app = create_app(manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/targets/{target['id']}/audit-review", json={"run_id": run.run_id, "facts": [{"fact_seq": fact_seq}]})
    assert response.status_code == 200

async def test_target_api_creates_target_and_run(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Crypto box", "category": "crypto"})
        assert created.status_code == 200
        target = created.json()["target"]
        target_id = target["id"]

        run_resp = await client.post(f"/api/targets/{target_id}/runs", json={"direction": "Oracle"})
        assert run_resp.status_code == 200
        run = run_resp.json()["run"]
        assert run["target_id"] == target_id
        assert run["direction"] == "Oracle"

        listed = await client.get("/api/targets")
        assert listed.status_code == 200
        card = listed.json()["targets"][0]
        assert card["run_count"] == 1
        assert card["runs"][0]["run_id"] == run["run_id"]

@pytest.mark.asyncio
async def test_delete_target_run_removes_history_and_target_relationship(tmp_path):
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Delete target", "category": "web"})
    run = manager.create_new()
    run.target_id = target["id"]
    run.target_name = target["name"]
    run.target_category = target["category"]
    manager.target_store.attach_run(target["id"], run.run_id)
    await run.bus.emit(Event(
        event_type=EventType.RUN_FINISHED,
        run_id=run.run_id,
        payload={"solved": False},
    ))
    marker = manager.workspace_dir(run.run_id) / "owned-marker"
    marker.write_text("owned", encoding="utf-8")
    log_path = manager.sessions_root / f"{run.run_id}.jsonl"
    assert log_path.exists()

    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(f"/api/runs/{run.run_id}")
        projected = await client.get(f"/api/targets/{target['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert manager.get(run.run_id) is None
    assert manager.target_store.get(target["id"])["run_ids"] == []
    assert projected.json()["target"]["run_count"] == 0
    assert not log_path.exists()
    assert not marker.parent.parent.exists()


@pytest.mark.asyncio
async def test_delete_target_run_cancels_live_task_before_removing_link(tmp_path):
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Live delete target", "category": "web"})
    run = manager.create_new()
    run.target_id = target["id"]
    manager.target_store.attach_run(target["id"], run.run_id)

    async def wait_forever():
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    run.task = task
    await asyncio.sleep(0)

    assert await manager.delete(run.run_id)
    assert task.cancelled()
    assert manager.target_store.get(target["id"])["run_ids"] == []



@pytest.mark.asyncio
async def test_target_create_accepts_initial_primary_direction_atomically(tmp_path):
    manager = RunManager(sessions_root=tmp_path)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/targets", json={
            "name": "Atomic target",
            "kind": "ctf",
            "category": "web",
            "description": "Find the auth boundary",
            "target_address": "https://target.test",
            "primary_direction": {
                "label": "Authentication",
                "description": "Inspect login and session boundaries",
                "category": "web",
            },
        })
    assert response.status_code == 200
    target = response.json()["target"]
    assert target["run_ids"] == []
    assert target["primary_direction_id"] == target["directions"][0]["id"]
    assert target["directions"][0]["label"] == "Authentication"


@pytest.mark.asyncio
async def test_target_run_start_keeps_run_instruction_and_target_context(tmp_path, monkeypatch):
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({
        "name": "Context target",
        "kind": "ctf",
        "category": "web",
        "description": "Inspect the login flow",
        "target_address": "https://portal.test",
        "flag_format_hint": "flag{...}",
        "primary_direction": {"label": "Authentication", "description": "Check sessions", "category": "web"},
    })
    manager.target_store.add_direction(target["id"], "File access", description="Check reads")
    run = manager.create_new()
    run.target_id = target["id"]
    manager.target_store.attach_run(target["id"], run.run_id)
    captured: dict[str, object] = {}

    def fake_build_driver(body, mgr=None):
        captured["body"] = body
        async def fake_driver(_run):
            return None
        fake_driver.dispatch_kind = "mock"
        return fake_driver

    async def fake_start(run_id, driver):
        captured["started"] = run_id

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(manager, "start", fake_start)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/runs/{run.run_id}/start", json={
            "kind": "mock",
            "target_id": target["id"],
            "direction_id": target["primary_direction_id"],
            "prompt": "Prioritize cookie tampering and explain the evidence needed.",
            "challenge": {"description": "Prioritize cookie tampering and explain the evidence needed."},
        })
    assert response.status_code == 200
    body = captured["body"]
    assert isinstance(body, dict)
    prompt = str(body["prompt"])
    description = str(body["challenge"]["description"])
    assert "[RUN INSTRUCTION]" in prompt
    assert "cookie tampering" in prompt
    assert "[TARGET DESCRIPTION]" in prompt
    assert "Inspect the login flow" in prompt
    assert "[TARGET DIRECTIONS]" in prompt
    assert "Authentication [PRIMARY]" in prompt
    assert "File access" in prompt
    assert body["challenge"]["target"] == "https://portal.test"
    assert body["challenge"]["flag_format_hint"] == "flag{...}"
    assert "cookie tampering" in description
    assert captured["started"] == run.run_id


def test_target_store_normalizes_invalid_primary_direction_on_reload(tmp_path):
    path = tmp_path / "_targets.json"
    path.write_text(
        '{"version": 1, "targets": {"target-x": {'
        '"id": "target-x", "name": "Legacy", '
        '"directions": [{"id": "direction-real", "label": "Real"}], '
        '"primary_direction_id": "direction-foreign"'
        '}}}',
        encoding="utf-8",
    )
    mgr = RunManager(sessions_root=tmp_path)
    target = mgr.target_store.get("target-x")
    assert target is not None
    assert target["primary_direction_id"] is None
    assert target["directions"] == [{"id": "direction-real", "label": "Real"}]


@pytest.mark.asyncio
async def test_target_api_rejects_foreign_direction_and_resolves_direction_label(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = (await client.post("/api/targets", json={"name": "First", "category": "web"})).json()["target"]
        second = (await client.post("/api/targets", json={"name": "Second", "category": "web"})).json()["target"]
        direction = (await client.post(
            f"/api/targets/{first['id']}/directions", json={"label": "Upload bypass"}
        )).json()["direction"]

        rejected = await client.post(
            f"/api/targets/{second['id']}/runs", json={"direction_id": direction["id"]}
        )
        assert rejected.status_code == 400
        assert "does not belong" in rejected.json()["detail"]

        created = await client.post(
            f"/api/targets/{first['id']}/runs", json={"direction_id": direction["id"]}
        )
        assert created.status_code == 200
        assert created.json()["run"]["direction_id"] == direction["id"]
        assert created.json()["run"]["direction"] == "Upload bypass"


@pytest.mark.asyncio
async def test_target_api_adds_direction_for_launch_wizard(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Branch target", "category": "web"})
        target_id = created.json()["target"]["id"]
        response = await client.post(f"/api/targets/{target_id}/directions", json={"label": "Auth bypass", "description": "Test login and session boundaries"})
        assert response.status_code == 200
        direction = response.json()["direction"]
        assert direction["label"] == "Auth bypass"
        target = (await client.get(f"/api/targets/{target_id}")).json()["target"]
        assert target["primary_direction_id"] == direction["id"]
        assert target["directions"][0]["description"] == "Test login and session boundaries"


@pytest.mark.asyncio
async def test_target_api_selects_and_validates_primary_direction(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Primary branch", "category": "web"})
        target_id = created.json()["target"]["id"]
        first = (await client.post(f"/api/targets/{target_id}/directions", json={"label": "Auth"})).json()["direction"]
        second = (await client.post(f"/api/targets/{target_id}/directions", json={"label": "Upload"})).json()["direction"]

        selected = await client.patch(f"/api/targets/{target_id}", json={"primary_direction_id": second["id"]})
        assert selected.status_code == 200
        assert selected.json()["target"]["primary_direction_id"] == second["id"]

        invalid = await client.patch(f"/api/targets/{target_id}", json={"primary_direction_id": "direction-does-not-exist"})
        assert invalid.status_code == 200
        assert invalid.json()["target"]["primary_direction_id"] is None
        assert first["id"] in {item["id"] for item in invalid.json()["target"]["directions"]}


@pytest.mark.asyncio
async def test_target_api_updates_metadata_and_adds_operator_hint(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Metadata target", "category": "web"})
        target_id = created.json()["target"]["id"]
        updated = await client.patch(f"/api/targets/{target_id}", json={
            "name": "Updated target",
            "description": "Updated operator context",
            "target_address": "https://target.test",
            "flag_format_hint": "flag{...}",
        })
        assert updated.status_code == 200
        card = updated.json()["target"]
        assert card["name"] == "Updated target"
        assert card["description"] == "Updated operator context"
        assert card["target_address"] == "https://target.test"
        assert card["flag_format_hint"] == "flag{...}"

        hint = await client.post(f"/api/targets/{target_id}/hints", json={"text": "Check auth before brute force", "pinned": True})
        assert hint.status_code == 200
        assert hint.json()["hint"]["text"] == "Check auth before brute force"
        assert hint.json()["hint"]["pinned"] is True
        listed = (await client.get(f"/api/targets/{target_id}")).json()["target"]
        assert listed["hints"][0]["source"] == "operator"

@pytest.mark.asyncio
async def test_target_api_rejects_malformed_body(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/targets", content="[]", headers={"content-type": "application/json"})
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_target_api_renames_and_archives_card(tmp_path):
    app = create_app(RunManager(sessions_root=tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Original"})
        target_id = created.json()["target"]["id"]

        renamed = await client.patch(f"/api/targets/{target_id}", json={"name": "Renamed"})
        assert renamed.status_code == 200
        assert renamed.json()["target"]["name"] == "Renamed"

        archived = await client.post(f"/api/targets/{target_id}/archive", json={"archived": True})
        assert archived.status_code == 200
        assert archived.json()["target"]["archived_at"] is not None
        assert (await client.get("/api/targets")).json()["targets"] == []
        assert len((await client.get("/api/targets?archived=1")).json()["targets"]) == 1

        restored = await client.post(f"/api/targets/{target_id}/archive", json={"archived": False})
        assert restored.status_code == 200
        assert restored.json()["target"]["archived_at"] is None


@pytest.mark.asyncio
async def test_target_facts_are_read_only_provenance_projection(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Fact target", "category": "web"})
    run = manager.create_new()
    run.target_id = target["id"]
    run.target_name = target["name"]
    run.target_category = target["category"]
    manager.target_store.attach_run(target["id"], run.run_id)
    graph_path = manager.workspace_dir(run.run_id) / "graph" / "shared_graph.db"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = SQLiteSharedGraph.open(db_path=graph_path, challenge=Challenge(id=run.run_id, name=run.name or "Fact target", category="web"))
    fact_seq = graph.add_evidence(actor="worker-1", source="stdout", fact="/admin requires auth", verified=True, artifact_id="artifact-1", witness="substring", verifier="provenance_gate")
    graph.close()

    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/targets/{target['id']}/facts")
        assert response.status_code == 200
        facts = response.json()["facts"]
        assert len(facts) == 1
        assert facts[0]["fact"] == "/admin requires auth"
        assert facts[0]["source_run_id"] == run.run_id
        assert facts[0]["fact_seq"] == fact_seq
        assert facts[0]["artifact_id"] == "artifact-1"
        assert facts[0]["provenance_status"] == "verified"

        # The projection does not append a Target-side graph event.
        listed = await client.get(f"/api/targets/{target['id']}")
        assert listed.json()["target"]["verified_fact_count"] == 1


@pytest.mark.asyncio
async def test_target_activity_distinguishes_card_events_run_events_and_fact_projection(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    manager = RunManager(sessions_root=tmp_path)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/targets", json={"name": "Timeline target", "category": "web"})
        target_id = created.json()["target"]["id"]
        await client.post(f"/api/targets/{target_id}/hints", json={"text": "check auth first"})
        run_response = await client.post(f"/api/targets/{target_id}/runs", json={"direction": "Auth"})
        run_id = run_response.json()["run"]["run_id"]
        run = manager.get(run_id)
        assert run is not None

        await run.bus.emit(Event(event_type=EventType.RUN_STARTED, run_id=run_id, ts=10.0, payload={"challenge": {"name": "Timeline target", "category": "web"}}))
        await run.bus.emit(Event(event_type=EventType.RUN_FINISHED, run_id=run_id, ts=11.0, payload={"solved": True, "flag": "flag{real}"}))

        graph_path = manager.workspace_dir(run_id) / "graph" / "shared_graph.db"
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph = SQLiteSharedGraph.open(db_path=graph_path, challenge=Challenge(id=run_id, name="Timeline target", category="web"))
        graph.add_evidence(actor="worker-1", source="stdout", fact="auth boundary confirmed", verified=True, artifact_id="artifact-1", witness="substring", verifier="provenance_gate")
        graph.close()

        response = await client.get(f"/api/targets/{target_id}/activity")
        assert response.status_code == 200
        activity = response.json()["activity"]
        kinds = {item["kind"] for item in activity}
        assert "target.created" in kinds
        assert "hint.added" in kinds
        assert "run.created" in kinds
        assert "run.started" in kinds
        assert "run.solved" in kinds
        projected = next(item for item in activity if item["kind"] == "fact.verified")
        assert projected["source"] == "run_projection"
        assert projected["run_id"] == run_id
        assert projected["source_seq"] >= 1

        # Run lifecycle entries point at the source event sequence instead of
        # copying the raw event payload into the Target card.
        solved = next(item for item in activity if item["kind"] == "run.solved")
        assert solved["source"] == "run"
        assert solved["source_seq"] >= 1


def _contains_secret(value):
    if isinstance(value, dict):
        return any(_contains_secret(k) or _contains_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(v) for v in value)
    return isinstance(value, str) and ("sk-live-secret" in value or "TOP-SECRET" in value)


@pytest.mark.asyncio
async def test_run_summary_is_secret_free_even_for_legacy_snapshot(tmp_path):
    """Every API projection must defend against an old/raw binding snapshot."""
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "legacy binding", "category": "web"})
    run = manager.create_new()
    run.binding_snapshot = {
        "version": 1,
        "roles": {
            "worker": {
                "provider_ref": "relay",
                "model": "worker-v1",
                "base_url": "https://relay.example/v1?api_key=DO-NOT-SHIP",
                "api_key": "DO-NOT-SHIP",
                "nested_secret": "DO-NOT-SHIP",
            }
        },
    }
    manager.target_store.attach_run(target["id"], run.run_id)
    # The rail lists started Runs; this also proves the same DTO boundary is
    # used by both the Target projection and the ordinary Run endpoint.
    run.started = True
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        targets = await client.get("/api/targets")
        runs = await client.get("/api/runs")

    assert targets.status_code == 200
    assert runs.status_code == 200
    target_run = targets.json()["targets"][0]["runs"][0]
    run_summary = runs.json()["runs"][0]
    for payload in (target_run, run_summary):
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "DO-NOT-SHIP" not in encoded
        assert "api_key" not in encoded
        assert payload["binding_snapshot"]["roles"]["worker"] == {
            "provider_ref": "relay", "model": "worker-v1", "base_url": "https://relay.example"
        }

    # Projection must not rewrite the private in-memory snapshot; the worker
    # launch path still owns the immutable source and only the DTO is public.
    assert run.binding_snapshot["roles"]["worker"]["api_key"] == "DO-NOT-SHIP"
    assert "api_key=DO-NOT-SHIP" in run.binding_snapshot["roles"]["worker"]["base_url"]


def test_run_binding_snapshot_is_redacted_and_immutable(tmp_path):
    from apps.web.run_manager import merge_resolve_dispatch

    manager = RunManager(sessions_root=tmp_path)
    original = manager.worker_config.get()["worker_profiles"][0]
    manager.worker_config.set(worker_profiles=[{**original, "enabled": True}])
    profile = manager.worker_config.get()["worker_profiles"][0]
    profile_name = str(profile.get("name") or profile.get("id"))
    run = manager.create_new()
    first = {
        "challenge": {"name": "binding target", "category": "web"},
        "engines": [profile_name],
        "llm_profiles": {
            "planner": {"provider_ref": "relay-a", "model": "reason-v1", "api_key": "sk-live-secret"},
            "titler": {"provider_ref": "relay-a", "model": "title-v1"},
        },
        "review_policy": {"enabled": True},
    }
    manager.remember_dispatch(run.run_id, first)
    snapshot = run.binding_snapshot
    assert snapshot["worker_profile"]["name"] == profile_name
    assert snapshot["roles"]["reason"]["model"] == "reason-v1"
    assert not _contains_secret(snapshot)

    manager.remember_dispatch(run.run_id, {
        "engines": ["different-profile"],
        "llm_profiles": {"planner": {"model": "new-model"}},
    })
    assert run.binding_snapshot == snapshot
    merged = merge_resolve_dispatch(manager.dispatch_for(run.run_id), {
        "engines": ["different-profile"],
        "llm_profiles": {"planner": {"model": "new-model"}},
    })
    assert merged["engines"] == [profile_name]
    assert merged["llm_profiles"]["planner"]["model"] == "reason-v1"


def test_run_binding_snapshot_separates_review_llm_and_policy(tmp_path):
    from apps.web.run_manager import build_binding_snapshot

    manager = RunManager(sessions_root=tmp_path)
    snapshot = build_binding_snapshot({
        "challenge": {"name": "review target", "category": "web"},
        "llm_profiles": {
            "review": {
                "provider_ref": "review-relay",
                "model": "review-v2",
                "api_key": "TOP-SECRET",
            },
        },
        "review_policy": {
            "enabled": True,
            "max_review_workers": 2,
            "authorization": "TOP-SECRET",
            "token": "TOP-SECRET",
            "query_token": "TOP-SECRET",
        },
    }, manager.worker_config)

    assert snapshot["roles"]["review"] == {
        "provider_ref": "review-relay",
        "model": "review-v2",
    }
    assert snapshot["review_policy"] == {"enabled": True, "max_review_workers": 2}
    assert not _contains_secret(snapshot)

def test_binding_snapshot_keeps_observer_fallback_under_observer_role(tmp_path):
    from apps.web.run_manager import build_binding_snapshot
    manager = RunManager(sessions_root=tmp_path)
    snapshot = build_binding_snapshot({"challenge": {"category": "web"}}, manager.worker_config)

    assert "observer_fallback" not in snapshot["roles"]
    assert snapshot["fallback_candidates"]["observer"]["model"] == "deepseek-v4-flash"
    assert "observer_fallback" not in snapshot["fallback_candidates"]


def test_observer_fallback_usage_projects_to_observer_role(tmp_path):
    from apps.web.run_observability import project_run_observability
    from dswarm.core.usage_journal import UsageCall, UsageRecord

    call = UsageCall(
        provider_call_id="observer-fallback-call", producer="internal",
        run_id="run-observer-usage", challenge_id="run-observer-usage",
        worker_instance_id=None, solver_id="observer", profile_id="observer_fallback",
        configured_account_id=None, billing_account_id=None, operation_kind="observer_fallback",
    )
    record = UsageRecord.from_call(
        call, call_outcome="succeeded", usage_status="measured",
        input_tokens=4, output_tokens=6, usd=0.01,
    )
    run = type("Run", (), {
        "run_id": "run-observer-usage", "store": None,
        "ledger": type("Ledger", (), {"records": {record.usage_id: record}})(),
        "binding_snapshot": {"roles": {"observer": {"model": "primary"}}, "fallback_candidates": {"observer": {"model": "fallback"}}},
        "updated_at": 0.0, "finished": True, "status": lambda self: "finished",
        "inherited_verified_facts": [],
    })()
    projected = project_run_observability(run, now=1.0)
    rows = {row["role"]: row for row in projected["binding"]["roles"]}
    assert rows["observer"]["usage"]["calls"] == 1
    assert rows["worker"]["usage"]["calls"] == 0
    assert rows["observer"]["fallback_candidate"]["model"] == "fallback"


def test_binding_snapshot_freezes_role_auth_shape_and_review_profile(tmp_path):
    """The Run snapshot keeps routing metadata, but never credential material."""
    from apps.web.run_manager import build_binding_snapshot

    class _Config:
        def get(self):
            return {
                "worker_profiles": [
                    {
                        "id": "worker-seat",
                        "name": "worker-seat",
                        "label": "Worker seat",
                        "engine": "pi",
                        "runtime": "local-runtime",
                        "roles": ["bootstrap", "explore", "respond"],
                        "provider_ref": "profile-worker-relay",
                        "model": "profile-worker-model",
                        "auth_mode": "header",
                        "auth_header": "X-Profile-Key",
                        "auth_prefix": "Token",
                        "credential_account": "worker-account",
                        "image": "ignored-by-snapshot",
                    },
                    {
                        "id": "review-seat",
                        "name": "review-seat",
                        "label": "Review seat",
                        "engine": "pi",
                        "runtime": "review-runtime",
                        "roles": ["review"],
                        "provider_ref": "profile-review-relay",
                        "model": "profile-review-model",
                        "auth_mode": "bearer",
                        "auth_header": "Authorization",
                        "auth_prefix": "Bearer",
                        "credential_account": "review-account",
                    },
                ],
                "llm_profiles": {},
                "review_policy": {"enabled": True, "engine": "review-seat"},
            }

        resolve = get

    snapshot = build_binding_snapshot(
        {
            "challenge": {"name": "binding target", "category": "web"},
            "engines": ["worker-seat"],
            "llm_profiles": {
                "reason": {
                    "provider_ref": "reason-relay",
                    "model": "reason-model",
                    "api_key": "REASON-SECRET",
                },
                "worker": {
                    "provider_ref": "run-worker-relay",
                    "model": "run-worker-model",
                    "auth_mode": "header",
                    "auth_header": "X-Run-Key",
                    "auth_prefix": "Token",
                    "credential_source": "account",
                    "credential_account": "run-worker-account",
                    "api_key": "WORKER-SECRET",
                },
            },
            "review_policy": {"enabled": True, "engine": "review-seat"},
        },
        _Config(),
    )

    assert snapshot["roles"]["reason"] == {
        "provider_ref": "reason-relay", "model": "reason-model",
    }
    assert snapshot["roles"]["worker"] == {
        "provider_ref": "run-worker-relay",
        "model": "run-worker-model",
        "auth_mode": "header",
        "auth_header": "X-Run-Key",
        "auth_prefix": "Token",
        "credential_source": "account",
        "credential_account": "run-worker-account",
    }
    # No explicit review binding: review_policy.engine selects and freezes the
    # review worker profile so later global config edits cannot retarget this run.
    assert snapshot["roles"]["review"] == {
        "provider_ref": "profile-review-relay",
        "model": "profile-review-model",
        "auth_mode": "bearer",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer",
        "credential_account": "review-account",
    }
    assert snapshot["roles"]["titler"] == {}
    assert not _contains_secret(snapshot)


@pytest.mark.asyncio
async def test_target_run_verified_fact_inheritance_is_explicit_and_restart_safe(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Inheritance target", "category": "web"})
    source = manager.create_new()
    source.target_id = target["id"]
    source.target_name = target["name"]
    source.target_category = target["category"]
    manager.target_store.attach_run(target["id"], source.run_id)
    graph_path = manager.workspace_dir(source.run_id) / "graph" / "shared_graph.db"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = SQLiteSharedGraph.open(
        db_path=graph_path,
        challenge=Challenge(id=source.run_id, name=target["name"], category="web"),
    )
    fact_seq = graph.add_evidence(
        actor="worker-1", source="stdout", fact="/admin requires auth",
        verified=True, artifact_id="artifact-1", witness="substring",
        verifier="provenance_gate",
    )
    graph.close()

    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The default is clean: a new Run must not silently inherit Target facts.
        clean = await client.post(f"/api/targets/{target['id']}/runs", json={})
        assert clean.status_code == 200
        assert clean.json()["run"]["inherited_verified_fact_count"] == 0

        projected = (await client.get(f"/api/targets/{target['id']}/facts")).json()["facts"]
        assert projected and projected[0]["fact_id"] == f"fact:{source.run_id}:{fact_seq}"

        selected = await client.post(
            f"/api/targets/{target['id']}/runs",
            json={"verified_fact_ids": [projected[0]["fact_id"]]},
        )
        assert selected.status_code == 200
        inherited = selected.json()["run"]["inherited_verified_facts"]
        assert len(inherited) == 1
        assert inherited[0]["source_run_id"] == source.run_id
        assert inherited[0]["fact_seq"] == fact_seq
        assert inherited[0]["provenance_status"] == "verified"
        refreshed = (await client.get(f"/api/targets/{target['id']}/facts")).json()["facts"]
        reverse = next(item for item in refreshed if item["fact_id"] == projected[0]["fact_id"])
        assert reverse["inherited_by_run_ids"] == [selected.json()["run"]["run_id"]]
        assert reverse["inherited_by_run_count"] == 1

    # The snapshot survives before /start, including a backend restart.
    restarted = RunManager(sessions_root=tmp_path)
    restored = restarted.get(selected.json()["run"]["run_id"])
    assert restored is not None
    assert restored.inherited_verified_facts[0]["source_run_id"] == source.run_id
    assert restored.inherited_verified_facts[0]["fact_seq"] == fact_seq


@pytest.mark.asyncio
async def test_target_inherited_facts_are_visible_to_existing_cli_dispatch_as_context(tmp_path, monkeypatch):
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({"name": "Context target", "category": "web", "description": "inspect portal", "target_address": "https://portal.test", "flag_format_hint": "flag{...}"})
    manager.target_store.add_hint(target["id"], "check the upload path before brute force")
    run = manager.create_new()
    run.target_id = target["id"]
    run.target_name = target["name"]
    run.target_category = target["category"]
    manager.target_store.attach_run(target["id"], run.run_id)
    run.inherited_verified_facts = [{
        "fact_id": "fact:old:3", "fact": "/admin requires auth",
        "provenance_status": "verified", "source_run_id": "run-old", "fact_seq": 3,
    }]
    manager.remember_run_context(run.run_id, {"inherited_verified_facts": run.inherited_verified_facts})

    captured = {}
    def fake_build_driver(body, mgr=None):
        captured["body"] = body
        async def fake_driver(_run):
            return None
        fake_driver.dispatch_kind = "mock"
        return fake_driver

    async def fake_start(run_id, driver):
        captured["started"] = run_id

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(manager, "start", fake_start)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run.run_id}/start",
            json={"kind": "mock", "prompt": "inspect portal", "challenge": {"name": target["name"], "category": "web", "description": "inspect portal"}},
        )
    assert response.status_code == 200
    body = captured["body"]
    assert body["inherited_verified_facts"][0]["source_run_id"] == "run-old"
    assert "READ-ONLY VERIFIED FACTS" in body["challenge"]["description"]
    assert "not accepted flags or new evidence" in body["prompt"]
    assert body["challenge"]["target"] == "https://portal.test"
    assert body["challenge"]["flag_format_hint"] == "flag{...}"
    assert "[OPERATOR HINTS FROM TARGET CARD]" in body["prompt"]
    assert "check the upload path before brute force" in body["challenge"]["description"]
    assert captured["started"] == run.run_id


@pytest.mark.asyncio
async def test_target_projection_redacts_nested_run_flags_without_operator_auth(tmp_path):
    """Target-first APIs must not bypass the run rail's flag containment."""
    manager = RunManager(sessions_root=tmp_path)
    app = create_app(manager)
    app.state.auth = AuthConfig(password="", secret=b"test-secret")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        target = (await client.post(
            "/api/targets", json={"name": "Redaction target", "category": "web"}
        )).json()["target"]
        run = manager.create_new()
        run.target_id = target["id"]
        manager.target_store.attach_run(target["id"], run.run_id)
        await run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED,
            run_id=run.run_id,
            payload={"solved": True, "flag": "flag{must-not-leak}", "flags": ["flag{must-not-leak}"]},
        ))

        response = await client.get(f"/api/targets/{target['id']}")
        assert response.status_code == 200
        nested = response.json()["target"]["runs"][0]
        assert nested["flag"] is None
        assert nested["flags"] == []
        assert nested["flag_count"] == 1
        assert nested["flags_redacted"] is True
        assert "must-not-leak" not in response.text

        listed = await client.get(f"/api/targets/{target['id']}/runs")
        assert listed.status_code == 200
        assert listed.json()["runs"][0]["flags_redacted"] is True


@pytest.mark.asyncio
async def test_target_hint_opt_out_is_preserved_by_start(tmp_path, monkeypatch):
    """A Launch Wizard opt-out must not be re-expanded by the start route."""
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({
        "name": "Hint opt-out target", "category": "web", "description": "inspect portal",
    })
    manager.target_store.add_hint(target["id"], "try the upload path")
    manager.target_store.add_hint(target["id"], "check the debug endpoint")

    captured = {}

    def fake_build_driver(body, mgr=None):
        captured["body"] = body

        async def fake_driver(_run):
            return None

        fake_driver.dispatch_kind = "mock"
        return fake_driver

    async def fake_start(run_id, driver):
        captured["started"] = run_id

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(manager, "start", fake_start)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/targets/{target['id']}/runs",
            json={"include_target_hints": False, "target_hint_ids": []},
        )
        assert created.status_code == 200
        run_id = created.json()["run"]["run_id"]
        response = await client.post(
            f"/api/runs/{run_id}/start",
            json={
                "kind": "mock",
                "prompt": "inspect portal",
                "challenge": {"name": target["name"], "category": "web", "description": "inspect portal"},
            },
        )

    assert response.status_code == 200
    assert "OPERATOR HINTS FROM TARGET CARD" not in captured["body"]["prompt"]
    assert "try the upload path" not in captured["body"]["prompt"]
    assert captured["started"] == run_id


@pytest.mark.asyncio
async def test_target_hint_selection_uses_stable_ids(tmp_path, monkeypatch):
    """Only the selected Target hint ids are injected, independent of list order."""
    manager = RunManager(sessions_root=tmp_path)
    target = manager.target_store.create({
        "name": "Hint selection target", "category": "web", "description": "inspect portal",
    })
    first = manager.target_store.add_hint(target["id"], "first lead")
    second = manager.target_store.add_hint(target["id"], "second lead")
    assert first and second

    captured = {}

    def fake_build_driver(body, mgr=None):
        captured["body"] = body

        async def fake_driver(_run):
            return None

        fake_driver.dispatch_kind = "mock"
        return fake_driver

    async def fake_start(run_id, driver):
        return None

    monkeypatch.setattr("apps.web.drivers.build_driver", fake_build_driver)
    monkeypatch.setattr(manager, "start", fake_start)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            f"/api/targets/{target['id']}/runs",
            json={"include_target_hints": True, "target_hint_ids": [second["id"]]},
        )
        assert created.status_code == 200
        run_id = created.json()["run"]["run_id"]
        response = await client.post(
            f"/api/runs/{run_id}/start",
            json={"kind": "mock", "prompt": "inspect portal", "challenge": {"category": "web"}},
        )

    assert response.status_code == 200
    prompt = captured["body"]["prompt"]
    assert "second lead" in prompt
    assert "first lead" not in prompt

    # An explicit empty selector is different from an omitted selector: it
    # means the operator enabled the hint feature but selected zero notes.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.post(
            f"/api/targets/{target['id']}/runs",
            json={"include_target_hints": True, "target_hint_ids": []},
        )
        empty_run_id = empty.json()["run"]["run_id"]
        empty_start = await client.post(
            f"/api/runs/{empty_run_id}/start",
            json={"kind": "mock", "prompt": "inspect portal", "challenge": {"category": "web"}},
        )

    assert empty.status_code == 200
    assert empty_start.status_code == 200
    assert "OPERATOR HINTS FROM TARGET CARD" not in captured["body"]["prompt"]
    assert "first lead" not in captured["body"]["prompt"]
    assert "second lead" not in captured["body"]["prompt"]


@pytest.mark.asyncio
async def test_target_review_snapshot_aggregates_real_data(tmp_path):
    """The deck's review modal gets all nine fields from actual run data."""
    manager = RunManager(sessions_root=tmp_path)
    app = create_app(manager)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        target = (
            await client.post("/api/targets", json={"name": "Review target", "category": "web"})
        ).json()["target"]
        run_resp = await client.post(
            f"/api/targets/{target['id']}/runs", json={"direction": "Auth"}
        )
        assert run_resp.status_code == 200
        run_id = run_resp.json()["run"]["run_id"]

        response = await client.get(f"/api/targets/{target['id']}/review")
        assert response.status_code == 200
        snapshot = response.json()
        assert set(snapshot) == {
            "summary",
            "known_facts",
            "attempted_routes",
            "failures_runtime",
            "evidence_refs",
            "intent_decisions",
            "usage_ledger",
            "provenance_chain",
        }
        assert snapshot["summary"]["target_id"] == target["id"]
        assert snapshot["summary"]["run_count"] == 1
        assert snapshot["summary"]["terminal_run_count"] == 0
        ledger_row = next(item for item in snapshot["usage_ledger"] if item["run_id"] == run_id)
        assert ledger_row["status"] in ("draft", "queued", "running", "paused")
        route_texts = {item["text"] for item in snapshot["attempted_routes"]}
        assert "Auth" in route_texts
