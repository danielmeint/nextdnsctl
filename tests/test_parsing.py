"""Unit tests for domain parsing logic."""

from nextdnsctl.nextdnsctl import _parse_domain_line


class TestParseDomainLine:
    """Tests for _parse_domain_line function."""

    def test_simple_domain(self):
        assert _parse_domain_line("example.com") == "example.com"

    def test_domain_with_inline_comment(self):
        assert _parse_domain_line("example.com # comment") == "example.com"

    def test_domain_with_inline_comment_no_space(self):
        assert _parse_domain_line("example.com#comment") == "example.com"

    def test_pure_comment(self):
        assert _parse_domain_line("# pure comment") is None

    def test_comment_with_leading_spaces(self):
        assert _parse_domain_line("   # comment") is None

    def test_empty_line(self):
        assert _parse_domain_line("") is None

    def test_whitespace_only(self):
        assert _parse_domain_line("   ") is None

    def test_domain_with_trailing_whitespace(self):
        assert _parse_domain_line("example.com   ") == "example.com"

    def test_domain_with_leading_whitespace(self):
        assert _parse_domain_line("   example.com") == "example.com"

    def test_subdomain(self):
        assert _parse_domain_line("sub.example.com") == "sub.example.com"

    def test_complex_inline_comment(self):
        # Only the first # should trigger comment stripping
        assert _parse_domain_line("domain.com # bad site # really bad") == "domain.com"
