import pytest
from types import SimpleNamespace
from dswarm.swarm.shared_graph import SQLiteSharedGraph
from dswarm.models.solve_graph import Challenge
from dswarm.swarm.reason_scheduler import ReasonSwarm, DispatchDecision

def _chal():
    return Challenge(id="test-chal", name="Test Challenge", category="web", points=100)

def test_reviewer_report_quarantine_and_raw_artifact_linkage(tmp_path):
    g = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())
    f_seq = g.add_evidence(actor="pi-worker-1", source="pi", fact="Found /admin endpoint", verified=False, review_required=True, artifact_id="tool-art-1")
    d_seq = g.add_dead_end(actor="pi-worker-1", reason="admin SQLi filtered", review_required=True, artifact_id="tool-art-2")
    
    # 报告必须携带 raw_artifact_ids 与 final transcript 引用并保持隔离
    rep_seq = g.worker_report_pending_review(
        actor="pi-worker-1",
        worker_id="pi-worker-1",
        intent_id="I-test-1",
        fact_seqs=[f_seq],
        dead_end_seqs=[d_seq],
        artifact_ids=["transcript-art-1"],
        raw_artifact_ids=["tool-art-1", "tool-art-2"],
        report_ref="fact:1",
    )
    assert rep_seq > 0
    
    # 尚未审查时，事实与死胡同对 Reason 不可见
    ctx = g.reviewed_context()
    verified_facts = [f for f in ctx["facts"] if f["status"] == "verified"]
    assert len(verified_facts) == 0
    assert len(ctx["dead_ends"]) == 0
    
    # Reviewer 审查通过
    dec_seq = g.decide_evidence_review(
        actor="reviewer",
        decision="verified",
        subject_id=f"fact:{f_seq}",
        fact_seq=f_seq,
        evidence_refs=["artifact:tool-art-1"],
        reason="Supported by raw curl stdout",
    )
    assert dec_seq > 0
    
    # 审查通过后事实进入 reviewed context
    ctx2 = g.reviewed_context()
    verified2 = [f for f in ctx2["facts"] if f["status"] == "verified"]
    assert len(verified2) == 1
    assert verified2[0]["fact_seq"] == f_seq

def test_intent_quality_decision_is_append_only(tmp_path):
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())

    seq = graph.decide_intent_quality(
        actor="reviewer", intent_id="intent-1", decision="blocked",
        reason="No reviewed facts support the proposal", evidence_refs=[],
        reason_cycle_id="reason-1",
    )

    assert seq > 0
    decisions = graph.intent_quality_decisions()
    assert decisions == [{
        "intent_id": "intent-1", "decision": "blocked",
        "reason": "No reviewed facts support the proposal", "replacement": None,
        "evidence_refs": [], "reason_cycle_id": "reason-1", "reviewer": "reviewer",
        "seq": seq, "actor": "reviewer",
    }]

@pytest.mark.asyncio
async def test_swarm_reviewer_accepts_fenced_json_response(tmp_path):
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.swarm.swarm import Swarm

    class FakeLLM:
        async def chat(self, **kwargs):
            return SimpleNamespace(content='```json\n{"decision":"approved","reason":"narrow and justified","evidence_refs":[]}\n```')

    swarm = Swarm(
        _chal(),
        llm=FakeLLM(),
        review_llm=FakeLLM(),
        sandbox=SandboxManager(root=tmp_path / "sandbox"),
        llm_profiles={"review": {"model": "glm-5.3-flash"}},
        review_policy={"enabled": True},
    )
    decision = DispatchDecision(
        intent_id="I-fenced", profile="pi-worker", direction="web",
        goal="Initial recon", worker_class="code", mode="explore",
        priority=1.0, dedupe_key="fenced",
    )
    result = await swarm._review_intent_quality(decision, "cycle-1")
    assert result["decision"] == "approved"
@pytest.mark.asyncio
async def test_quality_gate_rejects_and_revises():
    decision = DispatchDecision(
        intent_id="I-prop-1",
        profile="pi-worker",
        direction="web",
        goal="Duplicate attack",
        worker_class="code",
        mode="explore",
        priority=1.0,
        dedupe_key="prop:1",
    )
    async def fake_review_fn(dec, cycle_id):
        return {
            "decision": "revise",
            "reason": "Too broad; narrow down",
            "replacement": {
                "goal": "Narrow scan on /admin",
                "worker_class": "code",
                "mode": "explore",
                "profile": "pi-worker",
                "from_facts": [],
                "rationale": "narrow",
            }
        }
    scheduler = ReasonSwarm(
        challenge=_chal(),
        intent_review_fn=fake_review_fn,
    )
    res = await scheduler._quality_gate(decision, reason_cycle_id="cycle-1")
    assert res is not None
    assert res.goal == "Narrow scan on /admin"
    assert res.intent_id == "I-prop-1:rev:cycle-1"

@pytest.mark.asyncio
async def test_role_fallback_requires_three_failures_and_uses_frozen_default(tmp_path):
    from dswarm.models.solve_graph import Challenge
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.swarm.swarm import Swarm

    swarm = Swarm(
        Challenge(id="fallback", name="fallback", category="web"),
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sandbox"),
        binding_snapshot={
            "roles": {"worker": {"model": "override-model"}},
            "fallback": {"worker": {"model": "settings-model", "provider": "default"}},
        },
        llm_profiles={"worker": {"model": "override-model"}},
    )
    assert await swarm.trigger_role_fallback("worker", reason="timeout") is False
    assert await swarm.trigger_role_fallback("worker", reason="timeout") is False
    assert await swarm.trigger_role_fallback("worker", reason="timeout") is True
    assert swarm._effective_role_bindings["worker"]["model"] == "settings-model"
    assert await swarm.trigger_role_fallback("worker", reason="again", is_fatal=True) is False
    assert swarm.role_bindings["worker"]["model"] == "override-model"

@pytest.mark.asyncio
async def test_quality_gate_blocks_business_intent_when_reviewer_missing():
    decision = DispatchDecision(
        intent_id="I-no-review", profile="pi-worker", direction="web",
        goal="probe the admin route", worker_class="code", mode="explore",
        priority=1.0, dedupe_key="no-review",
    )
    scheduler = ReasonSwarm(_chal(), require_reviewer=True)
    assert await scheduler._quality_gate(decision, reason_cycle_id="cycle-1") is None


@pytest.mark.asyncio
async def test_quality_gate_rejects_invalid_revise_without_dispatch():
    decision = DispatchDecision(
        intent_id="I-invalid-revise", profile="pi-worker", direction="web",
        goal="probe /admin", worker_class="code", mode="explore",
        priority=1.0, dedupe_key="invalid-revise",
    )

    async def review(_decision, _cycle):
        return {"decision": "revise", "reason": "missing narrower task", "replacement": {"goal": ""}}

    scheduler = ReasonSwarm(_chal(), intent_review_fn=review)
    assert await scheduler._quality_gate(decision, reason_cycle_id="cycle-1") is None


@pytest.mark.asyncio
async def test_quality_gate_revise_must_be_strictly_narrower():
    """A `revise` replacement that widens the goal or leaves the reviewed fact
    basis is not a narrower task — it must be blocked, not dispatched."""
    decision = DispatchDecision(
        intent_id="I-broaden", profile="pi-worker", direction="web",
        goal="probe /admin", worker_class="code", mode="explore",
        priority=1.0, dedupe_key="broaden", from_facts=[7],
    )

    async def wider(_decision, _cycle):
        return {
            "decision": "revise",
            "reason": "try everything",
            "replacement": {"goal": "x" * 400, "worker_class": "code", "from_facts": [7]},
        }

    async def new_facts(_decision, _cycle):
        return {
            "decision": "revise",
            "reason": "unreviewed basis",
            "replacement": {"goal": "narrow /admin login", "worker_class": "code", "from_facts": [99]},
        }

    assert await ReasonSwarm(_chal(), intent_review_fn=wider)._quality_gate(
        decision, reason_cycle_id="cycle-1") is None
    assert await ReasonSwarm(_chal(), intent_review_fn=new_facts)._quality_gate(
        decision, reason_cycle_id="cycle-1") is None


@pytest.mark.asyncio
async def test_quality_gate_reviewer_call_failure_blocks_business_intent(tmp_path):
    """A Reviewer transport/parse failure is fail-closed: the business Intent is
    blocked, the quality decision is recorded append-only, and the scheduler
    reports the Reviewer as unavailable (never "trust the Worker")."""
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())

    async def broken_review(_decision, _cycle):
        raise RuntimeError("review provider 500")

    scheduler = ReasonSwarm(
        _chal(), graph=graph, intent_review_fn=broken_review, require_reviewer=True,
    )
    decision = DispatchDecision(
        intent_id="I-review-fail", profile="pi-worker", direction="web",
        goal="probe /admin", worker_class="code", mode="explore",
        priority=1.0, dedupe_key="review-fail",
    )
    assert await scheduler._quality_gate(decision, reason_cycle_id="cycle-1") is None
    assert scheduler._reviewer_blocked is True
    decisions = graph.intent_quality_decisions()
    assert decisions == [{
        "intent_id": "I-review-fail", "decision": "blocked",
        "reason": "reviewer unavailable", "replacement": None,
        "evidence_refs": [], "reason_cycle_id": "cycle-1", "reviewer": "reviewer",
        "seq": decisions[0]["seq"], "actor": "reviewer",
    }]


