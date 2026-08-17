from __future__ import annotations

import ast
from dataclasses import asdict, fields
import json
import math
from pathlib import Path

import pytest

from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_experiment import (
    AdvisorReferenceObjective,
    AdvisorSensitiveOutput,
    assess_suggestion,
    build_experimental_summary,
    compare_intent_traces,
    flag_scout_trigger,
    intent_trace_equivalent,
    make_advisor_fixture,
    safe_reason_trace,
)


def _fixture(**overrides):
    values = dict(
        benchmark_run_id="bench-run-1",
        challenge_id="multi-flag-1",
        challenge_mode="ctf",
        expected_flags=3,
        captured_flags_before_source=1,
        source_event_seq=42,
        source_event_ts=1000.0,
        source_intent_id="intent-web-1",
        source_route_hash="route-web",
        next_cycle_id="reason-2",
        graph_summary=(
            "[#10] verified web behavior\n"
            "## Flags already captured\n  - flag{opaque_fixture_secret}"
        ),
        fact_index="10: verified web behavior",
        available_fact_seqs=(10,),
        max_intents=4,
        goal="capture all flags",
        reference_objectives=(
            AdvisorReferenceObjective(
                objective_id="obj-admin",
                route_hash="route-admin",
                goal="inspect the admin branch",
            ),
        ),
    )
    values.update(overrides)
    return make_advisor_fixture(**values)


def _result(*intents: Intent, **overrides) -> ReasonResult:
    values = dict(
        goal_met=False,
        intents=list(intents),
        audit_notes=["audit-secret"],
        verdict="explore",
        drift="drift-secret",
        complete_why="complete-secret",
        pinned_facts=[10, 10],
        dispatches=[{"message": "dispatch-secret"}],
    )
    values.update(overrides)
    return ReasonResult(**values)


def _intent(**overrides) -> Intent:
    values = dict(
        intent_id="intent-admin",
        goal="Inspect the ADMIN branch",
        worker_class="code",
        rationale="rationale-secret",
        from_facts=[10],
        route_hash="route-admin",
        reopen_because="reopen-secret",
        direction="web",
        mode="explore",
        surface_target="surface-secret",
        priority=0.75,
        task_kind="analysis",
        requires_recon=False,
        host_scan=False,
    )
    values.update(overrides)
    return Intent(**values)


def _trace_json(trace) -> str:
    return json.dumps(asdict(trace), sort_keys=True, ensure_ascii=False)


def test_fixture_has_no_dedicated_raw_flag_field_but_preserves_opaque_summary():
    fixture = _fixture()
    names = {item.name for item in fields(type(fixture))}
    assert {"flag", "raw_flag", "flag_value"}.isdisjoint(names)
    assert "flag{opaque_fixture_secret}" in fixture.graph_summary
    assert fixture.summary_digest.startswith("m8-summary::")
    assert fixture.fixture_id.startswith("m8-fixture::")


def test_hidden_references_do_not_change_fixture_identity():
    first = _fixture()
    second = _fixture(reference_objectives=(AdvisorReferenceObjective(
        objective_id="another", route_hash="route-other", goal="other goal"
    ),))
    assert first.fixture_id == second.fixture_id
    assert first.summary_digest == second.summary_digest


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("benchmark_run_id", ""),
        ("challenge_id", ""),
        ("next_cycle_id", ""),
        ("source_event_ts", math.nan),
        ("source_event_ts", math.inf),
        ("expected_flags", 0),
        ("captured_flags_before_source", -1),
        ("captured_flags_before_source", 3),
        ("source_event_seq", 0),
        ("graph_summary", ""),
        ("available_fact_seqs", (True,)),
        ("available_fact_seqs", (0,)),
        ("max_intents", 0),
    ],
)
def test_fixture_rejects_invalid_contract_values(override, value):
    with pytest.raises(ValueError):
        _fixture(**{override: value})


def test_fixture_rejects_duplicate_or_empty_reference_identity():
    duplicate = (
        AdvisorReferenceObjective(objective_id="same", route_hash="route-a"),
        AdvisorReferenceObjective(objective_id="same", goal="goal-b"),
    )
    with pytest.raises(ValueError):
        _fixture(reference_objectives=duplicate)
    with pytest.raises(ValueError):
        _fixture(reference_objectives=(AdvisorReferenceObjective(
            objective_id="empty", route_hash="", goal="  "
        ),))


@pytest.mark.parametrize("value", ["line\nbreak", "bad\x00token", "x" * 129, "ümlaut"])
def test_source_identity_rejects_multiline_control_overlimit_and_non_ascii(value):
    with pytest.raises(ValueError):
        _fixture(source_intent_id=value)


def test_source_identity_normalizes_and_empty_attribution_is_allowed():
    fixture = _fixture(source_intent_id=" intent/one ", source_route_hash=" Route-One ")
    assert fixture.source_intent_id == "intent/one"
    assert fixture.source_route_hash == "route-one"
    unattributed = _fixture(source_intent_id="", source_route_hash="")
    trigger = flag_scout_trigger(unattributed)
    assert trigger.suggestion is not None
    assert trigger.suggestion.route_attribution == "unattributed"


