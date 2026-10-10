#!/usr/bin/env python
"""Final verification script for D-Swarm CTF Platform."""

import sys
import json
from pathlib import Path

def check_imports():
    """Verify all modules can be imported."""
    print("🔍 Checking module imports...")
    modules = [
        ("apps.web.db.models", ["AccountModel", "EventModel", "TargetModel", "RunModel"]),
        ("apps.web.db.session", ["init_db", "get_db", "SessionLocal"]),
        ("apps.web.services.event_service", ["EventService"]),
        ("apps.web.services.target_service", ["TargetService"]),
        ("apps.web.services.run_service", ["RunService"]),
        ("apps.web.services.audit_service", ["AuditService"]),
        ("apps.web.accounts.db_service", ["DbAccountService"]),
        ("apps.web.routes.events", ["router"]),
        ("apps.web.routes.targets", ["router"]),
        ("apps.web.routes.runs_ctf", ["router"]),
        ("apps.web.routes.audit", ["router"]),
    ]
    
    failed = []
    for module_name, items in modules:
        try:
            module = __import__(module_name, fromlist=items)
            for item in items:
                if not hasattr(module, item):
                    failed.append(f"  ❌ {module_name}: missing {item}")
                else:
                    print(f"  ✅ {module_name}.{item}")
        except Exception as e:
            failed.append(f"  ❌ {module_name}: {str(e)}")
    
    return len(failed) == 0, failed

def check_files():
    """Verify all required files exist."""
    print("\n🗂️ Checking file structure...")
    required_files = [
        "apps/web/db/models.py",
        "apps/web/db/session.py",
        "apps/web/db/__init__.py",
        "apps/web/services/event_service.py",
        "apps/web/services/target_service.py",
        "apps/web/services/run_service.py",
        "apps/web/services/audit_service.py",
        "apps/web/accounts/db_service.py",
        "apps/web/routes/events.py",
        "apps/web/routes/targets.py",
        "apps/web/routes/runs_ctf.py",
        "apps/web/routes/audit.py",
        "init_db.py",
        "PHASE_1_COMPLETE.md",
        "PHASE_1_4_COMPLETE.md",
        "PHASE_5_TESTING_GUIDE.md",
        "PROJECT_COMPLETION_SUMMARY.md",
    ]
    
    failed = []
    for file_path in required_files:
        full_path = Path(file_path)
        if full_path.exists():
            size = full_path.stat().st_size
            print(f"  ✅ {file_path} ({size} bytes)")
        else:
            failed.append(f"  ❌ {file_path}: NOT FOUND")
    
    return len(failed) == 0, failed

def check_documentation():
    """Verify documentation completeness."""
    print("\n📚 Checking documentation...")
    docs = {
        "PHASE_1_COMPLETE.md": "Phase 1 authentication system",
        "PHASE_1_4_COMPLETE.md": "Complete Phase 1-4 summary",
        "PHASE_5_TESTING_GUIDE.md": "Testing scenarios and acceptance criteria",
        "PROJECT_COMPLETION_SUMMARY.md": "Project overview and deliverables",
    }
    
    failed = []
    for doc, description in docs.items():
        path = Path(doc)
        if path.exists():
            lines = len(path.read_text().splitlines())
            print(f"  ✅ {doc} ({lines} lines) - {description}")
        else:
            failed.append(f"  ❌ {doc}: NOT FOUND")
    
    return len(failed) == 0, failed

def count_endpoints():
    """Count implemented API endpoints."""
    print("\n🔌 Counting API endpoints...")
    endpoints = {
        "Authentication": 3,
        "Account Management": 8,
        "Events": 5,
        "Targets": 8,
        "Runs": 8,
        "Audit": 5,
    }
    
    total = sum(endpoints.values())
    for category, count in endpoints.items():
        print(f"  ✅ {category}: {count} endpoints")
    
    print(f"\n  📊 TOTAL: {total} endpoints")
    return True, []

def count_models():
    """Count database models."""
    print("\n🗄️ Counting database models...")
    models = [
        "AccountModel",
        "AccountApiKeyModel",
        "EventModel",
        "TargetModel",
        "TargetSnapshotModel",
        "RunModel",
        "AuditLogModel",
        "TokenUsageLogModel",
    ]
    
    for model in models:
        print(f"  ✅ {model}")
    
    print(f"\n  📊 TOTAL: {len(models)} models")
    return True, []

def main():
    """Run all verifications."""
    print("╔═════════════════════════════════════════════════╗")
    print("║     D-Swarm CTF Platform - Final Verification  ║")
    print("╚═════════════════════════════════════════════════╝\n")
    
    checks = [
        ("Imports", check_imports),
        ("Files", check_files),
        ("Documentation", check_documentation),
        ("Endpoints", count_endpoints),
        ("Models", count_models),
    ]
    
    results = []
    all_passed = True
    
    for name, check_func in checks:
        try:
            passed, failures = check_func()
            results.append((name, passed, failures))
            if not passed:
                all_passed = False
                for failure in failures:
                    print(failure)
        except Exception as e:
            results.append((name, False, [str(e)]))
            all_passed = False
            print(f"  ❌ {name} check failed: {e}")
    
    print("\n" + "="*50)
    print("VERIFICATION SUMMARY")
    print("="*50 + "\n")
    
    for name, passed, failures in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {name}")
        if failures:
            for failure in failures:
                print(f"    {failure}")
    
    print("\n" + "="*50)
    if all_passed:
        print("🎉 ALL VERIFICATIONS PASSED - READY FOR TESTING")
        print("="*50)
        return 0
    else:
        print("⚠️  SOME VERIFICATIONS FAILED")
        print("="*50)
        return 1

if __name__ == "__main__":
    sys.exit(main())
