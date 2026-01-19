"""Tests for domain validation."""

import pytest

from nextdnsctl.api import validate_domain, InvalidDomainError


class TestDomainValidation:
    """Tests for domain format validation."""

    def test_valid_domain(self):
        """Should accept valid domain names."""
        valid_domains = [
            "example.com",
            "sub.example.com",
            "deep.sub.example.com",
            "example.co.uk",
            "test-domain.com",
            "123.example.com",
            "example123.com",
            "a.io",
        ]
        for domain in valid_domains:
            result = validate_domain(domain)
            assert result == domain.lower()

    def test_normalizes_to_lowercase(self):
        """Should normalize domains to lowercase."""
        assert validate_domain("EXAMPLE.COM") == "example.com"
        assert validate_domain("Example.Com") == "example.com"
        assert validate_domain("SUB.DOMAIN.COM") == "sub.domain.com"

    def test_strips_whitespace(self):
        """Should strip leading/trailing whitespace."""
        assert validate_domain("  example.com  ") == "example.com"
        assert validate_domain("\texample.com\n") == "example.com"

    def test_rejects_empty_domain(self):
        """Should reject empty domains."""
        with pytest.raises(InvalidDomainError, match="cannot be empty"):
            validate_domain("")
        with pytest.raises(InvalidDomainError, match="cannot be empty"):
            validate_domain("   ")

    def test_rejects_domain_without_tld(self):
        """Should reject domains without a TLD."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("localhost")
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("myserver")

    def test_rejects_domain_with_spaces(self):
        """Should reject domains with internal spaces."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("example .com")
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("my domain.com")

    def test_rejects_domain_starting_with_hyphen(self):
        """Should reject domains starting with hyphen."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("-example.com")

    def test_rejects_domain_ending_with_hyphen(self):
        """Should reject domains ending with hyphen in a label."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("example-.com")

    def test_rejects_domain_with_invalid_characters(self):
        """Should reject domains with invalid characters."""
        invalid = [
            "example@.com",
            "example!.com",
            "example#.com",
            "example$.com",
            "example%.com",
            "example&.com",
            "example*.com",
            "example,.com",
        ]
        for domain in invalid:
            with pytest.raises(InvalidDomainError, match="Invalid domain format"):
                validate_domain(domain)

    def test_rejects_domain_too_long(self):
        """Should reject domains longer than 253 characters."""
        long_domain = "a" * 250 + ".com"  # 254 chars total
        with pytest.raises(InvalidDomainError, match="too long"):
            validate_domain(long_domain)

    def test_rejects_single_char_tld(self):
        """Should reject domains with single character TLD."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("example.c")

    def test_accepts_two_char_tld(self):
        """Should accept domains with two character TLD."""
        assert validate_domain("example.io") == "example.io"
        assert validate_domain("example.uk") == "example.uk"

    def test_accepts_long_tld(self):
        """Should accept domains with long TLDs."""
        assert validate_domain("example.technology") == "example.technology"
        assert validate_domain("example.museum") == "example.museum"

    def test_rejects_numeric_only_tld(self):
        """Should reject domains with numeric-only TLD."""
        with pytest.raises(InvalidDomainError, match="Invalid domain format"):
            validate_domain("example.123")

    def test_accepts_hyphen_in_middle(self):
        """Should accept hyphens in the middle of labels."""
        assert validate_domain("my-example.com") == "my-example.com"
        assert validate_domain("test-domain-name.co.uk") == "test-domain-name.co.uk"

    def test_strips_http_protocol(self):
        """Should strip http:// protocol prefix."""
        assert validate_domain("http://example.com") == "example.com"
        assert validate_domain("http://sub.example.com") == "sub.example.com"

    def test_strips_https_protocol(self):
        """Should strip https:// protocol prefix."""
        assert validate_domain("https://example.com") == "example.com"
        assert validate_domain("https://sub.example.com") == "sub.example.com"

    def test_strips_other_protocols(self):
        """Should strip other protocol prefixes."""
        assert validate_domain("ftp://example.com") == "example.com"
        assert validate_domain("sftp://example.com") == "example.com"

    def test_strips_path(self):
        """Should strip path from URL."""
        assert validate_domain("example.com/path/to/page") == "example.com"
        assert validate_domain("https://example.com/path") == "example.com"
        assert validate_domain("http://example.com/") == "example.com"

    def test_strips_port(self):
        """Should strip port number from domain."""
        assert validate_domain("example.com:8080") == "example.com"
        assert validate_domain("https://example.com:443") == "example.com"
        assert validate_domain("http://example.com:8080/path") == "example.com"

    def test_strips_protocol_path_and_port(self):
        """Should handle full URLs with protocol, port, and path."""
        assert validate_domain("https://example.com:443/path/to/page") == "example.com"
        assert validate_domain("http://sub.example.com:8080/api/v1") == "sub.example.com"

    def test_rejects_protocol_only(self):
        """Should reject URLs that are just protocols."""
        with pytest.raises(InvalidDomainError, match="cannot be empty"):
            validate_domain("http://")
        with pytest.raises(InvalidDomainError, match="cannot be empty"):
            validate_domain("https://")
