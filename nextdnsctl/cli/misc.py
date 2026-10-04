"""auth, profile, catalog, rewrites, logs, why."""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any, Optional

import click

from .. import auth as auth_mod
from .. import cli as cli_pkg
from ..client import APIError
from ..config import normalize_rewrite_content, normalize_rewrite_name
from ..domains import InvalidDomainError, domain_from_argument
from ..model import canonicalize
from ..planner import Resolved
from . import State, cli, pass_state
from .declarative import confirm, run_plans
from .lists import add_entries, change_list, report_skipped
from .output import emit_json, info, render_plan, warn

# ── auth ───────────────────────────────────────────────────────────────────


@cli.command()
@click.argument("action", required=False, metavar="[login|logout|status]")
@pass_state
def auth(state: State, action: Optional[str]) -> None:
    """Store, remove or check the API key (find it at https://my.nextdns.io/account).

    'login' reads the key from a hidden prompt, or from stdin when piped:
    pbpaste | nextdnsctl auth login
    """
    if action == "logout":
        removed = auth_mod.delete_api_key()
        info(f"Removed {', '.join(removed)}" if removed else "No stored API key.")
        if auth_mod.find_api_key():
            warn(f"{auth_mod.ENV_VAR} is still set in your environment")
        return
    if action == "status":
        source = auth_mod.find_api_key()
        if source is None:
            raise click.ClickException(f"No API key. Run 'nextdnsctl auth login' or set {auth_mod.ENV_VAR}.")
        profiles = cli_pkg.make_client(source.key, state.timeout).list_profiles()
        info(f"API key from {source.origin} works; it can access {len(profiles)} profile(s).")
        return
    if action in (None, "login"):
        if sys.stdin.isatty():
            key = click.prompt("NextDNS API key", hide_input=True, err=True)
        else:
            key = sys.stdin.readline()
    else:
        warn(
            "passing the API key as an argument stores it in your shell history; "
            "use 'nextdnsctl auth login' to be prompted instead"
        )
        key = action
    key = key.strip()
    if not key:
        raise click.ClickException("No API key provided.")
    try:
        profiles = cli_pkg.make_client(key, state.timeout).list_profiles()
    except APIError as e:
        raise click.ClickException(f"NextDNS rejected this key ({e.describe()}); nothing was saved.")
    path = auth_mod.save_api_key(key)
    info(f"Saved to {path}. The key can access {len(profiles)} profile(s).")


# ── profiles ───────────────────────────────────────────────────────────────


@cli.group()
def profile() -> None:
    """List, create and delete profiles."""


@profile.command("list")
@pass_state
def profile_list(state: State) -> None:
    """List profiles."""
    profiles = state.planner.profiles()
    if state.json:
        emit_json([{"id": p["id"], "name": p["name"]} for p in profiles])
        return
    for p in profiles:
        click.echo(f"{p['id']}  {p['name']}")
    if not profiles:
        info("No profiles.")


@cli.command("profile-list", hidden=True)
@click.pass_context
def profile_list_legacy(ctx: click.Context) -> None:
    warn("'profile-list' is deprecated; use 'profile list'")
    ctx.invoke(profile_list)


@profile.command("create")
@click.argument("name")
@pass_state
def profile_create(state: State, name: str) -> None:
    """Create an empty profile."""
    if state.dry_run:
        info(f"Dry run: would create profile {name!r}")
        return
    created = state.client.create_profile(name)
    if state.json:
        emit_json({"id": created["id"], "name": name})
    else:
        click.echo(created["id"])
        info(f"Created profile {name!r}. Note that NextDNS creates profiles with logging disabled.")


@profile.command("delete")
@click.argument("name_or_id")
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
@pass_state
def profile_delete(state: State, name_or_id: str, yes: bool) -> None:
    """Delete a profile. This cannot be undone."""
    found = state.planner.require_profile(name_or_id)
    if state.dry_run:
        info(f"Dry run: would delete profile {found['name']!r} ({found['id']})")
        return
    if not yes:
        if not sys.stdin.isatty():
            raise click.UsageError("Not asking for confirmation without a terminal; pass --yes.")
        click.confirm(f"Delete profile {found['name']!r} ({found['id']})? This cannot be undone.", abort=True, err=True)
    state.client.delete_profile(found["id"])
    info(f"Deleted profile {found['name']!r} ({found['id']}).")


# ── catalog ────────────────────────────────────────────────────────────────

CATALOGS = {
    "blocklists": "privacy/blocklists",
    "natives": "privacy/natives",
    "services": "parentalControl/services",
    "categories": "parentalControl/categories",
    "tlds": "security/tlds",
}