def test_advisor_delta_does_not_copy_flag_from_opaque_summary():
    fixture = _fixture()
    trigger = flag_scout_trigger(fixture)
    assert trigger.suggestion is not None
    rendered = build_experimental_summary(fixture, trigger.suggestion)
    assert rendered.startswith(fixture.graph_summary)
    delta = rendered[len(fixture.graph_summary):]
    assert "flag{opaque_fixture_secret}" not in delta
    assert rendered.count("flag{opaque_fixture_secret}") == 1
    assert "obj-admin" not in delta


def test_flag_scout_trigger_exact_remaining_flag_rules():
    single = flag_scout_trigger(_fixture(expected_flags=1, captured_flags_before_source=0))
    assert (single.eligible, single.reason, single.suggestion) == (
        False, "single_flag_run", None
    )
    done = flag_scout_trigger(_fixture(expected_flags=2, captured_flags_before_source=1))
    assert (done.eligible, done.reason, done.suggestion) == (
        False, "no_remaining_flag_after_source", None
    )
    eligible = flag_scout_trigger(_fixture(expected_flags=4, captured_flags_before_source=1))
    assert eligible.eligible is True
    assert eligible.reason == "eligible"
    assert eligible.suggestion is not None
    assert eligible.suggestion.suggestion_id.startswith("m8-suggestion::")


def test_safe_reason_trace_uses_allowlist_and_never_calls_to_payload(monkeypatch):
    def forbidden(_self):
        raise AssertionError("to_payload must not be called")

    monkeypatch.setattr(Intent, "to_payload", forbidden)
    intent = _intent(goal="flag{planner-repeat} hidden-reference prompt-sentinel")
    trace = safe_reason_trace(
        _result(intent),
        available_fact_seqs=(10,),
    )
    payload = _trace_json(trace)
    for forbidden_text in (
        "flag{planner-repeat}", "hidden-reference", "prompt-sentinel",
        "rationale-secret", "audit-secret", "drift-secret", "complete-secret",
        "surface-secret", "reopen-secret", "dispatch-secret", "intent-admin",
        "route-admin", "Inspect the ADMIN branch",
    ):
        assert forbidden_text not in payload
    assert trace.audit_note_count == 1
    assert trace.dispatch_count == 1
    assert trace.pinned_facts == (10,)


def test_safe_trace_rejects_runner_known_forbidden_fragment_without_echo():
    with pytest.raises(AdvisorSensitiveOutput) as caught:
        safe_reason_trace(
            _result(_intent(goal="contains provider-canary")),
            available_fact_seqs=(10,),
            forbidden_fragments=("provider-canary",),
        )
    assert caught.value.code == "sensitive_output_redacted"
    assert "provider-canary" not in str(caught.value)


def test_goal_fingerprint_is_token_bag_stable_and_raw_free():
    first = safe_reason_trace(_result(_intent(goal="Inspect ADMIN branch")), available_fact_seqs=(10,))
    second = safe_reason_trace(_result(_intent(goal="branch inspect admin")), available_fact_seqs=(10,))
    assert first.intents[0].goal_fingerprint == second.intents[0].goal_fingerprint
    assert "admin" not in first.intents[0].goal_fingerprint


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("worker_class", "browser", "invalid_worker_class"),
        ("direction", "reversing", "unsafe_direction"),
        ("priority", math.nan, "invalid_priority"),
        ("from_facts", [True], "invalid_fact_reference"),
        ("from_facts", [0], "invalid_fact_reference"),
        ("from_facts", [-1], "invalid_fact_reference"),
        ("from_facts", [1.0], "invalid_fact_reference"),
        ("from_facts", ["1"], "invalid_fact_reference"),
    ],
)
def test_safe_trace_rejects_invalid_enum_numeric_and_fact_values(field, value, code):
    with pytest.raises(AdvisorSensitiveOutput) as caught:
        safe_reason_trace(_result(_intent(**{field: value})), available_fact_seqs=(10,))
    assert caught.value.code == code
    assert caught.value.field == field
    assert str(value) not in str(caught.value)


def test_safe_trace_retains_unavailable_positive_citation_and_dedupes():
    trace = safe_reason_trace(
        _result(_intent(from_facts=[99, 10, 99])), available_fact_seqs=(10,)
    )
    assert trace.intents[0].from_facts == (10, 99)
    comparison = compare_intent_traces(trace, trace, available_fact_seqs=(10,))
    assert comparison.baseline_unsupported_citation_count == 1
    assert comparison.advisor_unsupported_citation_count == 1


