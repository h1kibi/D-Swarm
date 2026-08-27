from pathlib import Path

from dswarm.swarm.blackboard_bridge import BlackboardBridgeMixin


class _Bridge(BlackboardBridgeMixin):
    pass


def test_verified_poc_bridge_payloads_do_not_expose_sensitive_command_or_output_fields():
    bridge = _Bridge()

    verified = bridge._graph_event_to_bb({
        "seq": 17,
        "kind": "poc_verified",
        "payload": {
            "poc_id": "poc-1",
            "reproduction_id": "repro-1",
            "verification_id": "ver-1",
            "exit_code": 0,
            "observed_location": "stdout",
            "provenance_artifact_ids": ["artifact-1"],
            "elapsed_ms": 12,
            "command": "python3 /workspace/poc.py",
            "entry_command": "python3 /workspace/poc.py",
            "stdout": "secret output",
            "stderr": "secret error",
        },
    })
    assert verified == [
        (
            "poc_verified",
            {
                "seq": 17,
                "poc_id": "poc-1",
                "reproduction_id": "repro-1",
                "verification_id": "ver-1",
                "status": "verified",
                "exit_code": 0,
                "observed_location": "stdout",
                "provenance_artifact_ids": ["artifact-1"],
                "elapsed_ms": 12,
            },
        )
    ]

    failed = bridge._graph_event_to_bb({
        "seq": 18,
        "kind": "poc_verification_failed",
        "payload": {
            "poc_id": "poc-1",
            "reproduction_id": "repro-1",
            "verification_id": "ver-1",
            "reason": "execution_error",
            "diagnostics": "host subprocess must not leak",
            "command": "python3 /workspace/poc.py",
            "stdout": "secret output",
            "stderr": "secret error",
        },
    })
    assert failed == [
        (
            "poc_verification_failed",
            {
                "seq": 18,
                "poc_id": "poc-1",
                "reproduction_id": "repro-1",
                "verification_id": "ver-1",
                "status": "failed",
                "reason": "execution_error",
                "exit_code": None,
                "diagnostics": "host subprocess must not leak",
                "elapsed_ms": None,
            },
        )
    ]


def test_verified_poc_m9_marked_implemented_in_kernel_doc():
    docs = Path("docs/10-v4-kernel-improvement-implementation.md").read_text(encoding="utf-8")
    assert "Status (2026-08-19): Verified-PoC M9 implemented and verified; other M9 items remain separate." in docs
