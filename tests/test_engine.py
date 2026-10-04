"""Planning and applying against the fake API."""

import textwrap

import pytest

from nextdnsctl.config import ConfigError, parse
from nextdnsctl.executor import Executor
from nextdnsctl.model import canonicalize
from nextdnsctl.planner import Planner, PlanError


def config(text: str, file: str = "nextdns.yaml"):
    return parse(textwrap.dedent(text), file)


def plan_and_apply(client, cfg, index=0):
    planner = Planner(client)
    plan = planner.plan(cfg.profiles[index])
    result = Executor(client, planner).apply(plan)
    return plan, result


class TestCanonical:
    def test_strips_setup_and_readonly_fields(self, fake):
        profile = fake.add_profile("home", privacy={"blocklists": [{"id": "oisd", "name": "OISD", "entries": 5}]})
        canonical = canonicalize(profile)
        assert "setup" not in canonical and "fingerprint" not in canonical
        assert canonical["privacy"]["blocklists"] == [{"id": "oisd"}]


class TestPlan:
    def test_no_changes(self, fake, client):
        fake.add_profile("home", denylist=[{"id": "a.com", "active": True}])
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com]
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert not plan.has_changes
        assert plan.writes == 0

    def test_absent_sections_are_untouched(self, fake, client):
        fake.add_profile("home", security={"nrd": True}, allowlist=[{"id": "keep.com", "active": True}])
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com]
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert set(plan.patch) == {"denylist"}

    def test_managed_list_is_authoritative(self, fake, client):
        fake.add_profile("home", denylist=[{"id": "old.com", "active": True}, {"id": "a.com", "active": True}])
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com, new.com]
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        change = plan.diff.arrays[0]
        assert [e["id"] for e in change.added] == ["new.com"]
        assert [e["id"] for e in change.removed] == ["old.com"]
        assert plan.removals == 1

    def test_one_patch_for_everything(self, fake, client):
        fake.add_profile("home")
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com]
                allowlist: [b.com]
                security: {nrd: true, tlds: [zip]}
                privacy: {blocklists: [oisd], natives: [apple]}
                parentalControl: {services: [tiktok], categories: [gambling]}
                settings: {logs: {enabled: true}}
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert plan.writes == 1 and not plan.ops
        assert plan.patch["denylist"] == [{"id": "a.com"}]  # A7: active:true omitted
        assert plan.patch["security"] == {"nrd": True, "tlds": [{"id": "zip"}]}
        assert plan.patch["parentalControl"]["services"] == [{"id": "tiktok", "active": True, "recreation": False}]

    def test_unknown_catalog_id_with_suggestion(self, fake, client):
        fake.add_profile("home")
        cfg = config(
            """
            version: 1
            profiles:
              home:
                privacy: {blocklists: [oisdd]}
            """
        )
        with pytest.raises(ConfigError, match="Did you mean 'oisd'"):
            Planner(client).plan(cfg.profiles[0])

    def test_unchecked_setting_must_exist_live(self, fake, client):
        fake.add_profile("home")
        cfg = config(
            """
            version: 1
            profiles:
              home:
                security: {nrdd: true}
            """
        )
        with pytest.raises(ConfigError, match="unknown setting 'security.nrdd'"):
            Planner(client).plan(cfg.profiles[0])

    def test_unchecked_setting_known_to_the_live_profile(self, fake, client):
        profile = fake.add_profile("home")
        profile["security"]["brandNewFeature"] = False
        cfg = config(
            """
            version: 1
            profiles:
              home:
                security: {brandNewFeature: true}
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert plan.patch == {"security": {"brandNewFeature": True}}

    def test_service_keeps_live_recreation_flag(self, fake, client):
        fake.add_profile("home", parentalControl={"services": [{"id": "tiktok", "active": True, "recreation": True}]})
        cfg = config(
            """
            version: 1
            profiles:
              home:
                parentalControl: {services: [tiktok, instagram]}
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        services = plan.patch["parentalControl"]["services"]
        assert services[0] == {"id": "tiktok", "active": True, "recreation": True}

    def test_ambiguous_name(self, fake, client):
        fake.add_profile("Home", "aaa111")
        fake.add_profile("home", "bbb222")
        cfg = config("version: 1\nprofiles:\n  home: {denylist: [a.com]}\n")
        with pytest.raises(PlanError, match="ambiguous"):
            Planner(client).plan(cfg.profiles[0])

    def test_pinned_id_renames(self, fake, client):
        fake.add_profile("old name", "abc123")
        cfg = config("version: 1\nprofiles:\n  new name: {id: abc123}\n")
        plan = Planner(client).plan(cfg.profiles[0])
        assert plan.patch == {"name": "new name"}

    def test_missing_profile_is_created(self, fake, client):
        cfg = config("version: 1\nprofiles:\n  fresh: {denylist: [a.com]}\n")
        plan, result = plan_and_apply(client, cfg)
        assert plan.create
        assert result.ok and result.created
        created = fake.profiles[result.profile_id]
        assert created["name"] == "fresh"
        assert created["denylist"] == [{"id": "a.com", "active": True}]


class TestSources:
    def test_sources_merge_with_inline_entries(self, fake, client, tmp_path):
        fake.add_profile("home")
        (tmp_path / "hosts.txt").write_text("127.0.0.1 localhost\n0.0.0.0 a.com\n0.0.0.0 b.com\n")
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist:
                  domains: [a.com, c.com]
                  sources: [hosts.txt]
            """,
            str(tmp_path / "nextdns.yaml"),
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert [e["id"] for e in plan.patch["denylist"]] == ["a.com", "c.com", "b.com"]

    def test_invalid_source_line_fails_with_location(self, fake, client, tmp_path):
        fake.add_profile("home")
        (tmp_path / "list.txt").write_text("a.com\nnot a domain\n")
        cfg = config(
            "version: 1\nprofiles:\n  home:\n    denylist: {sources: [list.txt]}\n", str(tmp_path / "nextdns.yaml")
        )
        with pytest.raises(ConfigError) as info:
            Planner(client).plan(cfg.profiles[0])
        assert "list.txt:2" in str(info.value)
        assert "skip_invalid" in str(info.value)

    def test_skip_invalid_downgrades_to_warning(self, fake, client, tmp_path):
        fake.add_profile("home")
        (tmp_path / "list.txt").write_text("a.com\nnot a domain\n")
        cfg = config(
            "version: 1\nprofiles:\n  home:\n    denylist:\n      sources: [{file: list.txt, skip_invalid: true}]\n",
            str(tmp_path / "nextdns.yaml"),
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert plan.patch["denylist"] == [{"id": "a.com"}]
        assert "1 invalid line" in plan.warnings[0]

    def test_conflicting_active_state(self, fake, client, tmp_path):
        fake.add_profile("home")
        (tmp_path / "list.txt").write_text("a.com\n")
        cfg = config(
            "version: 1\nprofiles:\n  home:\n    denylist:\n      domains: [{domain: a.com, active: false}]\n"
            "      sources: [list.txt]\n",
            str(tmp_path / "nextdns.yaml"),
        )
        with pytest.raises(ConfigError, match="both active and inactive"):
            Planner(client).plan(cfg.profiles[0])


class TestApply:
    def test_apply_converges_in_one_write(self, fake, client):
        profile = fake.add_profile("home", denylist=[{"id": "old.com", "active": True}])
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com, {domain: b.com, active: false}]
                security: {nrd: true}
            """
        )
        plan, result = plan_and_apply(client, cfg)
        assert result.ok, result.failures
        assert len(fake.writes()) == 1
        assert profile["denylist"] == [{"id": "a.com", "active": True}, {"id": "b.com", "active": False}]
        assert profile["security"]["nrd"] is True

    def test_large_list_is_split_and_applied_incrementally(self, fake, client, clock):
        profile = fake.add_profile("home")
        domains = [f"blocked-domain-number-{i}.example.com" for i in range(3000)]
        text = "version: 1\nprofiles:\n  home:\n    security: {nrd: true}\n    denylist:\n" + "".join(
            f"      - {d}\n" for d in domains
        )
        planner = Planner(client)
        plan = planner.plan(parse(text).profiles[0])
        assert plan.incremental_sections == ["denylist"]
        assert plan.patch == {"security": {"nrd": True}}
        assert len(plan.ops) == 3000

        result = Executor(client, planner).apply(plan)
        assert result.ok, (result.failures, result.aborted)
        assert len(profile["denylist"]) == 3000
        # Paced at the rate limit rather than retried into it (A11): ~1 write per second.
        assert clock.slept == pytest.approx(3000, rel=0.05)

    def test_api_error_points_at_the_source_line(self, fake, client, monkeypatch):
        fake.add_profile("home")
        cfg = config(
            """
            version: 1
            profiles:
              home:
                denylist: [a.com, b.com]
            """
        )
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        plan.patch["denylist"][1]["id"] = "INVALID"  # something only the server rejects
        result = Executor(client, planner).apply(plan)
        assert not result.ok
        assert "nextdns.yaml:5" in result.failures[0][1]

    def test_rewrites_are_diffed_by_name_and_content(self, fake, client):
        profile = fake.add_profile(
            "home",
            rewrites=[
                {"id": "r1", "name": "nas.lan", "type": "A", "content": "192.168.1.10"},
                {"id": "r2", "name": "old.lan", "type": "A", "content": "192.168.1.20"},
            ],
        )
        cfg = config(
            """
            version: 1
            profiles:
              home:
                rewrites:
                  - {name: nas.lan, content: 192.168.1.10}
                  - {name: nas.lan, content: 192.168.1.11}
            """
        )
        plan, result = plan_and_apply(client, cfg)
        assert result.ok, result.failures
        assert [op.kind for op in plan.ops] == ["remove", "add"]
        assert sorted((r["name"], r["content"]) for r in profile["rewrites"]) == [
            ("nas.lan", "192.168.1.10"),
            ("nas.lan", "192.168.1.11"),
        ]

    def test_already_converged_ops_are_not_failures(self, fake, client):
        profile = fake.add_profile("home", rewrites=[])
        cfg = config("version: 1\nprofiles:\n  home:\n    rewrites: [{name: a.lan, content: 10.0.0.1}]\n")
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        # Someone else removes nothing and adds nothing; an extra DELETE of a missing id is fine.
        from nextdnsctl.planner import Op

        plan.ops.insert(0, Op("remove", "rewrites", "DELETE", "rewrites/gone", None, "gone"))
        result = Executor(client, planner).apply(plan)
        assert result.ok, result.failures
        assert len(profile["rewrites"]) == 1

    def test_drift_is_reported(self, fake, client, monkeypatch):
        fake.add_profile("home")
        cfg = config("version: 1\nprofiles:\n  home:\n    denylist: [a.com]\n")
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        original = fake._patch_profile
        monkeypatch.setattr(fake, "_patch_profile", lambda p, b: (original(p, {}),)[0])  # accepts but does nothing
        result = Executor(client, planner).apply(plan)
        assert result.drift