def test_reviewer_candidate_and_rejected_never_become_verified_context(tmp_path):
    """docs/17 §5.2.1: only `verified` promotes business information to Reason.
    A `candidate` stays a pending-verification item and a `rejected` claim is a
    negative signal — neither may appear as a verified fact."""
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())
    cand = graph.add_evidence(
        actor="pi-worker-1", source="pi", fact="maybe /debug is open",
        verified=False, review_required=True, artifact_id="art-cand")
    rej = graph.add_evidence(
        actor="pi-worker-1", source="pi", fact="the flag is flag{guess}",
        verified=False, review_required=True, artifact_id="art-rej")
    verified = graph.add_evidence(
        actor="pi-worker-1", source="pi", fact="/admin requires auth",
        verified=False, review_required=True, artifact_id="art-ok")

    graph.decide_evidence_review(
        actor="reviewer", decision="candidate", fact_seq=cand,
        evidence_refs=["artifact:art-cand"], reason="needs a second probe")
    graph.decide_evidence_review(
        actor="reviewer", decision="rejected", fact_seq=rej,
        evidence_refs=["artifact:art-rej"], reason="contradicted by raw output")
    graph.decide_evidence_review(
        actor="reviewer", decision="verified", fact_seq=verified,
        evidence_refs=["artifact:art-ok"], reason="supported by raw 401")

    ctx = graph.reviewed_context()
    verified_seqs = {row["fact_seq"] for row in ctx["facts"]}
    assert verified_seqs == {verified}
    candidate_seqs = {row["fact_seq"] for row in ctx["candidates"]}
    assert cand in candidate_seqs and rej not in candidate_seqs
    rejected_seqs = {row["fact_seq"] for row in ctx["rejected"]}
    assert rejected_seqs == {rej}
    # The rejected flag guess must never surface as a verified fact.
    assert "flag{guess}" not in str(ctx["facts"])

    # And the Reason prompt: the verified fact is a lead; the rejected claim is
    # only a deny-list entry; the candidate is not stated as known fact.
    summary = graph.to_reason_summary()
    assert "/admin requires auth" in summary
    assert "flag{guess}" not in summary.split("Reviewer rejected claims")[0]
    assert "maybe /debug is open" not in summary.split("Reviewer rejected claims")[0]


def test_reviewer_verified_without_artifact_or_reference_degrades_to_candidate(tmp_path):
    """A `verified` verdict with no durable artifact or no bounded reference is
    downgraded to `candidate` — provenance cannot be asserted by the model."""
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())
    no_artifact = graph.add_evidence(
        actor="pi-worker-1", source="pi", fact="service banner is nginx/1.24",
        verified=False, review_required=True)
    no_ref = graph.add_evidence(
        actor="pi-worker-1", source="pi", fact="db port 5432 reachable",
        verified=False, review_required=True, artifact_id="art-5432")

    graph.decide_evidence_review(
        actor="reviewer", decision="verified", fact_seq=no_artifact,
        evidence_refs=["artifact:whatever"], reason="looks right")
    graph.decide_evidence_review(
        actor="reviewer", decision="verified", fact_seq=no_ref,
        evidence_refs=[], reason="looks right")

    ctx = graph.reviewed_context()
    assert ctx["facts"] == []
    statuses = {row["fact_seq"]: row["status"] for row in ctx["candidates"]}
    assert statuses[no_artifact] == "candidate"
    assert statuses[no_ref] == "candidate"
    decisions = {row["fact_seq"]: row for row in graph.evidence_review_decisions()}
    assert decisions[no_artifact]["requested_decision"] == "verified"
    assert decisions[no_artifact]["decision"] == "candidate"
    assert "requires artifact and evidence reference" in decisions[no_ref]["reason"]


def test_pending_worker_report_absent_from_reason_prompt(tmp_path):
    """A quarantined Worker report (raw prose + its unverified fact claim) must
    not reach the Reason planning prompt before the Reviewer publishes context."""
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "graph.db", challenge=_chal())
    seq = graph.add_evidence(
        actor="pi-worker-1", source="pi",
        fact="I am certain the flag is flag{model_claim}",
        verified=False, review_required=True, artifact_id="art-claim")
    graph.worker_report_pending_review(
        actor="pi-worker-1", worker_id="pi-worker-1", intent_id="I-1",
        fact_seqs=[seq], artifact_ids=["art-claim"],
        raw_artifact_ids=["art-claim"], report_ref="fact:1")

    assert "flag{model_claim}" not in graph.to_reason_summary()
    assert graph._reason_relevant_fact_seqs() == set()
    assert graph.reviewed_context()["facts"] == []

    # Only after an explicit, referenced Reviewer verdict does it promote.
    graph.decide_evidence_review(
        actor="reviewer", decision="verified", fact_seq=seq,
        evidence_refs=["artifact:art-claim"], reason="raw output shows the flag")
    assert "flag{model_claim}" in graph.to_reason_summary()
