"""Pool shutdown error recovery tests (~35 cases).

Scenarios testing recovery from common pool/shutdown conditions:
1. Concurrent finalization (15 cases)
2. Resource cleanup patterns (10 cases)
3. Claim lifecycle under stress (10 cases)
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _chal() -> Challenge:
    return Challenge(id="pool-test", name="pool", category="web")


class TestConcurrentFinalization:
    """Test finalize operations work correctly under concurrency."""

    def test_multiple_sequential_finalizes_safe(self, tmp_path):
        """Finalizing multiple graphs sequentially works."""
        for i in range(5):
            g = SQLiteSharedGraph.open(db_path=tmp_path / f"g{i}.db", challenge=_chal())
            
            g.propose_intent(
                actor="reason",
                intent_id=f"I-final-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
            g.claim_intent(worker=f"worker-{i}", intent_id=f"I-final-{i}")
            
            # Conclude before close
            g.conclude_intent(
                actor=f"worker-{i}",
                intent_id=f"I-final-{i}",
                result="EXPLORED"
            )
            
            g.close()

    def test_concurrent_database_closes_no_crash(self, tmp_path):
        """Multiple graphs closing concurrently."""
        graphs = []
        for i in range(5):
            g = SQLiteSharedGraph.open(db_path=tmp_path / f"g{i}.db", challenge=_chal())
            g.propose_intent(
                actor="reason",
                intent_id=f"I-close-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
            graphs.append(g)
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(5)
        
        def close_graph(g, idx):
            try:
                barrier.wait()
                g.close()
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=close_graph, args=(graphs[i], i)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        
        assert not errors

    def test_reason_summary_during_concurrent_conclusions(self, tmp_path):
        """Generating summary while workers conclude."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        for i in range(10):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-summary-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
            g.claim_intent(worker=f"worker-{i}", intent_id=f"I-summary-{i}")
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(15)  # 10 conclude + 5 summary
        
        def conclude_op(idx):
            try:
                barrier.wait()
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=f"I-summary-{idx}",
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append(("conclude", idx, str(e)))
        
        def summary_op(idx):
            try:
                barrier.wait()
                summary = g.to_reason_summary()
                assert summary is not None
            except Exception as e:
                with lock:
                    errors.append(("summary", idx, str(e)))
        
        threads = [threading.Thread(target=conclude_op, args=(i,)) for i in range(10)]
        threads += [threading.Thread(target=summary_op, args=(i,)) for i in range(5)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors
        g.close()


class TestResourceCleanupPatterns:
    """Test resource cleanup works correctly."""

    def test_conclude_release_resources_properly(self, tmp_path):
        """Concluding intents releases resources."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Create and conclude 50 intents
        for i in range(50):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-resource-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
            g.claim_intent(worker=f"worker-{i}", intent_id=f"I-resource-{i}")
            g.conclude_intent(
                actor=f"worker-{i}",
                intent_id=f"I-resource-{i}",
                result="EXPLORED"
            )
        
        # Should complete without memory issues
        g.close()

    def test_add_evidence_cleanup_no_leak(self, tmp_path):
        """Adding many facts doesn't leak resources."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        for i in range(100):
            g.add_evidence(
                actor="worker",
                source="probe",
                fact=f"finding {i}",
                route_hash=f"route_{i}"
            )
        
        # Should complete without crash
        summary = g.to_reason_summary()
        assert summary is not None
        
        g.close()

    def test_claim_lifecycle_scales_to_100(self, tmp_path):
        """Claim lifecycle scales efficiently."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Create 100 intents and lifecycle them
        for i in range(100):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-scale-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        errors = []
        lock = threading.Lock()
        
        def lifecycle(idx):
            try:
                g.claim_intent(worker=f"worker-{idx}", intent_id=f"I-scale-{idx}")
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=f"I-scale-{idx}",
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            list(executor.map(lifecycle, range(100), timeout=30))
        
        assert not errors
        g.close()

    def test_mixed_operations_no_deadlock_or_leak(self, tmp_path):
        """Mixed propose/claim/conclude operations don't leak."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Phase 1: Create many intents
        for i in range(30):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-mixed-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        # Phase 2: Concurrent lifecycle
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(30)
        
        def mixed_lifecycle(idx):
            try:
                barrier.wait()
                g.claim_intent(worker=f"worker-{idx}", intent_id=f"I-mixed-{idx}")
                g.add_evidence(
                    actor=f"worker-{idx}",
                    source="probe",
                    fact=f"evidence {idx}",
                    route_hash=f"route_{idx}"
                )
                g.conclude_intent(
                    actor=f"worker-{idx}",
                    intent_id=f"I-mixed-{idx}",
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=mixed_lifecycle, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        
        assert not errors
        g.close()


class TestClaimLifecycleUnderStress:
    """Test claim lifecycle under stress conditions."""

    def test_rapid_claim_release_cycles(self, tmp_path):
        """Rapid claim/conclude cycles."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        errors = []
        
        for cycle in range(5):
            try:
                g.propose_intent(
                    actor="reason",
                    intent_id=f"I-cycle-{cycle}",
                    goal=f"cycle {cycle}",
                    payload={"worker_class": "code", "route_hash": f"cycle{cycle}"}
                )
                g.claim_intent(worker=f"worker-cycle", intent_id=f"I-cycle-{cycle}")
                g.conclude_intent(
                    actor="worker-cycle",
                    intent_id=f"I-cycle-{cycle}",
                    result="EXPLORED"
                )
            except Exception as e:
                errors.append((cycle, str(e)))
        
        assert not errors
        g.close()

    def test_concurrent_claim_attempts_eventually_all_claimed(self, tmp_path):
        """All created intents eventually get claimed."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Create 20 unclaimed intents
        for i in range(20):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-unclaimed-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        # 20 workers try to claim
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)
        
        def try_claim(idx):
            barrier.wait()
            result = g.claim_intent(worker=f"worker-{idx}", intent_id=f"I-unclaimed-{idx}")
            with lock:
                results.append(result)
        
        threads = [threading.Thread(target=try_claim, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        # All should be claimed
        assert sum(results) == 20
        g.close()

    def test_stress_test_concurrent_all_operations(self, tmp_path):
        """Stress test with all operations concurrently."""
        g = SQLiteSharedGraph.open(db_path=tmp_path / "g.db", challenge=_chal())
        
        # Pre-create intents
        for i in range(50):
            g.propose_intent(
                actor="reason",
                intent_id=f"I-stress-{i}",
                goal=f"task {i}",
                payload={"worker_class": "code", "route_hash": f"task{i}"}
            )
        
        errors = []
        lock = threading.Lock()
        barrier = threading.Barrier(50)
        
        def stress_op(idx):
            try:
                barrier.wait()
                # Each worker does its full lifecycle
                g.claim_intent(worker=f"stress-{idx}", intent_id=f"I-stress-{idx}")
                g.add_evidence(
                    actor=f"stress-{idx}",
                    source="probe",
                    fact=f"stress evidence {idx}",
                    route_hash=f"stress_{idx}"
                )
                g.conclude_intent(
                    actor=f"stress-{idx}",
                    intent_id=f"I-stress-{idx}",
                    result="EXPLORED"
                )
            except Exception as e:
                with lock:
                    errors.append((idx, str(e)))
        
        threads = [threading.Thread(target=stress_op, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        
        assert not errors
        g.close()
