"""Tests for concurrency/parallelism validation."""

import requests_mock as rm

from nextdnsctl.nextdnsctl import cli
from nextdnsctl.api import API_BASE


class TestConcurrency:
    """Tests to verify parallel execution works correctly."""

    def test_parallel_mode_uses_summary_output(self, runner, mock_api_key, mock_profiles_response, tmp_path):
        """
        Verify that with concurrency > 1, parallel mode is used.

        Parallel mode shows a summary with "Completed: X, Failed: Y, Skipped: Z"
        while sequential mode shows per-domain output like "Added domain.com".
        """
        domains_file = tmp_path / "domains.txt"
        domains_file.write_text("d1.com\nd2.com\nd3.com\n")

        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            m.post(
                f"{API_BASE}profiles/abc1234/denylist",
                json={"id": "test", "active": True},
            )

            result = runner.invoke(
                cli,
                [
                    "--concurrency",
                    "3",
                    "denylist",
                    "import",
                    "abc1234",
                    str(domains_file),
                ],
            )

            assert result.exit_code == 0
            # Parallel mode shows summary format
            assert "Completed:" in result.output
            assert "Failed:" in result.output
            # Should NOT show per-domain "Added" messages (that's sequential mode)
            assert "Added d1.com" not in result.output

    def test_sequential_mode_shows_per_domain_output(self, runner, mock_api_key, mock_profiles_response, tmp_path):
        """
        Verify that with concurrency=1, sequential mode is used.

        Sequential mode shows per-domain output like "Added domain.com as active"
        while parallel mode shows a summary format.
        """
        domains_file = tmp_path / "domains.txt"
        domains_file.write_text("d1.com\nd2.com\nd3.com\n")

        with rm.Mocker() as m:
            m.get(f"{API_BASE}profiles", json=mock_profiles_response)
            m.post(
                f"{API_BASE}profiles/abc1234/denylist",
                json={"id": "test", "active": True},
            )

            result = runner.invoke(
                cli,
                [
                    "--concurrency",
                    "1",
                    "denylist",
                    "import",
                    "abc1234",
                    str(domains_file),
                ],
            )

            assert result.exit_code == 0
            # Sequential mode shows per-domain messages
            assert "Added d1.com" in result.output
            assert "Added d2.com" in result.output
            assert "Added d3.com" in result.output
            # Should NOT show parallel summary format
            assert "Completed:" not in result.output

    def test_concurrency_respects_max_limit(self, runner):
        """Concurrency option should reject values > 20."""
        result = runner.invoke(cli, ["--concurrency", "21", "profile-list"])

        assert result.exit_code != 0
        assert "21" in result.output or "range" in result.output.lower()

    def test_concurrency_respects_min_limit(self, runner):
        """Concurrency option should reject values < 1."""
        result = runner.invoke(cli, ["--concurrency", "0", "profile-list"])

        assert result.exit_code != 0
