"""Tests for dswarm.swarm.scope_audit — scope violation detection."""

from dswarm.swarm.scope_audit import (
    parse_scope,
    _extract_host_tokens,
    _is_private_or_link_local,
    scope_violations,
    format_violation_finding,
)


def test_parse_scope_empty():
    assert parse_scope("") == []
    assert parse_scope(None) == []
    assert parse_scope("   ") == []


def test_parse_scope_single_host():
    result = parse_scope("example.com")
    assert "example.com" in result


def test_parse_scope_multiple_delimiters():
    result = parse_scope("example.com, target.net; 10.0.0.1\nadmin.example.org")
    assert "example.com" in result
    assert "target.net" in result
    assert "10.0.0.1" in result
    assert "admin.example.org" in result


def test_parse_scope_drops_invalid():
    """An entry that has no hostname should be silently dropped."""
    result = parse_scope("example.com, ,,invalid__,, target.net")
    assert "example.com" in result
    assert "target.net" in result
    # Entries that fail _clean_lane_host are dropped.
    assert len(result) >= 2


def test_parse_scope_url_normalisation():
    """URL entries should be normalised to their hostname."""
    result = parse_scope("https://example.com/path?q=1")
    assert "example.com" in result


def test_is_private_positive():
    assert _is_private_or_link_local("10.0.0.1")
    assert _is_private_or_link_local("192.168.1.1")
    assert _is_private_or_link_local("172.16.0.1")
    assert _is_private_or_link_local("127.0.0.1")
    assert _is_private_or_link_local("::1")
    assert _is_private_or_link_local("fe80::1")


def test_is_private_negative():
    assert not _is_private_or_link_local("8.8.8.8")
    assert not _is_private_or_link_local("example.com")
    assert not _is_private_or_link_local("203.0.113.42")


def test_extract_host_tokens_finds_urls():
    text = "Found admin panel at https://admin.target.com/login"
    hosts = _extract_host_tokens(text)
    assert "admin.target.com" in hosts


def test_extract_host_tokens_finds_ips():
    text = "Server: 203.0.113.5:8080"
    hosts = _extract_host_tokens(text)
    assert "203.0.113.5" in hosts


def test_extract_host_tokens_skips_private():
    text = "Internal: 10.0.0.5, external: 1.2.3.4"
    hosts = _extract_host_tokens(text)
    assert "10.0.0.5" in hosts  # extracted, but marked private later
    assert "1.2.3.4" in hosts


def test_scope_violations_no_scope_flags_all():
    """No scope defined = every non-private host is a violation."""
    corpus = "Found target at https://evil.com and https://malicious.net"
    violations = scope_violations("", corpus)
    hosts = {v["host"] for v in violations}
    assert "evil.com" in hosts
    assert "malicious.net" in hosts


def test_scope_violations_whitelist_pass():
    corpus = "Connecting to https://example.com"
    violations = scope_violations("example.com", corpus)
    assert len(violations) == 0


def test_scope_violations_out_of_scope():
    corpus = "Found a shell at https://unauthorized.com"
    violations = scope_violations("example.com, target.net", corpus)
    assert len(violations) == 1
    assert violations[0]["host"] == "unauthorized.com"


def test_scope_violations_private_ips_always_in_scope():
    corpus = "Internal: 10.0.0.5, external: 1.2.3.4"
    violations = scope_violations("", corpus)
    hosts = {v["host"] for v in violations}
    assert "10.0.0.5" not in hosts  # private, always in-scope
    assert "1.2.3.4" in hosts


def test_scope_violations_dedup():
    corpus = "evil.com and evil.com again"
    violations = scope_violations("good.com", corpus)
    assert len(violations) == 1


def test_format_violation_finding():
    violation = {"host": "evil.com", "context": "Found shell at evil.com:8080"}
    finding = format_violation_finding(violation)
    assert finding["kind"] == "scope_violation"
    assert finding["severity"] == "high"
    assert "evil.com" in finding["summary"]
    assert "exclude_from_report" in finding["recommended_actions"]
    assert "notify_operator" in finding["recommended_actions"]