@cli.command()
@click.argument("kind", type=click.Choice(sorted(CATALOGS)))
@pass_state
def catalog(state: State, kind: str) -> None:
    """List the valid ids for blocklists, natives, services, categories or tlds."""
    entries = state.client.catalog(CATALOGS[kind])
    if state.json:
        emit_json(entries)
        return
    for entry in entries:
        details = []
        if entry.get("name"):
            details.append(str(entry["name"]))
        if entry.get("entries"):
            details.append(f"{entry['entries']:,} domains")
        if entry.get("website"):
            details.append(str(entry["website"]))
        click.echo(entry["id"] + (f"  ({', '.join(details)})" if details else ""))


# ── rewrites ───────────────────────────────────────────────────────────────


@cli.group()
def rewrites() -> None:
    """Quick edits to DNS rewrites (name → IP address or domain)."""


def _change_rewrites(state: State, build, yes: bool = True) -> None:
    planner = state.planner
    found = planner.require_profile(state.require_profile())
    api_profile = planner.live(found["id"])
    live = canonicalize(api_profile).get("rewrites") or []
    plan = planner.plan_against(found["name"], found["id"], Resolved(overlay={"rewrites": build(live)}), api_profile)
    if not plan.has_changes:
        info("No changes needed.")
        return
    if not state.json:
        for line in render_plan(plan, state.verbose):
            click.echo(line)
    if state.dry_run:
        info("Dry run: nothing was changed.")
        return
    confirm([plan], yes or plan.removals <= 1)
    run_plans(state, [plan])


@rewrites.command("list")
@pass_state
def rewrites_list(state: State) -> None:
    """List rewrites."""
    found = state.planner.require_profile(state.require_profile())
    items = state.client.get_items(found["id"], "rewrites")
    if state.json:
        emit_json([{"name": r["name"], "content": r["content"], "type": r.get("type")} for r in items])
        return
    for r in items:
        click.echo(f"{r['name']} → {r['content']} ({r.get('type', '?')})")


@rewrites.command("add")
@click.argument("name")
@click.argument("content")
@pass_state
def rewrites_add(state: State, name: str, content: str) -> None:
    """Add a rewrite: CONTENT is an IPv4/IPv6 address or a domain (CNAME)."""
    try:
        entry = {"name": normalize_rewrite_name(name), "content": normalize_rewrite_content(content)}
    except InvalidDomainError as e:
        raise click.ClickException(str(e))
    _change_rewrites(state, lambda live: live + ([] if entry in live else [entry]))


@rewrites.command("remove")
@click.argument("name")
@click.argument("content", required=False)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation when removing several.")
@pass_state
def rewrites_remove(state: State, name: str, content: Optional[str], yes: bool) -> None:
    """Remove rewrites for NAME (only the one pointing to CONTENT, if given)."""
    try:
        wanted_name = normalize_rewrite_name(name)
        wanted_content = normalize_rewrite_content(content) if content else None
    except InvalidDomainError as e:
        raise click.ClickException(str(e))

    def build(live: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            r
            for r in live
            if not (r["name"] == wanted_name and (wanted_content is None or r["content"] == wanted_content))
        ]

    _change_rewrites(state, build, yes=yes)


# ── logs / why ─────────────────────────────────────────────────────────────

FOLLOW_INTERVAL = 5.0


def _logging_enabled(state: State, profile_id: str) -> bool:
    settings = state.client.get_profile(profile_id).get("settings") or {}
    return bool((settings.get("logs") or {}).get("enabled"))


def _log_line(entry: dict[str, Any]) -> str:
    when = entry.get("timestamp", "")
    try:
        when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        pass
    reasons = ", ".join(r.get("name") or r.get("id", "") for r in entry.get("reasons") or [])
    device = (entry.get("device") or {}).get("name") or entry.get("clientIp") or ""
    status = entry.get("status", "")
    parts = [when, f"{status:<8}", entry.get("domain", "")]
    if reasons:
        parts.append(f"({reasons})")
    if device:
        parts.append(f"[{device}]")
    return "  ".join(parts)


def _since(value: Optional[str]) -> Optional[str]:
    """`1h` → `-1h`, the relative form NextDNS accepts for `from`."""
    if value is None:
        return None
    value = value.strip()
    if value[:1] in "-+" or not value[:1].isdigit() or value.isdigit():
        return value
    return "-" + value


