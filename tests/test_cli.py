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
