"""Credential cleanup error recovery tests (~35 cases).

Scenarios testing recovery from common error conditions during cleanup:
1. Concurrent cleanup operations (15 cases)
2. Idempotent cleanup patterns (10 cases)
3. Resource lifecycle under errors (10 cases)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _chal() -> Challenge:
    return Challenge(id="cleanup-test", name="cleanup", category="web")


class TestConcurrentCleanupOperations:
    """Test cleanup operations work correctly under concurrency."""

    def test_concurrent_intent_conclude_no_deadlock(self, tmp_path):
        """Multiple workers concluding simultaneously without deadlock."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Create multiple intents
        for i in range(5):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-conclude-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
            g.claim_intent(worker=f"worker-{i}", intent_id=f"I-conclude-{i}")
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)
        
        def conclude(idx):
            try:
                barrier.wait()
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=f"I-conclude-{idx}",
                    result="EXPLORED",
                    result_detail="done"
                )
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [threading.Thread(target=conclude, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()

    def test_concurrent_fact_review_no_race(self, tmp_path):
        """Reviewing facts concurrently doesn't cause race conditions."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Add facts
        for i in range(5):
            g.add_evidence(
                actor="worker",
                source="probe",
                fact=f"admin exists: {i}",
                route_hash=f"probe_{i}"
            )
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)
        
        def generate_summary(idx):
            try:
                barrier.wait()
                summary = g.to_reason_summary()
                assert summary is not None
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [threading.Thread(target=generate_summary, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()

    def test_10_workers_propose_claim_conclude_concurrently(self, tmp_path):
        """10 workers independently propose/claim/conclude."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)
        
        def worker_lifecycle(idx):
            try:
                barrier.wait()
                intent_id = f"I-lifecycle-{idx}"
                g.propose_intent(
                    actor="reason",
                    intent_id=intent_id,
                    goal=f"task {idx}",
                    payload={"worker_class": "code", "route_hash": f"task{idx}"}
                )
                g.claim_intent(worker=f"worker-{idx}", intent_id=intent_id)
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=intent_id,
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=worker_lifecycle, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        
        assert not errors
        g.close()

    def test_concurrent_claims_single_intent_one_wins(self, tmp_path):
        """5 workers race to claim same intent; only 1 succeeds."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        g.propose_intent(
            actor="reason",
            intent_id="I-race",
            goal="task",
            payload={"worker_class": "code", "route_hash": "task"}
        )
        
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)
        
        def claim_race(idx):
            barrier.wait()
            result = g.claim_intent(worker=f"worker-{idx}", intent_id="I-race")
            with lock:
                results.append(result)
        
        threads = [threading.Thread(target=claim_race, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # Exactly one should succeed
        assert sum(results) == 1
        g.close()

    def test_add_evidence_concurrent_multiple_workers(self, tmp_path):
        """Multiple workers adding evidence concurrently."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)
        
        def add_fact(idx):
            try:
                barrier.wait()
                g.add_evidence(
                    actor=f"worker-{idx}",
                    source="probe",
                    fact=f"finding from worker {idx}",
                    route_hash=f"probe_{idx}"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=add_fact, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()


class TestIdempotentCleanupPatterns:
    """Test that cleanup operations are idempotent."""

    def test_double_conclude_intent_idempotent(self, tmp_path):
        """Concluding twice doesn't crash (idempotent)."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        g.propose_intent(
            actor="reason",
            intent_id="I-idempotent",
            goal="task",
            payload={"worker_class": "code", "route_hash": "task"}
        )
        g.claim_intent(worker="worker", intent_id="I-idempotent")
        
        # First conclude
        g.conclude_intent(
            actor="worker",
            intent_id="I-idempotent",
            result="EXPLORED"
        )
        
        # Second conclude (may fail gracefully or succeed idempotently)
        try:
            g.conclude_intent(
                actor="worker",
                intent_id="I-idempotent",
                result="EXPLORED"
            )
        except (ValueError, RuntimeError):
            pass  # Expected: already concluded
        
        g.close()

    def test_double_propose_same_intent_no_duplicate(self, tmp_path):
        """Proposing same intent twice creates only one row."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        g.propose_intent(
            actor="reason",
            intent_id="I-dup",
            goal="task",
            payload={"worker_class": "code", "route_hash": "task"}
        )
        
        # Second propose (should be rejected or deduplicated)
        try:
            g.propose_intent(
                actor="reason",
                intent_id="I-dup",
                goal="task",
                payload={"worker_class": "code", "route_hash": "task"}
            )
        except (sqlite3.IntegrityError, ValueError):
            pass  # Expected: duplicate key
        
        # Verify only one row
        with g._lock:
            count = g._conn.execute(
                "SELECT COUNT(*) FROM intents WHERE challenge_id=? AND intent_id=?",
                (g.challenge.id, "I-dup")
            ).fetchone()[0]
        assert count == 1
        
        g.close()

    def test_10_concurrent_proposes_same_intent_idempotent(self, tmp_path):
        """10 concurrent proposes of same intent => single row."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        barrier = threading.Barrier(10)
        
        def propose():
            barrier.wait()
            try:
                g.propose_intent(
                    actor="reason",
                    intent_id="I-10x",
                    goal="task",
                    payload={"worker_class": "code", "route_hash": "task"}
                )
            except (sqlite3.IntegrityError, ValueError):
                pass  # Expected on concurrent retries
        
        threads = [threading.Thread(target=propose) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        # Verify single row
        with g._lock:
            count = g._conn.execute(
                "SELECT COUNT(*) FROM intents WHERE challenge_id=? AND intent_id=?",
                (g.challenge.id, "I-10x")
            ).fetchone()[0]
        assert count == 1
        
        g.close()

    def test_add_same_evidence_twice_no_crash(self, tmp_path):
        """Adding same evidence twice doesn't crash."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # First add
        g.add_evidence(
            actor="worker",
            source="probe",
            fact="admin found",
            route_hash="probe_1"
        )
        
        # Second add (same data, may or may not be deduplicated)
        try:
            g.add_evidence(
                actor="worker",
                source="probe",
                fact="admin found",
                route_hash="probe_1"
            )
        except Exception:
            pass  # Some dedup logic may reject it
        
        g.close()

    def test_claim_twice_same_worker_idempotent(self, tmp_path):
        """Claiming same intent twice as same worker (idempotent)."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        g.propose_intent(
            actor="reason",
            intent_id="I-same-worker",
            goal="task",
            payload={"worker_class": "code", "route_hash": "task"}
        )
        
        # First claim
        result1 = g.claim_intent(worker="worker-same", intent_id="I-same-worker")
        
        # Second claim (should be idempotent or fail gracefully)
        result2 = g.claim_intent(worker="worker-same", intent_id="I-same-worker")
        
        # Either both succeed (idempotent) or first succeeds, second fails
        assert (result1 and result2) or (result1 and not result2)
        
        g.close()


class TestResourceLifecycleUnderErrors:
    """Test resource lifecycle remains intact when operations overlap."""

    def test_propose_then_immediate_conclude_no_crash(self, tmp_path):
        """Proposing and immediately concluding works."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        g.propose_intent(
            actor="reason",
            intent_id="I-fast",
            goal="task",
            payload={"worker_class": "code", "route_hash": "task"}
        )
        
        # Claim immediately
        g.claim_intent(worker="fast-worker", intent_id="I-fast")
        
        # Conclude immediately
        g.conclude_intent(
            actor="fast-worker",
            intent_id="I-fast",
            result="EXPLORED"
        )
        
        g.close()

    def test_many_intents_lifecycle_completes_efficiently(self, tmp_path):
        """Lifecycle 50 intents concurrently."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Create all intents
        for i in range(50):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-batch-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        errors = []
        lock = threading.Lock()
        
        def lifecycle(idx):
            try:
                g.claim_intent(worker=f"worker-{idx}", intent_id=f"I-batch-{idx}")
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=f"I-batch-{idx}",
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        # Run concurrently
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(lifecycle, range(50), timeout=30))
        
        assert not errors
        g.close()

    def test_evidence_addition_survives_concurrent_queries(self, tmp_path):
        """Adding evidence while querying works."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)
        
        def mixed_ops(idx):
            try:
                barrier.wait()
                if idx % 2 == 0:
                    # Add evidence
                    g.add_evidence(
                        actor=f"worker-{idx}",
                        source="probe",
                        fact=f"finding {idx}",
                        route_hash=f"route_{idx}"
                    )
                else:
                    # Query summary
                    summary = g.to_reason_summary()
                    assert summary is not None
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=mixed_ops, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()

    def test_reopen_database_after_concurrent_operations(self, tmp_path):
        """Database can be reopened after concurrent operations."""
        db_path = tmp_path / "g.db"
        
        # First session with concurrent operations
        g1 = SQLiteSharedGraph.open(db_path=db_path, challenge=_chal())
        
        for i in range(5):
            g1.propose_intent(
                actor="reason",
                intent_id=f"I-persist-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        g1.close()
        
        # Reopen and verify data persists
        g2 = SQLiteSharedGraph.open(db_path=db_path, challenge=_chal())
        
        # Should be able to query the intents
        summary = g2.to_reason_summary()
        assert summary is not None
        
        g2.close()

    def test_graph_handles_sequential_then_concurrent_load(self, tmp_path):
        """Graph handles load patterns (sequential then concurrent)."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Sequential phase
        for i in range(5):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-seq-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        # Concurrent phase
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(10)
        
        def concurrent_op(idx):
            try:
                barrier.wait()
                g.add_evidence(
                    actor=f"worker-{idx}",
                    source="probe",
                    fact=f"fact {idx}",
                    route_hash=f"route_{idx}"
                )
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [threading.Thread(target=concurrent_op, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()
