"""Snapshot test for review lifecycle domain isolation.

This test verifies that the ReviewLifecycle module correctly delegates to the
shared graph's persistence layer while keeping review semantics isolated.
"""
import tempfile
from pathlib import Path

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def test_review_lifecycle_delegation():
    """Verify that review methods delegate to ReviewLifecycle correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "test.db"
        challenge = Challenge(id="test-chal", name="Test Challenge", category="web", description="Test")
        graph = SQLiteSharedGraph(db, challenge)
        
        try:
            # Verify that the lifecycle object exists
            assert hasattr(graph, "_review_lifecycle")
            assert graph._review_lifecycle is not None
            
            # Test add_review_finding delegation
            seq = graph.add_review_finding(
                actor="reviewer",
                kind="vulnerability",
                severity="blocker",
                summary="SQL injection in login form",
            )
            assert seq > 0
            
            # Test add_review_proposal delegation
            proposal_seq = graph.add_review_proposal(
                actor="reviewer",
                marker="ROUTE_SUPPRESS",
                payload={"route_hash": "test-route", "reason": "dead end"},
            )
            assert proposal_seq > 0
            
            # Test decide_review_proposal delegation
            decision_seq = graph.decide_review_proposal(
                actor="coordinator",
                proposal_seq=proposal_seq,
                decision="accepted",
                reason="Confirmed dead end",
            )
            assert decision_seq > 0
            
            # Test fact lifecycle methods (add_evidence requires source)
            fact_seq = graph.add_evidence(
                actor="worker-1",
                fact="Found open port 8080",
                source="nmap",
                verified=False,
            )
            assert fact_seq > 0
            
            # Test challenge_fact
            result = graph.challenge_fact(
                actor="reviewer",
                fact_seq=fact_seq,
                reason="Need to verify port is actually open",
                verification_goal="Verify port 8080 is open",
            )
            assert result["fact_seq"] == fact_seq
            assert result["seq"] > 0
            
            # Test revalidate_fact
            revalidate_seq = graph.revalidate_fact(
                actor="reviewer",
                fact_seq=fact_seq,
                reason="Port confirmed open",
            )
            assert revalidate_seq > 0
            
            # Test reject_fact
            fact_seq2 = graph.add_evidence(
                actor="worker-1",
                fact="Found port 9999 open",
                source="nmap",
                verified=False,
            )
            reject_seq = graph.reject_fact(
                actor="reviewer",
                fact_seq=fact_seq2,
                reason="Port 9999 is filtered, not open",
            )
            assert reject_seq > 0
            
            # Test merge_fact
            fact_seq3 = graph.add_evidence(
                actor="worker-2",
                fact="Port 8080 is open",
                source="nmap",
                verified=False,
            )
            merge_seq = graph.merge_fact(
                actor="reviewer",
                from_fact_seq=fact_seq3,
                to_fact_seq=fact_seq,
                reason="Duplicate finding",
            )
            assert merge_seq > 0
            
            # Test supersede_fact
            fact_seq4 = graph.add_evidence(
                actor="worker-1",
                fact="Service version 1.0 on port 8080",
                source="banner",
                verified=False,
            )
            fact_seq5 = graph.add_evidence(
                actor="worker-1",
                fact="Service version 2.0 on port 8080",
                source="banner",
                verified=True,
            )
            supersede_seq = graph.supersede_fact(
                actor="reviewer",
                fact_seq=fact_seq4,
                reason="Superseded by newer version info",
                by_fact_seq=fact_seq5,
            )
            assert supersede_seq > 0
            
            # Test verify_fact
            verify_seq = graph.verify_fact(
                actor="reviewer",
                fact_seq=fact_seq,
                reason="Confirmed by secondary scan",
            )
            assert verify_seq > 0
            
            # Test review_fact unified dispatcher
            fact_seq6 = graph.add_evidence(
                actor="worker-3",
                fact="Admin panel found at /admin",
                source="dirb",
                verified=False,
            )
            
            # Dispatch to challenge
            result = graph.review_fact(
                actor="reviewer",
                fact_seq=fact_seq6,
                action="challenge",
                reason="Need to verify admin panel access",
                verification_goal="Verify /admin is accessible",
            )
            assert result["action"] == "challenge"
            assert result["seq"] > 0
            
            # Dispatch to revalidate
            result = graph.review_fact(
                actor="reviewer",
                fact_seq=fact_seq6,
                action="revalidate",
                reason="Admin panel confirmed",
            )
            assert result["action"] == "revalidate"
            assert result["seq"] > 0
        finally:
            # Close the connection to allow cleanup on Windows
            graph._conn.close()


def test_review_lifecycle_module_isolation():
    """Verify that ReviewLifecycle is properly isolated from SharedGraph."""
    from dswarm.swarm.review_lifecycle import ReviewLifecycle
    
    # Verify that ReviewLifecycle defines its own constants (no circular import)
    assert hasattr(ReviewLifecycle, "__init__")
    
    # Verify module-level constants are defined in review_lifecycle.py
    import dswarm.swarm.review_lifecycle as rl_module
    assert hasattr(rl_module, "EV_REVIEW_FINDING")
    assert hasattr(rl_module, "EV_FACT_CHALLENGED")
    assert hasattr(rl_module, "FACT_STATE_REJECTED")
    assert rl_module.FACT_STATE_REJECTED == "rejected"


def test_shared_graph_line_count_reduction():
    """Document the line count reduction from review lifecycle extraction."""
    shared_graph_path = Path(__file__).parent.parent / "dswarm" / "swarm" / "shared_graph.py"
    review_lifecycle_path = Path(__file__).parent.parent / "dswarm" / "swarm" / "review_lifecycle.py"
    
    assert shared_graph_path.exists()
    assert review_lifecycle_path.exists()
    
    shared_graph_lines = len(shared_graph_path.read_text(encoding="utf-8").splitlines())
    review_lifecycle_lines = len(review_lifecycle_path.read_text(encoding="utf-8").splitlines())
    
    # After 2026-09-02 refactoring:
    # - shared_graph.py reduced from 4854 to ~4724 lines (130 lines removed)
    # - review_lifecycle.py added with 302 lines
    # Net: +172 lines total, but shared_graph.py is more maintainable
    
    assert shared_graph_lines < 4800, (
        f"shared_graph.py grew to {shared_graph_lines} lines after review lifecycle extraction. "
        f"Expected it to stay under 4800 lines."
    )
    
    assert 280 < review_lifecycle_lines < 350, (
        f"review_lifecycle.py is {review_lifecycle_lines} lines. "
        f"Expected approximately 300 lines for the review domain."
    )
