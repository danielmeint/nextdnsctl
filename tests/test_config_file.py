"""Loading and validating nextdns.yaml."""

import textwrap

import pytest

from nextdnsctl.config import ConfigError, parse, parse_duration


def load(text: str, file: str = "/work/nextdns.yaml"):
    return parse(textwrap.dedent(text), file)


class TestStructure:
    def test_minimal(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                denylist: [bad.com]
            """
        )
        assert [p.key for p in config.profiles] == ["home"]
        assert config.profiles[0].lists["denylist"].entries[0][:2] == ("bad.com", True)

    def test_version_is_required(self):
        with pytest.raises(ConfigError, match="'version'"):
            load("profiles: {home: {}}")

    def test_unknown_top_level_key_with_line(self):
        with pytest.raises(ConfigError) as info:
            load(
                """
                version: 1
                profile:
                  home: {}
                """
            )
        assert info.value.line == 3
        assert "unknown key 'profile'" in info.value.message

    def test_unknown_section_key(self):
        with pytest.raises(ConfigError, match="unknown key 'denylists'"):
            load(
                """
                version: 1
                profiles:
                  home:
                    denylists: [a.com]
                """
            )

    def test_duplicate_key(self):
        with pytest.raises(ConfigError, match="duplicate key 'home'"):
            load(
                """
                version: 1
                profiles:
                  home: {}
                  home: {}
                """
            )

    def test_api_key_is_rejected(self):
        with pytest.raises(ConfigError, match="API key must not be stored"):
            load(
                """
                version: 1
                profiles:
                  home:
                    api_key: abc
                """
            )

    def test_invalid_yaml_has_a_line(self):
        with pytest.raises(ConfigError) as info:
            load("version: 1\nprofiles:\n  home: [unclosed\n")
        assert info.value.line is not None

    def test_anchors_share_sections(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                allowlist: &shared [a.com, b.com]
              kids:
                allowlist: *shared
            """
        )
        assert [d for d, _, _ in config.profiles[1].lists["allowlist"].entries] == ["a.com", "b.com"]


class TestDomainLists:
    def test_entries_and_sources(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                denylist:
                  domains:
                    - BAD.com
                    - {domain: tracker.example, active: false}
                  sources:
                    - https://example.com/hosts.txt
                    - {file: lists/extra.txt, format: adblock, skip_invalid: true}
            """
        )
        spec = config.profiles[0].lists["denylist"]
        assert [(d, a) for d, a, _ in spec.entries] == [("bad.com", True), ("tracker.example", False)]
        assert spec.sources[0].location == "https://example.com/hosts.txt"
        assert spec.sources[1].location == "/work/lists/extra.txt"
        assert spec.sources[1].format == "adblock" and spec.sources[1].skip_invalid

    def test_invalid_domain_points_at_its_line(self):
        with pytest.raises(ConfigError) as info:
            load(
                """
                version: 1
                profiles:
                  home:
                    denylist:
                      - good.com
                      - bad..com
                """
            )
        assert info.value.line == 7

    def test_source_needs_url_or_file(self):
        with pytest.raises(ConfigError, match="exactly one of"):
            load(
                """
                version: 1
                profiles:
                  home:
                    denylist:
                      sources:
                        - {format: hosts}
                """
            )


class TestSettings:
    def test_known_values_are_type_checked(self):
        with pytest.raises(ConfigError, match="must be true or false"):
            load(
                """
                version: 1
                profiles:
                  home:
                    security: {nrd: "yes please"}
                """
            )

    def test_unknown_values_are_kept_for_checking_against_the_live_profile(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                security: {brandNewFeature: true}
            """
        )
        assert config.profiles[0].unchecked == [("security", "brandNewFeature")]

    def test_retention_duration(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                settings: {logs: {retention: 30d}}
            """
        )
        assert config.profiles[0].overlay["settings"]["logs"]["retention"] == 30 * 86400

    def test_catalog_entries(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                privacy: {blocklists: [oisd]}
                parentalControl: {services: [tiktok, {id: instagram, recreation: true}]}
            """
        )
        overlay = config.profiles[0].overlay
        assert overlay["privacy"]["blocklists"] == [{"id": "oisd"}]
        assert overlay["parentalControl"]["services"] == [{"id": "tiktok"}, {"id": "instagram", "recreation": True}]

    def test_catalog_duplicates(self):
        with pytest.raises(ConfigError, match="listed twice"):
            load(
                """
                version: 1
                profiles:
                  home:
                    privacy: {blocklists: [oisd, oisd]}
                """
            )


class TestRewrites:
    def test_valid(self):
        config = load(
            """
            version: 1
            profiles:
              home:
                rewrites:
                  - {name: NAS.lan, content: 192.168.1.10}
                  - {name: nas, content: "fd00::10"}
                  - {name: alias.lan, content: nas.lan}
            """
        )
        assert config.profiles[0].overlay["rewrites"] == [
            {"name": "nas.lan", "content": "192.168.1.10"},
            {"name": "nas", "content": "fd00::10"},
            {"name": "alias.lan", "content": "nas.lan"},
        ]

    def test_invalid_content(self):
        with pytest.raises(ConfigError, match="IP address or a domain"):
            load(
                """
                version: 1
                profiles:
                  home:
                    rewrites: [{name: x.lan, content: "not an ip"}]
                """
            )


@pytest.mark.parametrize("value, seconds", [(3600, 3600), ("1h", 3600), ("30d", 2592000), ("1y", 31536000)])
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds
