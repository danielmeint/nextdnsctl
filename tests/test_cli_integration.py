"""Integration tests for CLI commands with mocked API."""

import requests_mock as rm

from nextdnsctl.nextdnsctl import cli
from nextdnsctl.api import API_BASE


class TestProfileList:
    """Tests for profile-list command."""

    def test_lists_profiles(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)

            result = runner.invoke(cli, ["profile-list"])

            assert result.exit_code == 0
            assert "abc1234: My Profile" in result.output
            assert "xyz9876: Kids Profile" in result.output

    def test_empty_profiles(self, runner, mock_api_key):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json={"data": []})

            result = runner.invoke(cli, ["profile-list"])

            assert result.exit_code == 0
            assert "No profiles found" in result.output


class TestDenylistCommands:
    """Tests for denylist subcommands."""

    def test_denylist_list(self, runner, mock_api_key, mock_profiles_response, mock_denylist_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            m.get(f"{API_BASE}profiles/abc1234/denylist", json=mock_denylist_response)

            result = runner.invoke(cli, ["denylist", "list", "abc1234"])

            assert result.exit_code == 0
            assert "bad-domain.com" in result.output
            assert "inactive-domain.com (inactive)" in result.output

    def test_denylist_list_by_name(self, runner, mock_api_key, mock_profiles_response, mock_denylist_response):
        """Profile resolution by name should work."""
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            m.get(f"{API_BASE}profiles/abc1234/denylist", json=mock_denylist_response)

            result = runner.invoke(cli, ["denylist", "list", "My Profile"])

            assert result.exit_code == 0
            assert "bad-domain.com" in result.output

    def test_denylist_add(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            adapter = m.post(f"{API_BASE}profiles/abc1234/denylist", json={"id": "bad.com", "active": True})

            result = runner.invoke(cli, ["--concurrency", "1", "denylist", "add", "abc1234", "bad.com"])

            assert result.exit_code == 0
            assert adapter.called
            assert adapter.last_request.json() == {"id": "bad.com", "active": True}

    def test_denylist_add_inactive(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            adapter = m.post(f"{API_BASE}profiles/abc1234/denylist", json={"id": "bad.com", "active": False})

            result = runner.invoke(cli, ["--concurrency", "1", "denylist", "add", "abc1234", "--inactive", "bad.com"])

            assert result.exit_code == 0
            assert adapter.last_request.json() == {"id": "bad.com", "active": False}

    def test_denylist_remove(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            adapter = m.delete(f"{API_BASE}profiles/abc1234/denylist/bad.com", status_code=204)

            result = runner.invoke(cli, ["--concurrency", "1", "denylist", "remove", "abc1234", "bad.com"])

            assert result.exit_code == 0
            assert adapter.called


class TestDryRun:
    """Tests for --dry-run mode."""

    def test_denylist_add_dry_run_makes_no_requests(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            # No POST mock - if it tries to POST, it will fail
            adapter = m.post(f"{API_BASE}profiles/abc1234/denylist", status_code=500)

            result = runner.invoke(cli, ["--dry-run", "denylist", "add", "abc1234", "bad.com"])

            assert result.exit_code == 0
            assert "[DRY-RUN]" in result.output
            assert "bad.com" in result.output
            assert not adapter.called  # No actual request made


class TestProfileResolution:
    """Tests for profile ID/name resolution."""

    def test_profile_not_found(self, runner, mock_api_key, mock_profiles_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)

            result = runner.invoke(cli, ["denylist", "list", "Nonexistent Profile"])

            assert result.exit_code != 0
            assert "not found" in result.output

    def test_case_insensitive_name_match(self, runner, mock_api_key, mock_profiles_response, mock_denylist_response):
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            m.get(f"{API_BASE}profiles/abc1234/denylist", json=mock_denylist_response)

            result = runner.invoke(cli, ["denylist", "list", "my profile"])  # lowercase

            assert result.exit_code == 0
            assert "bad-domain.com" in result.output


class TestAuthCommand:
    """Tests for auth command."""

    def test_auth_saves_api_key(self, runner, tmp_path, monkeypatch):
        """Auth command should save API key to config file."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"

        monkeypatch.setattr("nextdnsctl.config.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))
        # Also patch in nextdnsctl.py since it imports from config
        monkeypatch.setattr("nextdnsctl.nextdnsctl.save_api_key",
                            lambda k: __import__('nextdnsctl.config', fromlist=['save_api_key']).save_api_key(k))

        result = runner.invoke(cli, ["auth", "my-test-key-123"])

        assert result.exit_code == 0
        assert "saved successfully" in result.output
        assert config_file.exists()

    def test_auth_without_key_fails(self, runner):
        """Auth command should fail when no key provided."""
        result = runner.invoke(cli, ["auth"])

        assert result.exit_code != 0
        assert "Missing argument" in result.output


class TestImportCommand:
    """Tests for import command."""

    def test_import_from_file(self, runner, mock_api_key, mock_profiles_response, tmp_path):
        """Import should read domains from a file and add them."""
        # Create a test file with domains
        domains_file = tmp_path / "domains.txt"
        domains_file.write_text("bad1.com\nbad2.com\n# comment line\nbad3.com # inline comment\n")

        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            adapter = m.post(f"{API_BASE}profiles/abc1234/denylist", json={"id": "test", "active": True})

            result = runner.invoke(cli, ["--concurrency", "1", "denylist", "import", "abc1234", str(domains_file)])

            assert result.exit_code == 0
            # Should have made 3 POST requests (comment lines excluded)
            assert adapter.call_count == 3
            # Verify the domains that were sent
            requests_made = [req.json()["id"] for req in adapter.request_history]
            assert "bad1.com" in requests_made
            assert "bad2.com" in requests_made
            assert "bad3.com" in requests_made

    def test_import_empty_file(self, runner, mock_api_key, mock_profiles_response, tmp_path):
        """Import should handle empty files gracefully."""
        domains_file = tmp_path / "empty.txt"
        domains_file.write_text("# only comments\n\n   \n")

        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)

            result = runner.invoke(cli, ["denylist", "import", "abc1234", str(domains_file)])

            assert "No domains found" in result.output

    def test_import_nonexistent_file(self, runner, mock_api_key, mock_profiles_response):
        """Import should fail gracefully for nonexistent files."""
        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)

            result = runner.invoke(cli, ["denylist", "import", "abc1234", "/nonexistent/file.txt"])

            assert result.exit_code != 0
            assert "Error reading source" in result.output

    def test_import_dry_run(self, runner, mock_api_key, mock_profiles_response, tmp_path):
        """Import with --dry-run should not make API calls."""
        domains_file = tmp_path / "domains.txt"
        domains_file.write_text("bad1.com\nbad2.com\n")

        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            adapter = m.post(f"{API_BASE}profiles/abc1234/denylist", status_code=500)

            result = runner.invoke(cli, ["--dry-run", "denylist", "import", "abc1234", str(domains_file)])

            assert result.exit_code == 0
            assert "[DRY-RUN]" in result.output
            assert "bad1.com" in result.output
            assert "bad2.com" in result.output
            assert not adapter.called
