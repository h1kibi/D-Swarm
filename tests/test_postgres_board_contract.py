from inspect import signature

from dswarm.swarm.board import Finding, ReplacementOutcome
from dswarm.swarm.postgres_board import PostgresBoard


def test_postgres_board_exposes_projection_contract():
    assert "projection_key" in signature(PostgresBoard.write_finding).parameters
    assert hasattr(PostgresBoard, "replace_by_source")


def test_postgres_row_mapping_preserves_projection_identity():
    row = (
        7, "c1", "pi-web", "HTTP_ENDPOINT", "/admin", {"verified": True},
        0.8, 1800, 42, "fact:42:promotion:99", None,
        __import__("datetime").datetime(2026, 8, 14, tzinfo=__import__("datetime").timezone.utc),
    )
    finding = PostgresBoard._finding_from_row(row)
    assert finding.source_seq == 42
    assert finding.projection_key == "fact:42:promotion:99"
    assert finding.finding_id == "fact:42:promotion:99"


def test_postgres_replacement_result_types_are_public_contract():
    finding = Finding(challenge_id="c1", kind="TEXT_FACT", source_seq=1)
    # This test pins the typed outcomes used by both MemoryBoard and PostgresBoard.
    assert ReplacementOutcome.REPLACED.value == "replaced"
    assert finding.projection_key == ""
