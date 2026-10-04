import os

import pytest
from click.testing import CliRunner

from nextdnsctl.client import Client

from .fake_api import API_KEY, FakeClock, FakeNextDNS


# Captured before the isolation fixture below clears the environment; only live tests use it.
LIVE_API_KEY = os.environ.get("NEXTDNS_API_KEY")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(clock):
    return FakeNextDNS(clock)


@pytest.fixture
def client(fake, clock):
    return Client(API_KEY, session=fake, clock=clock, sleep=clock.sleep)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read or write the real ~/.config or ~/.nextdnsctl in tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("NEXTDNS_API_KEY", raising=False)
    monkeypatch.delenv("NEXTDNS_PROFILE", raising=False)
