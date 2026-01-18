import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    """Provides a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_api_key(monkeypatch):
    """Sets a fake API key via environment variable."""
    monkeypatch.setenv("NEXTDNS_API_KEY", "fake-key-123")


@pytest.fixture
def mock_profiles_response():
    """Standard mock response for the profiles endpoint."""
    return {
        "data": [
            {"id": "abc1234", "name": "My Profile"},
            {"id": "xyz9876", "name": "Kids Profile"},
        ]
    }


@pytest.fixture
def mock_denylist_response():
    """Standard mock response for a denylist."""
    return {
        "data": [
            {"id": "bad-domain.com", "active": True},
            {"id": "inactive-domain.com", "active": False},
            {"id": "another-bad.net", "active": True},
        ]
    }


@pytest.fixture
def mock_allowlist_response():
    """Standard mock response for an allowlist."""
    return {
        "data": [
            {"id": "good-domain.com", "active": True},
            {"id": "trusted-site.org", "active": True},
        ]
    }


@pytest.fixture
def mock_empty_list_response():
    """Mock response for an empty list."""
    return {"data": []}
