"""Unit tests for configuration handling."""

import json
import os
import stat
import pytest

from nextdnsctl.config import save_api_key, load_api_key, ENV_VAR_NAME


class TestSaveApiKey:
    """Tests for save_api_key function."""

    def test_creates_config_file(self, tmp_path, monkeypatch):
        """Verify save_api_key creates the config file."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"

        monkeypatch.setattr("nextdnsctl.config.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))

        save_api_key("test-key-123")

        assert config_file.exists()
        with open(config_file) as f:
            data = json.load(f)
        assert data["api_key"] == "test-key-123"

    def test_file_permissions_are_secure(self, tmp_path, monkeypatch):
        """Verify config file has 600 permissions (owner read/write only)."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"

        monkeypatch.setattr("nextdnsctl.config.CONFIG_DIR", str(config_dir))
        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))

        save_api_key("test-key-123")

        file_mode = os.stat(config_file).st_mode
        # Check that only owner has read/write (600 = S_IRUSR | S_IWUSR)
        assert file_mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR


class TestLoadApiKey:
    """Tests for load_api_key function."""

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        """Environment variable should override config file."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"
        config_dir.mkdir()

        # Create config file with one key
        with open(config_file, "w") as f:
            json.dump({"api_key": "file-key"}, f)

        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))
        monkeypatch.setenv(ENV_VAR_NAME, "env-key")

        assert load_api_key() == "env-key"

    def test_falls_back_to_config_file(self, tmp_path, monkeypatch):
        """Should use config file when env var is not set."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"
        config_dir.mkdir()

        with open(config_file, "w") as f:
            json.dump({"api_key": "file-key"}, f)

        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)

        assert load_api_key() == "file-key"

    def test_raises_when_no_config_exists(self, tmp_path, monkeypatch):
        """Should raise ValueError when no API key is found."""
        config_file = tmp_path / "nonexistent" / "config.json"

        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)

        with pytest.raises(ValueError, match="No API key found"):
            load_api_key()

    def test_raises_when_config_missing_api_key(self, tmp_path, monkeypatch):
        """Should raise ValueError when config file lacks api_key."""
        config_dir = tmp_path / ".nextdnsctl"
        config_file = config_dir / "config.json"
        config_dir.mkdir()

        with open(config_file, "w") as f:
            json.dump({"other_key": "value"}, f)

        monkeypatch.setattr("nextdnsctl.config.CONFIG_FILE", str(config_file))
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)

        with pytest.raises(ValueError, match="Invalid config file"):
            load_api_key()
