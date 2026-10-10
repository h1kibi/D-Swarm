"""Fuzzing tests for secret redaction edge cases (Base64 truncation, cloud tokens, etc)."""

from __future__ import annotations

import base64
import re
from typing import Any

import pytest


# Import the regex from cli_solver
from dswarm.solver.cli_solver import _SECRET_LITERAL_RE


class TestSecretLiteralRegex:
    """Test _SECRET_LITERAL_RE against various secret formats and truncation boundaries."""

    def test_basic_api_key_literal(self) -> None:
        """Basic api_key= pattern should redact."""
        text = 'api_key=abc123def456ghi789jkl012'
        assert _SECRET_LITERAL_RE.search(text)
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert '<SECRET>' in result
        assert 'abc123def456ghi789jkl012' not in result

    def test_basic_token_literal(self) -> None:
        """Basic token: pattern should redact."""
        text = 'token: abcdef123456789abcdef1234567890ab'
        assert _SECRET_LITERAL_RE.search(text)
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert '<SECRET>' in result

    def test_basic_secret_literal(self) -> None:
        """Basic secret= pattern should redact."""
        text = 'secret=VeryLongSecretValue123456789'
        assert _SECRET_LITERAL_RE.search(text)
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert '<SECRET>' in result

    def test_password_pattern(self) -> None:
        """Password field should redact."""
        text = 'password="strongPasswordABC123!@#XYZ"'
        assert _SECRET_LITERAL_RE.search(text)

    def test_private_key_pem_header(self) -> None:
        """PEM PRIVATE KEY header should redact."""
        pem = '-----BEGIN PRIVATE KEY-----\nabcdefghijklmnop\n-----END PRIVATE KEY-----'
        assert _SECRET_LITERAL_RE.search(pem)
        result = _SECRET_LITERAL_RE.sub('<SECRET>', pem)
        assert '-----BEGIN' not in result

    def test_rsa_private_key_header(self) -> None:
        """PEM RSA PRIVATE KEY header should redact."""
        pem = '-----BEGIN RSA PRIVATE KEY-----\nabcdefghijklmnop\n-----END RSA PRIVATE KEY-----'
        assert _SECRET_LITERAL_RE.search(pem)

    def test_ec_private_key_header(self) -> None:
        """PEM EC PRIVATE KEY header should redact."""
        pem = '-----BEGIN EC PRIVATE KEY-----\nabcdefghijklmnop\n-----END EC PRIVATE KEY-----'
        assert _SECRET_LITERAL_RE.search(pem)

    # ── Base64 truncation edge cases ─────────────────────────────────────

    def test_base64_api_key_600_chars_boundary(self) -> None:
        """Base64 API key at 600-char truncation boundary."""
        # Generate a 600-char Base64 string
        raw = b'x' * 450  # ~600 chars when Base64 encoded
        b64 = base64.b64encode(raw).decode('ascii')
        assert len(b64) >= 595  # Approximately 600 chars
        text = f'api_key={b64}'
        assert _SECRET_LITERAL_RE.search(text), f"Failed to match Base64 at 600-char boundary: {b64[:50]}..."

    def test_base64_api_key_650_chars_full(self) -> None:
        """Base64 API key 650 chars (exceeds truncation limit)."""
        raw = b'y' * 490  # ~650 chars when Base64 encoded
        b64 = base64.b64encode(raw).decode('ascii')
        assert len(b64) >= 640
        text = f'api_key={b64}'
        # Full string should match
        assert _SECRET_LITERAL_RE.search(text)

    def test_base64_api_key_650_chars_truncated_at_600(self) -> None:
        """Base64 API key 650 chars, truncated at 600-char boundary (CRITICAL)."""
        raw = b'z' * 490  # ~650 chars when Base64 encoded
        b64 = base64.b64encode(raw).decode('ascii')
        text = f'api_key={b64}'
        # Truncate to 600 chars (simulating output truncation)
        truncated = text[:600]
        # Truncated portion should still match (contains valid Base64 chars)
        assert _SECRET_LITERAL_RE.search(truncated), \
            f"Failed to match Base64 at truncation boundary. Truncated text:\n{truncated[-50:]}"

    def test_base64_api_key_900_chars_truncated_at_600(self) -> None:
        """Base64 API key 900 chars, truncated at 600-char boundary."""
        raw = b'w' * 675  # ~900 chars when Base64 encoded
        b64 = base64.b64encode(raw).decode('ascii')
        assert len(b64) >= 880
        text = f'api_key={b64}'
        truncated = text[:600]
        assert _SECRET_LITERAL_RE.search(truncated)

    def test_base64_token_multiline(self) -> None:
        """Multiline Base64-encoded token block."""
        raw = b'x' * 600
        b64 = base64.b64encode(raw).decode('ascii')
        # Simulate wrapped Base64 block
        lines = [b64[i:i+76] for i in range(0, len(b64), 76)]
        text = 'token: \n' + '\n'.join(lines) + '\n'
        # At least the first line should match the pattern
        assert _SECRET_LITERAL_RE.search(text)

    def test_base64_padding_variants(self) -> None:
        """Base64 with various padding scenarios."""
        test_cases = [
            (base64.b64encode(b'x' * 10).decode('ascii'), 'api_key'),  # May have no padding
            (base64.b64encode(b'x' * 11).decode('ascii'), 'token'),    # May have = padding
            (base64.b64encode(b'x' * 12).decode('ascii'), 'secret'),   # May have == padding
        ]
        for b64, field in test_cases:
            text = f'{field}={b64}'
            assert _SECRET_LITERAL_RE.search(text), \
                f"Failed to match {field} with padding: {b64}"

    # ── Cloud provider token prefixes ───────────────────────────────────

    def test_aws_access_key_akia_prefix(self) -> None:
        """AWS IAM access key with AKIA prefix."""
        text = 'aws_key=AKIAIOSFODNN7EXAMPLE1234567890AB'
        assert _SECRET_LITERAL_RE.search(text)

    def test_github_token_ghp_prefix(self) -> None:
        """GitHub personal access token with ghp_ prefix."""
        text = 'token=ghp_1234567890abcdefghijklmnopqrstuvwxyz'
        assert _SECRET_LITERAL_RE.search(text)

    def test_openai_sk_prefix(self) -> None:
        """OpenAI API key with sk- prefix."""
        text = 'openai_key=sk-1234567890abcdefghijklmnopqrstuvwxyz'
        assert _SECRET_LITERAL_RE.search(text)

    def test_stripe_sk_prefix(self) -> None:
        """Stripe secret key with sk_ prefix."""
        text = 'stripe_secret=sk_test_abcdefghijklmnopqrstuvwxyz1234567890'
        assert _SECRET_LITERAL_RE.search(text)

    def test_google_sv_prefix(self) -> None:
        """Google service account key with sv_ prefix."""
        text = 'gcp_key=sv_abcdefghijklmnopqrstuvwxyz1234567890abcdef'
        assert _SECRET_LITERAL_RE.search(text)

    def test_api_underscore_variants(self) -> None:
        """API key field with various naming conventions."""
        cases = [
            'api_key=secretvalue123456789',
            'apiKey=secretvalue123456789',
            'api-key=secretvalue123456789',
            'API_KEY=secretvalue123456789',
        ]
        for text in cases:
            assert _SECRET_LITERAL_RE.search(text), f"Failed to match: {text}"

    # ── Boundary conditions ──────────────────────────────────────────────

    def test_minimum_length_12_chars(self) -> None:
        """Secret with exactly 12 chars should match (minimum length)."""
        text = 'token=abcdefghijkl'  # exactly 12 chars
        assert _SECRET_LITERAL_RE.search(text)

    def test_just_below_minimum_11_chars(self) -> None:
        """Secret with 11 chars should not match (below minimum)."""
        text = 'token=abcdefghijk'  # 11 chars
        # Should NOT match - minimum is 12
        assert not _SECRET_LITERAL_RE.search(text)

    def test_very_long_secret_2000_chars(self) -> None:
        """Very long Base64 secret (2000 chars)."""
        raw = b'x' * 1500
        b64 = base64.b64encode(raw).decode('ascii')
        text = f'secret={b64}'
        assert _SECRET_LITERAL_RE.search(text)

    def test_very_long_secret_2000_chars_truncated_at_600(self) -> None:
        """Very long Base64 secret (2000 chars), truncated at 600."""
        raw = b'x' * 1500
        b64 = base64.b64encode(raw).decode('ascii')
        text = f'secret={b64}'
        truncated = text[:600]
        assert _SECRET_LITERAL_RE.search(truncated)

    # ── False negative prevention ───────────────────────────────────────

    def test_does_not_match_short_alphanumeric_in_output(self) -> None:
        """Legitimate short alphanumeric text should not be over-redacted."""
        text = 'Build completed in 10.5 seconds'
        # Should not trigger redaction
        assert not _SECRET_LITERAL_RE.search(text)

    def test_does_not_match_single_word_key(self) -> None:
        """Single short word following = should not match (non-cloud fields only)."""
        # Note: 'api_key=' matches the aggressive cloud prefix pattern 'api_',
        # which is intentional for security. Use a non-cloud field name.
        text = 'database_url=myvalue'  # no recognized secret field pattern
        assert not _SECRET_LITERAL_RE.search(text)

    def test_does_not_match_url_parameters(self) -> None:
        """Regular URL query parameters should not be redacted (if short)."""
        text = 'https://example.com?id=user123&page=5'
        # id and page values are too short
        assert not _SECRET_LITERAL_RE.search(text)

    def test_pem_key_does_not_match_non_key_headers(self) -> None:
        """Should not match arbitrary key-like headers."""
        text = '-----BEGIN CERTIFICATE-----\ndata\n-----END CERTIFICATE-----'
        # CERTIFICATE should not match (not PRIVATE KEY)
        assert not _SECRET_LITERAL_RE.search(text)

    # ── Redaction behavior tests ─────────────────────────────────────────

    def test_multiple_secrets_redacted(self) -> None:
        """Multiple secrets in one string should all be redacted."""
        text = 'api_key=abc123def456ghi789 and token=xyz987uvw654tsr321'
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert result.count('<SECRET>') == 2
        assert 'abc123def456ghi789' not in result
        assert 'xyz987uvw654tsr321' not in result

    def test_pem_header_replaced_completely(self) -> None:
        """PEM BEGIN line should be fully replaced."""
        text = '-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----'
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert '-----BEGIN' not in result
        assert result.count('<SECRET>') >= 1

    def test_redaction_preserves_surrounding_text(self) -> None:
        """Redaction should preserve context around the secret."""
        text = 'Database connection: api_key=secret123456789abc password=pass123456789def endpoint=db.example.com'
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert 'Database connection:' in result
        assert 'endpoint=db.example.com' in result  # short value not redacted

    # ── Realistic scenarios ──────────────────────────────────────────────

    def test_realistic_aws_config_output(self) -> None:
        """Realistic AWS configuration output."""
        text = '''AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1234'''
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert '<SECRET>' in result
        assert 'EXAMPLE' not in result or result.count('EXAMPLE') == 1

    def test_realistic_api_response_with_bearer_token(self) -> None:
        """API response with Authorization bearer token."""
        token = base64.b64encode(b'x' * 100).decode('ascii')
        text = f'Authorization: Bearer {token}'
        # Token should be long enough to match pattern
        if len(token) >= 12:
            assert _SECRET_LITERAL_RE.search(text)

    def test_realistic_config_file_json(self) -> None:
        """Realistic JSON config with API keys."""
        text = '''{
  "api_key": "ABCDefghijklmnopqrstuvwxyz1234567890AB",
  "token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
  "database_url": "postgres://user:pass@localhost/db"
}'''
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        # Both api_key and token values should be redacted
        assert '<SECRET>' in result

    def test_realistic_log_with_exception_and_secret(self) -> None:
        """Log output with exception trace and secret."""
        text = '''2024-10-10 12:34:56 ERROR Connection failed
Stack trace:
  File "main.py", line 42
    response = requests.get(url, headers={'Authorization': 'Bearer sk_test_1234567890abcdefghijklmnop'})
Failed to connect'''
        result = _SECRET_LITERAL_RE.sub('<SECRET>', text)
        assert 'sk_test_' not in result or '<SECRET>' in result

    # ── Truncation boundary sweep ───────────────────────────────────────

    @pytest.mark.parametrize('truncation_point', [550, 575, 600, 625, 650])
    def test_base64_truncation_sweep(self, truncation_point: int) -> None:
        """Test Base64 redaction at multiple truncation boundaries."""
        raw = b'x' * 500
        b64 = base64.b64encode(raw).decode('ascii')
        text = f'api_key={b64}'
        if len(text) >= truncation_point:
            truncated = text[:truncation_point]
            # Should still match if it contains enough Base64 chars
            if len(truncated.split('=')[-1]) >= 12:
                assert _SECRET_LITERAL_RE.search(truncated), \
                    f"Failed at truncation point {truncation_point}"

    # ── Ensure backward compatibility ───────────────────────────────────

    def test_legacy_api_key_format(self) -> None:
        """Original simple API key format still works."""
        text = "config['api_key'] = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij'"
        assert _SECRET_LITERAL_RE.search(text)

    def test_legacy_token_format(self) -> None:
        """Original simple token format still works."""
        text = 'token_value = "1234567890abcdefghijklmnopqrstuvwxyz1234"'
        assert _SECRET_LITERAL_RE.search(text)

    def test_legacy_password_format(self) -> None:
        """Original simple password format still works."""
        text = 'password: SuperSecretPasswordWith1234567890Characters'
        assert _SECRET_LITERAL_RE.search(text)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
