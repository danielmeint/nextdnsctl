"""End-to-end CLI tests against the fake API."""

import json
import os

import pytest
import yaml

from nextdnsctl import cli as cli_pkg
from nextdnsctl.cli import cli
from nextdnsctl.client import Client

from .fake_api import API_KEY


@pytest.fixture(autouse=True)
def use_fake(fake, clock, monkeypatch):
    monkeypatch.setattr(
        cli_pkg, "make_client", lambda key, timeout=15.0: Client(key, session=fake, clock=clock, sleep=clock.sleep)
    )


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("NEXTDNS_API_KEY", API_KEY)


@pytest.fixture
def home(fake):
    return fake.add_profile("Home", "abc123", denylist=[{"id": "old.com", "active": True}])


def run(runner, *args, input=None):
    return runner.invoke(cli, list(args), input=input, catch_exceptions=False)


class TestAuth:
    def test_login_from_stdin_verifies_and_saves(self, runner, fake, tmp_path):
        fake.add_profile("Home")
        result = run(runner, "auth", "login", input=API_KEY + "\n")
        assert result.exit_code == 0, result.output
        saved = json.loads((tmp_path / "xdg" / "nextdnsctl" / "config.json").read_text())
        assert saved == {"api_key": API_KEY}
        assert oct(os.stat(tmp_path / "xdg" / "nextdnsctl" / "config.json").st_mode & 0o777) == "0o600"

    def test_rejected_key_is_not_saved(self, runner, tmp_path):
        result = runner.invoke(cli, ["auth", "login"], input="wrong-key\n")
        assert result.exit_code == 1
        assert "nothing was saved" in result.output
        assert not (tmp_path / "xdg" / "nextdnsctl" / "config.json").exists()

    def test_key_as_argument_warns(self, runner, fake):
        result = run(runner, "auth", API_KEY)
        assert result.exit_code == 0
        assert "shell history" in result.output

    def test_legacy_config_location_is_read(self, runner, fake, tmp_path):
        fake.add_profile("Home")
        legacy = tmp_path / "home" / ".nextdnsctl"
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_text(json.dumps({"api_key": API_KEY}))
        result = run(runner, "auth", "status")
        assert result.exit_code == 0
        assert ".nextdnsctl" in result.output

    def test_no_key_is_a_clean_error(self, runner):
        result = runner.invoke(cli, ["profile", "list"])
        assert result.exit_code == 1
        assert "No API key found" in result.output
        assert "Traceback" not in result.output


