"""Round trips against the real NextDNS API (opt-in: `pytest -m live`, needs NEXTDNS_API_KEY).

Creates a temporary profile, never touches existing ones, and deletes it afterwards.
Run before releases: the API facts the engine depends on are observations, not guarantees.
"""

import secrets
import textwrap

import pytest

from nextdnsctl.client import APIError, Client
from nextdnsctl.config import parse
from nextdnsctl.executor import Executor
from nextdnsctl.planner import Planner
from nextdnsctl.pull import document

from .conftest import LIVE_API_KEY

pytestmark = pytest.mark.live


@pytest.fixture
def live_client():
    key = LIVE_API_KEY
    if not key:
        pytest.skip("NEXTDNS_API_KEY not set")
    return Client(key)


@pytest.fixture
def profile_name(live_client):
    name = f"nextdnsctl-live-{secrets.token_hex(3)}"
    yield name
    for profile in live_client.list_profiles():
        if profile["name"] == name:
            live_client.delete_profile(profile["id"])


def test_round_trip(live_client, profile_name, tmp_path):
    (tmp_path / "hosts.txt").write_text("127.0.0.1 localhost\n0.0.0.0 ads.example-live.com\n")
    text = textwrap.dedent(
        f"""
        version: 1
        profiles:
          {profile_name}:
            denylist:
              domains: [blocked.example-live.com, {{domain: off.example-live.com, active: false}}]
              sources: [hosts.txt]
            allowlist: [allowed.example-live.com]
            security: {{nrd: true, tlds: [zip]}}
            privacy: {{blocklists: [nextdns-recommended], natives: [apple]}}
            parentalControl: {{services: [tiktok], categories: [gambling]}}
            settings: {{logs: {{enabled: true, retention: 1d}}}}
            rewrites:
              - {{name: nas.lan, content: 192.168.1.10}}
              - {{name: alias.lan, content: nas.lan}}
        """
    )
    file = str(tmp_path / "nextdns.yaml")
    planner = Planner(live_client)
    executor = Executor(live_client, planner)

    # Create and converge.
    plan = planner.plan(parse(text, file).profiles[0])
    assert plan.create
    result = executor.apply(plan)
    assert result.ok, (result.failures, result.aborted, result.drift)
    planner.forget_profiles()
    assert not planner.plan(parse(text, file).profiles[0]).has_changes

    # Pull reproduces the same state.
    profile_id = result.profile_id
    pulled = document([live_client.get_profile(profile_id)])
    assert "updateToken" not in pulled
    assert not planner.plan(parse(pulled, file).profiles[0]).has_changes

    # Change things: remove a rewrite, flip an entry, drop a blocklist.
    changed = (
        text.replace("      - {name: alias.lan, content: nas.lan}\n", "")
        .replace("{domain: off.example-live.com, active: false}", "off.example-live.com")
        .replace("blocklists: [nextdns-recommended]", "blocklists: []")
    )
    plan = planner.plan(parse(changed, file).profiles[0])
    result = executor.apply(plan)
    assert result.ok, (result.failures, result.aborted, result.drift)
    live = live_client.get_profile(profile_id)
    assert [r["name"] for r in live["rewrites"]] == ["nas.lan"]
    assert {"id": "off.example-live.com", "active": True} in live["denylist"]
    assert live["privacy"]["blocklists"] == []


def test_api_facts(live_client, profile_name):
    """Re-check the behaviours the design depends on (A3, A5, A13)."""
    profile = live_client.create_profile(profile_name)
    pid = profile["id"]
    assert live_client.get_profile(pid)["settings"]["logs"]["enabled"] is False  # A13

    # A5: PUT with a duplicate wipes the list; PATCH rejects it safely.
    live_client.patch_profile(pid, {"denylist": [{"id": "keep.example-live.com"}]})
    with pytest.raises(APIError) as info:
        live_client.patch_profile(pid, {"denylist": [{"id": "x.example-live.com"}, {"id": "x.example-live.com"}]})
    assert info.value.has_code("duplicate")
    assert [e["id"] for e in live_client.get_items(pid, "denylist")] == ["keep.example-live.com"]

    # A3: just under the body limit is accepted.
    big = [{"id": f"q{i}.example-test.com"} for i in range(2240)]
    live_client.patch_profile(pid, {"denylist": big})
    assert len(live_client.get_items(pid, "denylist")) == 2240

    # The catalog endpoints exist and are public (A15).
    assert any(b["id"] == "nextdns-recommended" for b in live_client.catalog("privacy/blocklists"))
