"""Planning and applying against the fake API."""

import textwrap

import pytest

from nextdnsctl.client import encode_body
from nextdnsctl.config import ConfigError, parse
from nextdnsctl.executor import FRESH_SECONDS, Executor
from nextdnsctl.model import canonicalize
from nextdnsctl.planner import SAFE_BODY_BYTES, Planner, PlanError, Resolved


def config(text: str, file: str = "nextdns.yaml"):
    return parse(textwrap.dedent(text), file)


def body(plan):
    """The body of the plan's only PATCH."""
    assert len(plan.patches) == 1, plan.patches
    return plan.patches[0].body


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
                denylist: [a.com, b.com]
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert set(body(plan)) == {"denylist"}

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
                denylist: [a.com, c.com]
                allowlist: [b.com, d.com]
                security: {nrd: true, tlds: [zip]}
                privacy: {blocklists: [oisd], natives: [apple]}
                parentalControl: {services: [tiktok], categories: [gambling]}
                settings: {logs: {enabled: true}}
            """
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert plan.writes == 1 and not plan.ops
        assert body(plan)["denylist"] == [{"id": "a.com"}, {"id": "c.com"}]  # A7: active:true omitted
        assert body(plan)["security"] == {"nrd": True, "tlds": [{"id": "zip"}]}
        assert body(plan)["parentalControl"]["services"] == [{"id": "tiktok", "active": True, "recreation": False}]

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
        assert body(plan) == {"security": {"brandNewFeature": True}}

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
        services = body(plan)["parentalControl"]["services"]
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
        assert body(plan) == {"name": "new name"}

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
        assert [e["id"] for e in body(plan)["denylist"]] == ["a.com", "c.com", "b.com"]

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
        (tmp_path / "list.txt").write_text("a.com\nnot a domain\nb.com\n")
        cfg = config(
            "version: 1\nprofiles:\n  home:\n    denylist:\n      sources: [{file: list.txt, skip_invalid: true}]\n",
            str(tmp_path / "nextdns.yaml"),
        )
        plan = Planner(client).plan(cfg.profiles[0])
        assert body(plan)["denylist"] == [{"id": "a.com"}, {"id": "b.com"}]
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

    def test_large_new_list_is_one_patch_plus_the_rest(self, fake, client, clock):
        profile = fake.add_profile("home")
        domains = [f"blocked-domain-number-{i}.example.com" for i in range(3000)]
        text = "version: 1\nprofiles:\n  home:\n    security: {nrd: true}\n    denylist:\n" + "".join(
            f"      - {d}\n" for d in domains
        )
        planner = Planner(client)
        plan = planner.plan(parse(text).profiles[0])
        assert plan.partial_sections == ["denylist"] and not plan.incremental_sections
        assert plan.patches[0].body == {"security": {"nrd": True}}
        in_patch = len(plan.patches[1].body["denylist"])
        assert 2000 < in_patch < 3000
        assert all(len(encode_body(p.body)) <= SAFE_BODY_BYTES for p in plan.patches)
        assert len(plan.ops) == 3000 - in_patch and {op.method for op in plan.ops} == {"POST"}

        result = Executor(client, planner).apply(plan)
        assert result.ok, (result.failures, result.aborted)
        assert sorted(e["id"] for e in profile["denylist"]) == sorted(domains)
        # Paced at the rate limit rather than retried into it (A11): ~1 write per second.
        assert clock.slept == pytest.approx(len(plan.ops) + 2, rel=0.05)

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
        plan.patches[0].body["denylist"][1]["id"] = "INVALID"  # something only the server rejects
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
        cfg = config("version: 1\nprofiles:\n  home:\n    denylist: [a.com, b.com]\n")
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        original = fake._patch_profile
        monkeypatch.setattr(fake, "_patch_profile", lambda p, b: (original(p, {}),)[0])  # accepts but does nothing
        result = Executor(client, planner).apply(plan)
        assert result.drift


def domains(prefix, count):
    return [f"{prefix}-{i}.example.com" for i in range(count)]


def entries(names, active=True):
    return [{"id": d, "active": active} for d in names]


def list_config(**lists):
    text = "version: 1\nprofiles:\n  home:\n"
    for name, values in lists.items():
        if isinstance(values, dict):
            text += f"    {name}: {values}\n".replace("'", "")
            continue
        text += f"    {name}:\n" + "".join(f"      - {d}\n" for d in values)
    return parse(text)


def ids(profile, section="denylist"):
    return sorted(e["id"] for e in profile[section])


@pytest.fixture
def small_requests(monkeypatch):
    """Make the request size limit small, so tests can use lists of ~100 entries instead of thousands."""
    from nextdnsctl import planner as planner_mod

    monkeypatch.setattr(planner_mod, "SAFE_BODY_BYTES", 2048)  # ~70 entries of ~29 bytes
    return 2048


def watch_writes(fake, check):
    """Call `check()` after every request the fake answers."""
    original = fake.request

    def request(*args, **kwargs):
        response = original(*args, **kwargs)
        check()
        return response

    fake.request = request


class TestWriteStrategy:
    def test_lists_too_big_together_get_a_patch_each(self, fake, client):
        profile = fake.add_profile("home")
        deny, allow = domains("blocked-domain-entry", 1600), domains("allowed-domain-entry", 1600)
        planner = Planner(client)
        plan = planner.plan(list_config(denylist=deny, allowlist=allow, security={"nrd": True}).profiles[0])
        assert len(plan.patches) == 2 and not plan.ops
        assert not plan.incremental_sections and not plan.partial_sections
        assert sorted(s for p in plan.patches for s in p.sections) == ["allowlist", "denylist", "security"]
        assert all(len(encode_body(p.body)) <= SAFE_BODY_BYTES for p in plan.patches)

        result = Executor(client, planner).apply(plan)
        assert result.ok, result.failures
        assert result.patches_done == 2 and len(fake.writes()) == 2
        assert ids(profile) == sorted(deny) and ids(profile, "allowlist") == sorted(allow)
        assert profile["security"]["nrd"] is True

    def test_failed_second_patch_is_partial_and_points_at_its_line(self, fake, client, small_requests):
        fake.add_profile("home")
        cfg = list_config(denylist=domains("d", 60), allowlist=domains("a", 60))
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        assert len(plan.patches) == 2
        second = plan.patches[1]
        section = second.sections[0]
        second.body[section][3]["id"] = "INVALID"
        result = Executor(client, planner).apply(plan)
        assert result.patches_done == 1 and result.partial
        what, why = result.failures[0]
        assert section in what
        line = 4 + 4 + (61 if section == "allowlist" else 0)  # header lines + index 3 in that list
        assert f"nextdns.yaml:{line}" in why

    def test_single_change_uses_the_entry_endpoint(self, fake, client):
        profile = fake.add_profile("home", denylist=entries(["keep.com", "drop.com"]))
        planner = Planner(client)
        add = planner.plan(list_config(denylist=["keep.com", "drop.com", "new.com"]).profiles[0])
        assert not add.patches
        assert [(op.method, op.path) for op in add.ops] == [("POST", "denylist")]

        remove = planner.plan(list_config(denylist=["keep.com"]).profiles[0])
        assert [(op.method, op.path) for op in remove.ops] == [("DELETE", "denylist/drop.com")]

        toggle = planner.plan(list_config(denylist=["keep.com", "{domain: drop.com, active: false}"]).profiles[0])
        assert [(op.method, op.path, op.body) for op in toggle.ops] == [
            ("PATCH", "denylist/drop.com", {"active": False})
        ]

        # Unlike a full-list PATCH, it can't lose an entry someone added after the plan was made.
        fake.profiles[profile["id"]]["denylist"].insert(0, {"id": "concurrent.com", "active": True})
        result = Executor(client, planner).apply(add, verify=False)
        assert result.ok, result.failures
        assert ids(profile) == ["concurrent.com", "drop.com", "keep.com", "new.com"]

    def test_large_list_keeps_every_entry_while_growing(self, fake, client, small_requests):
        live = domains("k", 50)
        profile = fake.add_profile("home", denylist=entries(live))
        target = live[:45] + domains("n", 60)  # remove 5, add 60: 65 changes one by one
        planner = Planner(client)
        plan = planner.plan(list_config(denylist=target).profiles[0])
        assert plan.partial_sections == ["denylist"] and plan.gap == 0
        patched = {e["id"] for e in plan.patches[0].body["denylist"]}
        assert set(live[:45]) <= patched
        assert plan.writes == 1 + (60 - (len(patched) - 45)) < 65
        assert len(encode_body(plan.patches[0].body)) <= small_requests

        missing = []
        watch_writes(fake, lambda: missing.extend(set(live[:45]) - set(ids(profile))))
        result = Executor(client, planner).apply(plan)
        assert result.ok, result.failures
        assert ids(profile) == sorted(target)
        assert not missing  # kept entries were never absent

    def test_large_list_whose_kept_entries_dont_fit_goes_entry_by_entry(self, fake, client, small_requests):
        live = domains("k", 400)
        fake.add_profile("home", denylist=entries(live))
        plan = Planner(client).plan(list_config(denylist=live[:120]).profiles[0])
        assert plan.incremental_sections == ["denylist"] and not plan.patches
        assert len(plan.ops) == 280 and {op.method for op in plan.ops} == {"DELETE"}
        [hint] = plan.gap_hints
        assert hint.section == "denylist" and hint.writes < 280 and hint.writes == 1 + hint.gap

    def test_allow_gaps_drops_and_readds_kept_entries(self, fake, client, small_requests):
        live = domains("k", 400)
        profile = fake.add_profile("home", denylist=entries(live))
        target = live[:120] + domains("n", 5)
        planner = Planner(client, allow_gaps=True)
        plan = planner.plan(list_config(denylist=target).profiles[0])
        assert plan.partial_sections == ["denylist"] and not plan.gap_hints
        in_patch = len(plan.patches[0].body["denylist"])
        assert plan.gap == 120 - in_patch > 0
        assert plan.writes == 1 + plan.gap + 5 < 280 + 5
        # The dropped entries are re-added before anything else.
        gap = {op.label for op in plan.ops[: plan.gap]}
        assert gap == set(live[in_patch:120])

        result = Executor(client, planner).apply(plan)
        assert result.ok, (result.failures, result.drift)
        assert ids(profile) == sorted(target)

    def test_allow_gaps_only_when_it_saves_writes(self, fake, client, small_requests):
        live = domains("k", 200)
        fake.add_profile("home", denylist=entries(live))
        plan = Planner(client, allow_gaps=True).plan(list_config(denylist=live[:190]).profiles[0])
        assert plan.incremental_sections == ["denylist"] and plan.gap == 0
        assert len(plan.ops) == 10

    def test_section_without_entry_endpoints_too_large_is_an_error(self, fake, client, monkeypatch):
        from nextdnsctl import planner as planner_mod

        monkeypatch.setattr(planner_mod, "SAFE_BODY_BYTES", 30)
        fake.add_profile("home")
        cfg = config("version: 1\nprofiles:\n  home:\n    privacy: {blocklists: [oisd, nextdns-recommended]}\n")
        with pytest.raises(PlanError, match="privacy"):
            Planner(client).plan(cfg.profiles[0])


class TestFreshness:
    def quick_add(self, planner, profile, names):
        """Plan like `denylist add`: the target is built from whatever the live list is."""

        def resolve(api):
            live = {e["id"] for e in api.get("denylist") or []}
            return Resolved(overlay={"denylist": (api.get("denylist") or []) + entries(set(names) - live)})

        api = planner.live_for(profile, {"denylist"})
        return planner.plan_with(profile["name"], profile["id"], resolve, api)

    def test_quick_edit_replans_when_the_list_changed(self, fake, client, clock):
        profile = fake.add_profile("home", denylist=entries(["old.com"]))
        planner = Planner(client)
        plan = self.quick_add(planner, profile, ["a.com", "b.com"])
        assert plan.patches
        fake.profiles[profile["id"]]["denylist"].insert(0, {"id": "concurrent.com", "active": True})
        clock.now += FRESH_SECONDS + 1

        result = Executor(client, planner).apply(plan)
        assert result.ok and result.rebased
        assert ids(profile) == ["a.com", "b.com", "concurrent.com", "old.com"]
        full_reads = [r for r in fake.requests if r == ("GET", f"profiles/{profile['id']}", None)]
        assert not full_reads  # the re-check reads the list endpoint only

    def test_young_plan_is_not_rechecked(self, fake, client):
        profile = fake.add_profile("home", denylist=entries(["old.com"]))
        planner = Planner(client)
        plan = self.quick_add(planner, profile, ["a.com", "b.com"])
        reads = len(fake.requests)
        result = Executor(client, planner).apply(plan, verify=False)
        assert result.ok and not result.rebased
        assert len(fake.requests) == reads + 1  # just the PATCH

    def test_unchanged_list_keeps_the_plan(self, fake, client, clock):
        profile = fake.add_profile("home", denylist=entries(["old.com"]))
        planner = Planner(client)
        plan = self.quick_add(planner, profile, ["a.com", "b.com"])
        clock.now += FRESH_SECONDS + 1
        rebases = []
        result = Executor(client, planner, on_rebase=lambda old, new: rebases.append(new) or True).apply(plan)
        assert result.ok and not result.rebased and not rebases

    def test_apply_asks_again_and_can_be_declined(self, fake, client, clock):
        profile = fake.add_profile("home", denylist=entries(["old.com"]), security={"nrd": False})
        cfg = list_config(denylist=["a.com", "b.com"], security={"nrd": True})
        planner = Planner(client)
        plan = planner.plan(cfg.profiles[0])
        fake.profiles[profile["id"]]["denylist"].insert(0, {"id": "concurrent.com", "active": True})
        clock.now += FRESH_SECONDS + 1

        seen = []
        result = Executor(client, planner, on_rebase=lambda old, new: seen.append(new) and False).apply(plan)
        assert result.aborted and not result.partial and not fake.writes()
        removed = [e["id"] for c in seen[0].diff.arrays for e in c.removed]
        assert sorted(removed) == ["concurrent.com", "old.com"]  # the new plan shows the new removal

    def test_values_only_plan_is_not_rechecked(self, fake, client, clock):
        fake.add_profile("home", security={"nrd": False})
        planner = Planner(client)
        plan = planner.plan(list_config(security={"nrd": True}).profiles[0])
        clock.now += FRESH_SECONDS + 1
        reads = len(fake.requests)
        result = Executor(client, planner).apply(plan, verify=False)
        assert result.ok and len(fake.requests) == reads + 1  # objects merge (A2): nothing to overwrite