@pytest.mark.usefixtures("keyed")
class TestDeclarative:
    def test_pull_then_plan_has_no_changes(self, runner, fake, home, tmp_path):
        fake.add_profile(
            "Kids",
            "kid123",
            security={"nrd": True},
            rewrites=[{"id": "r1", "name": "nas.lan", "type": "A", "content": "192.168.1.10"}],
        )
        file = str(tmp_path / "nextdns.yaml")
        assert run(runner, "-f", file, "pull").exit_code == 0
        document = yaml.safe_load(open(file))
        assert set(document["profiles"]) == {"Home", "Kids"}
        assert "setup" not in document["profiles"]["Home"]
        assert "updateToken" not in open(file).read()

        result = run(runner, "-f", file, "plan")
        assert result.exit_code == 0, result.output
        assert "no changes" in result.output

    def test_pull_refuses_to_overwrite(self, runner, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("x")
        result = runner.invoke(cli, ["-f", str(file), "pull"])
        assert result.exit_code == 1 and "already exists" in result.output

    def test_plan_exit_code_and_json(self, runner, fake, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [new.com]\n")
        result = run(runner, "-f", str(file), "--json", "plan")
        assert result.exit_code == 2
        data = json.loads(result.stdout)
        change = data["profiles"][0]["changes"]["arrays"][0]
        assert [e["id"] for e in change["added"]] == ["new.com"]
        assert [e["id"] for e in change["removed"]] == ["old.com"]
        assert not fake.writes()

    def test_apply_requires_yes_without_a_terminal(self, runner, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [new.com]\n")
        result = runner.invoke(cli, ["-f", str(file), "apply"])
        assert result.exit_code == 2
        assert "--yes" in result.output

    def test_apply(self, runner, fake, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [new.com]\n    security: {nrd: true}\n")
        result = run(runner, "-f", str(file), "apply", "--yes")
        assert result.exit_code == 0, result.output
        assert home["denylist"] == [{"id": "new.com", "active": True}]
        assert home["security"]["nrd"] is True
        assert len(fake.writes()) == 1

    def test_apply_dry_run(self, runner, fake, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [new.com]\n")
        result = run(runner, "-f", str(file), "--dry-run", "apply")
        assert result.exit_code == 0
        assert not fake.writes()

    def test_config_error_has_file_and_line(self, runner, home, tmp_path):
        file = tmp_path / "nextdns.yaml"
        file.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [ok.com, bad..com]\n")
        result = runner.invoke(cli, ["-f", str(file), "plan"])
        assert result.exit_code == 1
        assert f"{file}:4" in result.output


@pytest.mark.usefixtures("keyed")
class TestListShortcuts:
    def test_add_is_one_write(self, runner, fake, home):
        result = run(runner, "-p", "home", "denylist", "add", "a.com", "https://b.com/x", "münchen.de")
        assert result.exit_code == 0, result.output
        assert len(fake.writes()) == 1
        assert {e["id"] for e in home["denylist"]} == {"old.com", "a.com", "b.com", "xn--mnchen-3ya.de"}

    def test_legacy_positional_profile_warns(self, runner, fake, home):
        result = run(runner, "denylist", "add", "Home", "a.com")
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output
        assert any(e["id"] == "a.com" for e in home["denylist"])

    def test_no_profile(self, runner, home):
        result = runner.invoke(cli, ["denylist", "add", "a.com"])
        assert result.exit_code == 2
        assert "No profile given" in result.output

    def test_invalid_domain_is_an_error(self, runner, fake, home):
        result = runner.invoke(cli, ["-p", "home", "denylist", "add", "a.com", "*.bad.com"])
        assert result.exit_code == 1
        assert "*.bad.com" in result.output
        assert not fake.writes()

    def test_add_skips_present_and_reports_mismatch(self, runner, fake, home):
        home["denylist"].append({"id": "off.com", "active": False})
        result = run(runner, "-p", "home", "denylist", "add", "old.com", "off.com")
        assert "No changes needed" in result.output
        assert "--update-existing" in result.output
        assert not fake.writes()

    def test_remove(self, runner, fake, home):
        result = run(runner, "-p", "home", "denylist", "remove", "old.com", "missing.com")
        assert result.exit_code == 0
        assert home["denylist"] == []
        assert "Not in the list: 1" in result.output

    def test_import_is_strict(self, runner, fake, home, tmp_path):
        source = tmp_path / "list.txt"
        source.write_text("0.0.0.0 a.com\n192.168.1.1 nas.lan\n")
        result = runner.invoke(cli, ["-p", "home", "denylist", "import", str(source)])
        assert result.exit_code == 1
        assert "line 2" in result.output and "rewrite" in result.output
        assert not fake.writes()

        result = run(runner, "-p", "home", "denylist", "import", str(source), "--skip-invalid")
        assert result.exit_code == 0
        assert any(e["id"] == "a.com" for e in home["denylist"])

    def test_export_stdout_is_only_domains(self, runner, fake, home):
        result = run(runner, "-p", "home", "denylist", "export")
        assert result.stdout == "old.com\n"

    def test_clear_requires_confirmation(self, runner, fake, home):
        result = runner.invoke(cli, ["-p", "home", "denylist", "clear"])
        assert result.exit_code == 2
        assert home["denylist"]
        result = run(runner, "-p", "home", "denylist", "clear", "--yes")
        assert result.exit_code == 0
        assert home["denylist"] == []

    def test_deprecated_concurrency_option(self, runner, home):
        result = run(runner, "--concurrency", "5", "-p", "home", "denylist", "list")
        assert "no effect" in result.output
        assert "old.com" in result.output


@pytest.mark.usefixtures("keyed")
class TestOther:
    def test_rewrites(self, runner, fake, home):
        assert run(runner, "-p", "home", "rewrites", "add", "NAS.lan", "192.168.1.10").exit_code == 0
        assert [(r["name"], r["content"]) for r in home["rewrites"]] == [("nas.lan", "192.168.1.10")]
        assert run(runner, "-p", "home", "rewrites", "remove", "nas.lan").exit_code == 0
        assert home["rewrites"] == []

    def test_catalog(self, runner):
        result = run(runner, "catalog", "blocklists")
        assert "oisd" in result.output

    def test_profile_create_and_delete(self, runner, fake):
        result = run(runner, "profile", "create", "Scratch")
        profile_id = result.stdout.strip()
        assert fake.profiles[profile_id]["name"] == "Scratch"
        assert run(runner, "profile", "delete", "Scratch", "--yes").exit_code == 0
        assert profile_id not in fake.profiles

    def test_why(self, runner, fake, home):
        home["settings"]["logs"]["enabled"] = True
        fake.logs["abc123"] = [
            {
                "timestamp": "2026-10-04T10:00:00Z",
                "domain": "ads.example.com",
                "status": "blocked",
                "reasons": [{"id": "oisd", "name": "OISD"}],
            },
            {
                "timestamp": "2026-10-04T09:00:00Z",
                "domain": "example.com",
                "status": "blocked",
                "reasons": [{"id": "denylist", "name": "Denylist"}],
            },
            {
                "timestamp": "2026-10-04T08:00:00Z",
                "domain": "notexample.com",
                "status": "blocked",
                "reasons": [{"id": "x", "name": "X"}],
            },
        ]
        result = run(runner, "-p", "home", "why", "example.com")
        assert "OISD [oisd]: 1 query" in result.output
        assert "Denylist [denylist]: 1 query" in result.output
        assert "X [x]" not in result.output

    def test_why_without_logging(self, runner, home):
        result = runner.invoke(cli, ["-p", "home", "why", "example.com"])
        assert result.exit_code == 1
        assert "logging is disabled" in result.output

    def test_why_allow(self, runner, fake, home):
        home["settings"]["logs"]["enabled"] = True
        result = run(runner, "-p", "home", "why", "example.com", "--allow")
        assert result.exit_code == 0
        assert home["allowlist"] == [{"id": "example.com", "active": True}]

    def test_logs(self, runner, fake, home):
        home["settings"]["logs"]["enabled"] = True
        fake.logs["abc123"] = [
            {"timestamp": f"2026-10-04T10:00:{i:02d}Z", "domain": f"d{i}.com", "status": "default", "reasons": []}
            for i in range(30)
        ]
        result = run(runner, "-p", "home", "logs", "-n", "5")
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 5
        assert "d4.com" in lines[0] and "d0.com" in lines[-1]  # oldest first


@pytest.mark.usefixtures("keyed")
def test_removing_several_rewrites_asks_first(runner, fake, home):
    home["rewrites"] = [
        {"id": "r1", "name": "nas.lan", "type": "A", "content": "192.168.1.10"},
        {"id": "r2", "name": "nas.lan", "type": "A", "content": "192.168.1.11"},
    ]
    result = runner.invoke(cli, ["-p", "home", "rewrites", "remove", "nas.lan"])
    assert result.exit_code == 2 and len(home["rewrites"]) == 2
    assert run(runner, "-p", "home", "rewrites", "remove", "nas.lan", "--yes").exit_code == 0
    assert home["rewrites"] == []


@pytest.mark.usefixtures("keyed")
def test_quick_edits_never_fetch_the_whole_profile(runner, fake, home):
    run(runner, "-p", "home", "denylist", "add", "a.com")
    run(runner, "-p", "home", "rewrites", "add", "nas.lan", "10.0.0.1")
    full_profile_reads = [r for r in fake.requests if r[0] == "GET" and r[1] == "profiles/abc123"]
    assert full_profile_reads == []  # only /denylist and /rewrites, which NextDNS throttles less


def test_rejected_key_message(runner, monkeypatch):
    monkeypatch.setenv("NEXTDNS_API_KEY", "wrong")
    result = runner.invoke(cli, ["profile", "list"])
    assert result.exit_code == 1
    assert "NextDNS rejected the API key (from NEXTDNS_API_KEY)" in result.output
    assert "auth login" in result.output


@pytest.fixture
def small_requests(monkeypatch):
    from nextdnsctl import planner as planner_mod

    monkeypatch.setattr(planner_mod, "SAFE_BODY_BYTES", 2048)


def many(prefix, count):
    return [f"{prefix}-{i}.example.com" for i in range(count)]


@pytest.mark.usefixtures("keyed")
class TestWriteStrategy:
    def test_adding_one_domain_is_one_post(self, runner, fake, home):
        result = run(runner, "-p", "home", "denylist", "add", "a.com")
        assert result.exit_code == 0, result.output
        assert fake.writes() == [("POST", "profiles/abc123/denylist", {"id": "a.com", "active": True})]
        assert "1 individual write" in result.output

    def test_plan_shows_split_and_gap_hint(self, runner, fake, home, tmp_path, small_requests):
        home["denylist"] = [{"id": d, "active": True} for d in many("k", 400)]
        file = tmp_path / "nextdns.yaml"
        file.write_text(
            "version: 1\nprofiles:\n  Home:\n    denylist:\n"
            + "".join(f"      - {d}\n" for d in many("k", 120))
            + "    allowlist:\n"
            + "".join(f"      - {d}\n" for d in many("a", 60))
            + "    security: {nrd: true}\n"
        )
        result = runner.invoke(cli, ["-f", str(file), "plan"])
        assert result.exit_code == 2, result.output
        assert "denylist is too large for one request: changed entry by entry" in result.output
        assert "With --allow-gaps:" in result.output

        result = runner.invoke(cli, ["-f", str(file), "--allow-gaps", "--json", "plan"])
        writes = json.loads(result.stdout)["profiles"][0]["writes"]
        assert writes["partial_sections"] == ["denylist"] and writes["incremental_sections"] == []
        assert writes["atomic_updates"] == 2 and writes["gap_entries"] > 0

        result = runner.invoke(cli, ["-f", str(file), "--allow-gaps", "plan"])
        assert "2 atomic updates" in result.output
        assert "will be missing" in result.output

        result = run(runner, "-f", str(file), "--allow-gaps", "apply", "--yes")
        assert result.exit_code == 0, result.output
        assert sorted(e["id"] for e in home["denylist"]) == sorted(many("k", 120))
        assert len(home["allowlist"]) == 60


@pytest.mark.usefixtures("keyed")
class TestChangedDuringConfirmation:
    """NextDNS changes while the confirmation prompt is open."""

    @pytest.fixture
    def meanwhile(self, monkeypatch, fake, home, clock):
        from nextdnsctl.cli import declarative, lists

        def confirmed(plans, yes):
            home["denylist"].insert(0, {"id": "concurrent.com", "active": True})
            clock.now += 60

        monkeypatch.setattr(declarative, "confirm", confirmed)
        monkeypatch.setattr(lists, "confirm", confirmed)

    @pytest.fixture
    def file(self, tmp_path):
        path = tmp_path / "nextdns.yaml"
        path.write_text("version: 1\nprofiles:\n  Home:\n    denylist: [a.com, b.com]\n")
        return str(path)

    def test_apply_shows_the_new_plan_and_can_be_declined(self, runner, fake, home, file, meanwhile):
        result = runner.invoke(cli, ["-f", file, "apply"], input="n\n")
        assert "changed in NextDNS since it was read" in result.output
        assert "− concurrent.com" in result.output
        assert "Home: not applied" in result.output
        assert result.exit_code == 1
        assert not fake.writes()
        assert any(e["id"] == "concurrent.com" for e in home["denylist"])

    def test_apply_after_accepting_the_new_plan(self, runner, fake, home, file, meanwhile):
        result = runner.invoke(cli, ["-f", file, "apply"], input="y\n")
        assert result.exit_code == 0, result.output
        assert sorted(e["id"] for e in home["denylist"]) == ["a.com", "b.com"]

    def test_clear_asks_again(self, runner, fake, home, meanwhile):
        home["denylist"].append({"id": "other.com", "active": True})  # 2 entries: cleared by one PATCH
        result = runner.invoke(cli, ["-p", "home", "denylist", "clear"], input="n\n")
        assert "Apply the new plan?" in result.output
        assert result.exit_code == 1 and not fake.writes()


@pytest.mark.usefixtures("keyed")
def test_quick_edit_replan_reports_once(runner, fake, home, clock, monkeypatch):
    """A re-plan rebuilds the edit from the fresh list without double-counting what was skipped."""
    from nextdnsctl import executor as executor_mod

    monkeypatch.setattr(executor_mod, "FRESH_SECONDS", -1)  # always re-check
    original = fake._domain_list

    def changing(profile, name, method, rest, body):
        if method == "GET" and not getattr(changing, "done", False) and len(fake.requests) > 2:
            changing.done = True
            profile[name].insert(0, {"id": "concurrent.com", "active": True})
        return original(profile, name, method, rest, body)

    monkeypatch.setattr(fake, "_domain_list", changing)
    result = run(runner, "-p", "home", "denylist", "add", "old.com", "a.com", "b.com")
    assert result.exit_code == 0, result.output
    assert sorted(e["id"] for e in home["denylist"]) == ["a.com", "b.com", "concurrent.com", "old.com"]
    assert "Already present: 1" in result.output


@pytest.mark.usefixtures("keyed")
def test_entry_added_by_someone_else_during_a_quick_add_is_not_drift(runner, fake, home, monkeypatch):
    original = fake._domain_list

    def concurrent(profile, name, method, rest, body):
        response = original(profile, name, method, rest, body)
        if method == "POST":
            profile[name].insert(0, {"id": "concurrent.com", "active": True})
        return response

    monkeypatch.setattr(fake, "_domain_list", concurrent)
    result = run(runner, "-p", "home", "denylist", "add", "a.com")
    assert result.exit_code == 0, result.output
    assert sorted(e["id"] for e in home["denylist"]) == ["a.com", "concurrent.com", "old.com"]