@cli.command()
@click.option("--blocked", is_flag=True, help="Only blocked queries.")
@click.option("--search", help="Only domains containing this text.")
@click.option("--since", help="How far back, e.g. 30m, 6h, 7d.")
@click.option(
    "-n",
    "--limit",
    type=click.IntRange(1, 10000),
    default=50,
    show_default=True,
    help="Number of entries (ignored with --follow).",
)
@click.option("--follow", is_flag=True, help="Keep printing new queries as they arrive.")
@pass_state
def logs(state: State, blocked: bool, search: Optional[str], since: Optional[str], limit: int, follow: bool) -> None:
    """Show recent DNS queries."""
    found = state.planner.require_profile(state.require_profile())
    if not _logging_enabled(state, found["id"]):
        warn("logging is disabled for this profile, so there are no logs to show")
    params = {"status": "blocked" if blocked else None, "search": search, "from": _since(since)}
    if not follow:
        entries = []
        for entry in state.client.iter_logs(found["id"], **params, limit=min(max(limit, 10), 1000)):
            entries.append(entry)
            if len(entries) >= limit:
                break
        if state.json:
            emit_json(entries)
        else:
            for entry in reversed(entries):
                click.echo(_log_line(entry))
        return
    # Poll /logs rather than using the stream, which drops events (A14).
    seen: set[tuple] = set()
    first = True
    try:
        while True:
            page = list(_first_page(state, found["id"], params))
            fresh = [e for e in page if _log_key(e) not in seen]
            for entry in reversed(fresh if not first else fresh[:limit]):
                click.echo(emit_line(state, entry))
            seen.update(_log_key(e) for e in page)
            first = False
            time.sleep(FOLLOW_INTERVAL)
    except KeyboardInterrupt:
        return


def _first_page(state: State, profile_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    response = state.client.request(
        "GET", f"profiles/{profile_id}/logs", params={**{k: v for k, v in params.items() if v}, "limit": 100}
    )
    return (response or {}).get("data") or []


def _log_key(entry: dict[str, Any]) -> tuple:
    return (entry.get("timestamp"), entry.get("domain"), entry.get("clientIp"), entry.get("status"))


def emit_line(state: State, entry: dict[str, Any]) -> str:
    if state.json:
        return json.dumps(entry, ensure_ascii=False)
    return _log_line(entry)


@cli.command()
@click.argument("domain")
@click.option("--since", default="7d", show_default=True, help="How far back to look.")
@click.option("--allow", is_flag=True, help="Add the domain to the allowlist.")
@pass_state
def why(state: State, domain: str, since: str, allow: bool) -> None:
    """Explain why DOMAIN was blocked, from the query logs."""
    try:
        name = domain_from_argument(domain)
    except InvalidDomainError as e:
        raise click.ClickException(str(e))
    found = state.planner.require_profile(state.require_profile())
    if not _logging_enabled(state, found["id"]):
        raise click.ClickException(
            "logging is disabled for this profile, so there's no record of what was blocked. "
            "Enable it (settings.logs.enabled) and try again after the domain is queried."
        )
    reasons: Counter[str] = Counter()
    last_seen: dict[str, str] = {}
    domains: dict[str, set[str]] = {}
    for count, entry in enumerate(
        state.client.iter_logs(found["id"], status="blocked", search=name, limit=1000, **{"from": _since(since)})
    ):
        if count >= 5000:
            break
        queried = entry.get("domain", "")
        if queried != name and not queried.endswith("." + name):
            continue
        for reason in entry.get("reasons") or [{"id": "unknown", "name": "unknown"}]:
            label = reason.get("name") or reason.get("id", "unknown")
            if reason.get("id") and reason.get("id") != label:
                label += f" [{reason['id']}]"
            reasons[label] += 1
            last_seen.setdefault(label, entry.get("timestamp", ""))
            domains.setdefault(label, set()).add(queried)

    if state.json:
        emit_json(
            [
                {"reason": r, "count": c, "last_seen": last_seen[r], "domains": sorted(domains[r])}
                for r, c in reasons.most_common()
            ]
        )
    elif not reasons:
        click.echo(f"No blocked queries for {name} (or its subdomains) in the last {since}.")
    else:
        click.echo(f"{name} was blocked by:")
        for reason, count in reasons.most_common():
            examples = ", ".join(sorted(domains[reason])[:3])
            click.echo(f"  {reason}: {count} quer{'y' if count == 1 else 'ies'}, last {last_seen[reason]} ({examples})")
    if allow:
        build, report = add_entries([name], True, True)
        change_list(state, found["id"], "allowlist", build)
        report_skipped(report)
