from inspect import signature

from dswarm.swarm.board import Finding, ReplacementOutcome
from dswarm.swarm.postgres_board import PostgresBoard


def test_postgres_board_exposes_projection_contract():
    assert "projection_key" in signature(PostgresBoard.write_finding).parameters
    assert hasattr(PostgresBoard, "replace_by_source")


def test_postgres_row_mapping_preserves_projection_identity():
    timestamp = __import__("datetime").datetime(
        2026, 8, 14, tzinfo=__import__("datetime").timezone.utc
    )
    row = (
        7, "c1", "pi-web", "HTTP_ENDPOINT", "/admin", {"verified": True},
        0.8, 1800, 42, "fact:42:promotion:99", None, timestamp,
        "route-web", "inherited", timestamp, timestamp, timestamp, timestamp,
    )
    finding = PostgresBoard._finding_from_row(row)
    assert finding.source_seq == 42
    assert finding.projection_key == "fact:42:promotion:99"
    assert finding.finding_id == "fact:42:promotion:99"
    assert finding.route_hash == "route-web"
    assert finding.route_lineage == "inherited"
    assert finding.event_ts == timestamp.timestamp()
    assert finding.projected_at == timestamp.timestamp()
    assert finding.pheromone_origin_ts == timestamp.timestamp()
    assert finding.fact_origin_ts == timestamp.timestamp()


def test_postgres_replacement_result_types_are_public_contract():
    finding = Finding(challenge_id="c1", kind="TEXT_FACT", source_seq=1)
    # This test pins the typed outcomes used by both MemoryBoard and PostgresBoard.
    assert ReplacementOutcome.REPLACED.value == "replaced"
    assert finding.projection_key == ""


def test_postgres_projection_contract_includes_all_m6_columns():
    columns = PostgresBoard._finding_columns()
    for name in (
        "route_hash",
        "route_lineage",
        "event_ts",
        "projected_at",
        "pheromone_origin_ts",
        "fact_origin_ts",
    ):
        assert name in columns
        assert name in signature(PostgresBoard.write_finding).parameters
