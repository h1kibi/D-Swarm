"""
Exception handling audit: lock down the current count and classification
of silent `except Exception: pass` handlers to prevent unintentional drift.

This test enforces that:
1. The total count remains stable (new silent handlers require justification)
2. Files with many handlers are documented in docs/exception-handling.md
3. Changes to handler counts are intentional and reviewed
"""
import ast
from pathlib import Path


def test_silent_exception_handler_count_stable():
    """Lock current count of silent 'except Exception: pass' handlers."""
    root = Path(__file__).parent.parent / "dswarm"
    count = 0
    
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                is_generic = node.type is None or (
                    isinstance(node.type, ast.Name) and node.type.id == "Exception"
                )
                if is_generic and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    count += 1
    
    # Expected count as of 2026-09-02 (from docs/kernel-fixlist B2 disposition)
    # K=30, R=20, T=15, D=55 (D handlers are instrumented but still counted)
    expected = 120
    
    assert count == expected, (
        f"Silent exception handler count changed: expected {expected}, found {count}. "
        f"New handlers require classification (K/R/T/D) and documentation in "
        f"docs/exception-handling.md. See B2 disposition rules."
    )


def test_high_frequency_files_documented():
    """Ensure files with ≥5 silent handlers are listed in the documentation."""
    root = Path(__file__).parent.parent / "dswarm"
    counts_by_file = {}
    
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError):
            continue
        
        file_count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                is_generic = node.type is None or (
                    isinstance(node.type, ast.Name) and node.type.id == "Exception"
                )
                if is_generic and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    file_count += 1
        
        if file_count >= 5:
            rel = path.relative_to(root)
            counts_by_file[str(rel)] = file_count
    
    # Top files documented in docs/exception-handling.md §2 (as of 2026-09-02)
    documented = {
        "swarm\\swarm.py": 30,
        "solver\\cli_solver.py": 24,
        "solver\\btw.py": 10,
        "solver\\container_exec.py": 9,
        "solver\\cli_driver.py": 7,
        "swarm\\reason_scheduler.py": 6,
        "solver\\container_pool.py": 5,
    }
    
    for file, count in counts_by_file.items():
        # Normalize path separators for cross-platform
        normalized = file.replace("/", "\\")
        assert normalized in documented, (
            f"{file} has {count} silent handlers but is not documented in "
            f"docs/exception-handling.md §2. Add it to the file distribution table."
        )
        assert documented[normalized] == count, (
            f"{file} count changed: documented {documented[normalized]}, "
            f"actual {count}. Update docs/exception-handling.md."
        )


def test_durable_write_paths_instrumented():
    """
    Ensure D-class handlers (durable evidence writes) have observability.
    
    This is a smoke check: files with durable writes should use
    *_db_write_failed deltas, not silent pass.
    """
    # Files that had D-class handlers and were instrumented (B2 closed 2026-08-28)
    instrumented_files = [
        "solver/cli_solver.py",      # flag/intent/fact/PoC writes
        "swarm/reason_scheduler.py",  # intent dispatch
        "swarm/swarm.py",             # winner.json persist
        "swarm/review_flow.py",       # review decision
    ]
    
    root = Path(__file__).parent.parent / "dswarm"
    
    for rel_path in instrumented_files:
        path = root / rel_path.replace("/", "\\")
        if not path.exists():
            continue
        
        src = path.read_text(encoding="utf-8")
        
        # Check for observability markers (blackboard delta emit)
        has_observability = any(marker in src for marker in [
            "intent_db_write_failed",
            "fact_db_write_failed",
            "flag_db_write_failed",
            "poc_db_write_failed",
            "review_db_write_failed",
            "winner_persist_failed",
        ])
        
        assert has_observability, (
            f"{rel_path} was instrumented in B2 but lacks *_db_write_failed markers. "
            f"Verify observability has not regressed."
        )