def test_intent_equivalence_and_comparison_are_deterministic():
    baseline = safe_reason_trace(_result(
        _intent(intent_id="a", goal="inspect admin branch", route_hash="route-admin"),
        _intent(intent_id="dup", goal="branch admin inspect", route_hash=""),
    ), available_fact_seqs=(10,))
    advisor = safe_reason_trace(_result(
        _intent(intent_id="b", goal="different", route_hash="route-admin"),
        _intent(intent_id="c", goal="new crypto path", route_hash="route-crypto", from_facts=[]),
    ), available_fact_seqs=(10,))
    assert intent_trace_equivalent(baseline.intents[0], advisor.intents[0])
    comparison = compare_intent_traces(baseline, advisor, available_fact_seqs=(10,))
    assert comparison.baseline_count == 2
    assert comparison.advisor_count == 2
    assert comparison.overlap_count == 1
    assert comparison.baseline_duplicate_count == 1
    assert comparison.advisor_duplicate_count == 0
    assert comparison.advisor_unsupported_citation_count == 1
    assert comparison.baseline_only_intent_indexes == (1,)
    assert comparison.advisor_only_intent_indexes == (1,)
    assert comparison.jaccard == pytest.approx(1 / 3)


def test_assess_suggestion_uses_frozen_precedence():
    assert assess_suggestion(
        baseline_success=False, advisor_success=True,
        advisor_intent_count=1, gained_count=1, lost_count=0,
        supported_gain=True, planning_delta=True,
    ).verdict == "indeterminate_planner_error"
    assert assess_suggestion(
        baseline_success=True, advisor_success=True,
        advisor_intent_count=0, gained_count=0, lost_count=0,
        supported_gain=False, planning_delta=True,
    ).verdict == "rejected_advisor_empty"
    assert assess_suggestion(
        baseline_success=True, advisor_success=True,
        advisor_intent_count=1, gained_count=1, lost_count=0,
        supported_gain=True, planning_delta=True,
    ).verdict == "accepted_reference_gain"


def test_module_does_not_import_production_scheduler_or_graph():
    source = Path("dswarm/swarm/advisor_experiment.py").read_text("utf-8")
    for forbidden in ("reason_scheduler", "shared_graph", "event_bus", "solver.gate"):
        assert forbidden not in source


_M8_OFFLINE_FILES = (
    Path("dswarm/swarm/advisor_experiment.py"),
    Path("dswarm/swarm/advisor_sidecar.py"),
    Path("dswarm/swarm/advisor_runner.py"),
    Path("dswarm/swarm/advisor_report.py"),
    Path("dswarm/swarm/advisor_benchmark.py"),
    Path("scripts/advisor_benchmark.py"),
)
_PRODUCTION_FILES = (
    Path("dswarm/swarm/reason_scheduler.py"),
    Path("dswarm/swarm/shared_graph.py"),
    Path("dswarm/solver/reason.py"),
    Path("dswarm/solver/gate.py"),
)
_FORBIDDEN_OFFLINE_IMPORT_PREFIXES = (
    "apps",
    "dswarm.core.event_bus",
    "dswarm.solver.gate",
    "dswarm.swarm.reason_scheduler",
    "dswarm.swarm.shared_graph",
)
_M8_MODULE_NAMES = {
    "advisor_experiment",
    "advisor_sidecar",
    "advisor_runner",
    "advisor_report",
    "advisor_benchmark",
}


def _python_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _import_names(path: Path) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(_python_tree(path)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
    return tuple(names)


def test_all_m8_modules_are_statically_isolated_from_production_substrate():
    for path in _M8_OFFLINE_FILES:
        imports = _import_names(path)
        for imported in imports:
            assert not any(
                imported == prefix or imported.startswith(prefix + ".")
                for prefix in _FORBIDDEN_OFFLINE_IMPORT_PREFIXES
            ), (path, imported)


def test_production_paths_do_not_import_m8_experiment_modules():
    for path in _PRODUCTION_FILES:
        imports = _import_names(path)
        for imported in imports:
            assert not any(
                imported == name or imported.endswith("." + name)
                for name in _M8_MODULE_NAMES
            ), (path, imported)


def test_trace_and_runner_modules_do_not_use_generic_planner_serialization():
    targets = (
        Path("dswarm/swarm/advisor_experiment.py"),
        Path("dswarm/swarm/advisor_sidecar.py"),
        Path("dswarm/swarm/advisor_runner.py"),
    )
    forbidden_calls = {"asdict", "model_dump", "vars", "to_payload"}
    for path in targets:
        for node in ast.walk(_python_tree(path)):
            if isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id if isinstance(function, ast.Name)
                    else function.attr if isinstance(function, ast.Attribute)
                    else ""
                )
                assert name not in forbidden_calls, (path, name)
            if isinstance(node, ast.Attribute):
                assert node.attr != "__dict__", path


def test_hidden_references_are_only_semantically_read_by_reporter():
    targets = (
        Path("dswarm/swarm/advisor_runner.py"),
        Path("dswarm/swarm/advisor_sidecar.py"),
        Path("dswarm/swarm/advisor_benchmark.py"),
        Path("scripts/advisor_benchmark.py"),
    )
    for path in targets:
        reads = [
            node for node in ast.walk(_python_tree(path))
            if isinstance(node, ast.Attribute)
            and node.attr == "reference_objectives"
            and isinstance(node.ctx, ast.Load)
        ]
        assert reads == [], path
