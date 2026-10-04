"""Domain normalisation and strict source parsing."""

import pytest

from nextdnsctl.domains import InvalidDomainError, domain_from_argument, normalize_domain
from nextdnsctl.sources import SourceError, detect_format, parse


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("example.com", "example.com"),
            ("Example.COM", "example.com"),
            ("  example.com\n", "example.com"),
            ("example.com.", "example.com"),
            ("münchen.de", "xn--mnchen-3ya.de"),
            ("under_score.com", "under_score.com"),
            ("_dmarc.example.com", "_dmarc.example.com"),
            ("xn--80ak6aa92e.xn--p1ai", "xn--80ak6aa92e.xn--p1ai"),
            ("foo.lan", "foo.lan"),  # A16: no TLD list
        ],
    )
    def test_valid(self, value, expected):
        assert normalize_domain(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "localhost",
            "*.example.com",
            "1.2.3.4",
            "a..b.com",
            "-a.com",
            "a-.com",
            "a.-b.com",
            "example.com..",
            "a.com/path",
            "a.com:443",
            "space .com",
            "example.c",
            "example.123",
            "a" * 250 + ".com",
        ],
    )
    def test_invalid(self, value):
        with pytest.raises(InvalidDomainError):
            normalize_domain(value)

    def test_url_with_scheme_is_reduced_to_host(self):
        assert domain_from_argument("https://Example.com:8443/path?q=1#x") == "example.com"
        assert domain_from_argument("http://user@example.com/") == "example.com"

    def test_bare_path_is_rejected(self):
        with pytest.raises(InvalidDomainError):
            domain_from_argument("example.com/path")


class TestDetectFormat:
    def test_plain(self):
        assert detect_format(["# comment", "a.com", "b.com # why"]) == "plain"

    def test_hosts(self):
        assert detect_format(["127.0.0.1 localhost", "0.0.0.0 a.com"]) == "hosts"

    def test_adblock(self):
        assert detect_format(["! Title", "[Adblock Plus 2.0]", "||a.com^"]) == "adblock"

    def test_empty_is_plain(self):
        assert detect_format(["# nothing", ""]) == "plain"

    def test_mixed_is_an_error_naming_both_lines(self):
        with pytest.raises(SourceError, match=r"hosts \(first at line 1\), adblock \(first at line 3\)"):
            detect_format(["0.0.0.0 a.com", "# x", "||b.com^"], "list.txt")


class TestPlain:
    def test_comments_and_urls(self):
        result = parse(["# header", "a.com", "B.com # inline", "", "https://c.com/x"], "plain")
        assert [d for d, _ in result.domains] == ["a.com", "b.com", "c.com"]
        assert [line for _, line in result.domains] == [2, 3, 5]
        assert not result.errors

    def test_two_tokens_suggest_hosts(self):
        result = parse(["0.0.0.0 a.com"], "plain")
        assert "hosts" in result.errors[0].reason


class TestHosts:
    def test_sinkhole_lines(self):
        result = parse(["0.0.0.0 a.com b.com", "127.0.0.1 c.com", ":: d.com", "::1 e.com"], "hosts")
        assert [d for d, _ in result.domains] == ["a.com", "b.com", "c.com", "d.com", "e.com"]

    def test_boilerplate_is_skipped(self):
        lines = [
            "127.0.0.1 localhost",
            "255.255.255.255 broadcasthost",
            "::1 localhost ip6-localhost",
            "0.0.0.0 0.0.0.0",
        ]
        result = parse(lines, "hosts")
        assert not result.domains and not result.errors
        assert result.boilerplate_skipped == 4

    def test_real_address_is_an_error(self):
        result = parse(["192.168.1.10 nas.lan"], "hosts")
        assert "rewrite" in result.errors[0].reason
        assert result.errors[0].line == 1

    def test_invalid_host(self):
        result = parse(["0.0.0.0 bad..com"], "hosts")
        assert result.errors and not result.domains


class TestAdblock:
    def test_domain_rules(self):
        result = parse(["! comment", "||a.com^", "||Sub.B.com^"], "adblock")
        assert [d for d, _ in result.domains] == ["a.com", "sub.b.com"]

    @pytest.mark.parametrize(
        "line, reason",
        [
            ("@@||a.com^", "exception"),
            ("||a.com^$third-party", "modifiers"),
            ("||ads*.a.com^", "wildcard"),
            ("||a.com/ads^", "path"),
            ("a.com##.banner", "cosmetic"),
            ("##.banner", "cosmetic"),
            ("/banner/", "path"),
            ("a.com", "not a ||domain^ rule"),
        ],
    )
    def test_everything_else_is_an_error(self, line, reason):
        result = parse([line], "adblock")
        assert not result.domains
        assert reason in result.errors[0].reason


def test_auto_parses_with_detected_format():
    result = parse(["0.0.0.0 a.com", "0.0.0.0 b.com"])
    assert result.format == "hosts"
    assert [d for d, _ in result.domains] == ["a.com", "b.com"]
