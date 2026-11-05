"""Round trips against the real NextDNS API (opt-in: `pytest -m live`, needs NEXTDNS_API_KEY).

Creates a temporary profile, never touches existing ones, and deletes it afterwards.
Run before releases: the API facts the engine depends on are observations, not guarantees.
"""

import secrets
import textwrap

import pytest

from nextdnsctl.client import APIError, Client, encode_body
from nextdnsctl.config import parse
from nextdnsctl.executor import Executor
from nextdnsctl.planner import SAFE_BODY_BYTES, Planner
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


def _names(prefix, count, pad=""):
    return [f"{prefix}{i}{pad}.example-live.com" for i in range(count)]


def _list_file(**lists):
    text = "version: 1\nprofiles:\n  {name}:\n"
    for section, names in lists.items():
        text += f"    {section}:\n" + "".join(f"      - {d}\n" for d in names)
    return text


def _ids(client, pid, section="denylist"):
    return sorted(e["id"] for e in client.get_items(pid, section))


def test_write_strategies(live_client, profile_name, monkeypatch):
    """Multiple PATCHes, single-entry writes, large-list splits and gaps, with a small request limit."""
    from nextdnsctl import planner as planner_mod

    monkeypatch.setattr(planner_mod, "SAFE_BODY_BYTES", 2048)  # ~65 entries per request
    pid = live_client.create_profile(profile_name)["id"]
    planner = Planner(live_client)

    def plan(allow_gaps=False, **lists):
        planner.allow_gaps = allow_gaps
        return planner.plan(parse(_list_file(**lists).format(name=profile_name)).profiles[0])

    def apply(p):
        result = Executor(live_client, planner).apply(p)
        assert result.ok, (result.failures, result.aborted, result.drift)
        return result

    # Two lists that don't fit in one request together: one PATCH each.
    deny, allow = _names("k", 50), _names("a", 50)
    p = plan(denylist=deny, allowlist=allow)
    assert len(p.patches) == 2 and not p.ops
    apply(p)
    assert _ids(live_client, pid) == sorted(deny) and _ids(live_client, pid, "allowlist") == sorted(allow)

    # One change: the entry's own endpoint.
    p = plan(denylist=deny + ["single.example-live.com"])
    assert not p.patches and [op.method for op in p.ops] == ["POST"]
    apply(p)
    deny = deny + ["single.example-live.com"]

    # Grow past one request: every kept entry in the PATCH, the rest added one by one.
    grown = deny[:45] + _names("n", 100)
    p = plan(denylist=grown)
    assert p.partial_sections == ["denylist"] and p.gap == 0
    assert p.writes < 106  # 6 removals + 100 adds one by one
    apply(p)
    assert _ids(live_client, pid) == sorted(grown)

    # Shrink a list whose kept entries don't fit: entry by entry, unless gaps are allowed.
    shrunk = grown[:70]
    p = plan(denylist=shrunk)
    assert p.incremental_sections == ["denylist"] and p.gap_hints
    p = plan(allow_gaps=True, denylist=shrunk)
    assert p.partial_sections == ["denylist"] and 0 < p.gap < 10 and p.writes == 1 + p.gap
    apply(p)
    assert _ids(live_client, pid) == sorted(shrunk)


def test_changed_list_is_replanned(live_client, profile_name):
    pid = live_client.create_profile(profile_name)["id"]
    live_client.patch_profile(pid, {"denylist": [{"id": "old.example-live.com"}]})
    planner = Planner(live_client)
    text = _list_file(denylist=["a.example-live.com", "b.example-live.com"]).format(name=profile_name)
    p = planner.plan(parse(text).profiles[0])
    assert p.patches

    live_client.add_item(pid, "denylist", {"id": "concurrent.example-live.com"})
    p.read_at -= 60  # as if a confirmation prompt had been open for a minute
    seen = []
    result = Executor(live_client, planner, on_rebase=lambda old, new: seen.append(new) and False).apply(p)
    assert result.aborted and seen
    removed = sorted(e["id"] for c in seen[0].diff.arrays for e in c.removed)
    assert removed == ["concurrent.example-live.com", "old.example-live.com"]
    assert "concurrent.example-live.com" in _ids(live_client, pid)  # nothing was written

    result = Executor(live_client, planner).apply(p)  # accept the new plan
    assert result.ok and result.rebased, (result.failures, result.drift)
    assert _ids(live_client, pid) == ["a.example-live.com", "b.example-live.com"]


def test_large_list_at_the_real_size_limit(live_client, profile_name):
    """The split PATCH is as large as the planner allows, and NextDNS accepts it (A3)."""
    pid = live_client.create_profile(profile_name)["id"]
    domains = _names("long-domain-name-for-the-size-limit-", 1240, pad="-padding-padding")  # ~1 197 fit
    planner = Planner(live_client)
    p = planner.plan(parse(_list_file(denylist=domains).format(name=profile_name)).profiles[0])
    assert p.partial_sections == ["denylist"]
    size = len(encode_body(p.patches[0].body))
    assert SAFE_BODY_BYTES - 200 < size <= SAFE_BODY_BYTES
    assert 0 < len(p.ops) < 60
    result = Executor(live_client, planner).apply(p)
    assert result.ok, (result.failures, result.aborted, result.drift)
    assert _ids(live_client, pid) == sorted(domains)
