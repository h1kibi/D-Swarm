from dswarm.solver.marker_parser import (
    extract_cleanup_markers,
    extract_fact_witness,
    extract_flag,
    extract_flags,
    extract_need_inputs,
    extract_need_requests,
    extract_poc_repros,
    extract_poc_saves,
    extract_ready_to_submit,
    extract_structured_facts,
    extract_structured_findings,
    fact_witnessed_in_chunk,
    is_bare_raw_flag,
)


def test_marker_parser_behavior_snapshot():
    text = """\
stdout: flag{only-mentioned-in-prose}
FOUND_FLAG=flag{first}
VERIFIED_FACT=endpoint /health returns 200
DEADEND=admin panel returned 404
FINDING_TYPE=ENDPOINT
FINDING_TARGET=/health
FINDING_DATA=status=200
FACT_WITNESS=GET /health -> 200 OK
NEED_INPUT=an operator-provided public VPS
NEED_KIND=external_blocker
READY_TO_SUBMIT=local verifier passed
POC_SAVE=proof.py|python3 proof.py|flag{proof}|reproduction proof
POC_REPRO=proof.py|flag{proof}
CLEANUP=stop_listener:listener-1
FOUND_FLAG=flag{last}
"""

    assert extract_flag(text) == "flag{last}"
    assert extract_flags(text) == ["flag{first}", "flag{last}"]
    assert extract_structured_facts(text) == (
        ["endpoint /health returns 200"],
        ["admin panel returned 404"],
    )
    assert extract_structured_findings(text) == [{
        "kind": "ENDPOINT",
        "target": "/health",
        "data": "status=200",
    }]
    assert extract_fact_witness(text) == "GET /health -> 200 OK"
    assert fact_witnessed_in_chunk(
        "endpoint /health returns 200",
        text,
    ) is True
    assert extract_need_requests(text) == [("an operator-provided public VPS", "external_blocker")]
    assert extract_need_inputs(text) == ["an operator-provided public VPS"]
    assert extract_ready_to_submit(text) == ["local verifier passed"]
    assert extract_poc_saves(text) == [(
        "proof.py", "python3 proof.py", "flag{proof}", "reproduction proof",
    )]
    assert extract_poc_repros(text) == [("proof.py", "flag{proof}")]
    assert extract_cleanup_markers(text) == ["stop_listener:listener-1"]


def test_marker_parser_raw_flag_guard_snapshot():
    assert is_bare_raw_flag("flag{clean_body_123}") is True
    assert is_bare_raw_flag("app{position:fixed}") is False
    assert is_bare_raw_flag("flag{short}") is False
