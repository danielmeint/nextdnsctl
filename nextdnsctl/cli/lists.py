"""denylist / allowlist: quick edits without a profile file."""

from __future__ import annotations

from typing import Any, Callable, Optional

import click

from .. import sources as sources_mod
from ..domains import InvalidDomainError, domain_from_argument
from ..model import canonicalize
from ..planner import PlanError, Resolved
from . import State, cli, pass_state
from .declarative import confirm, run_plans
from .output import emit_json, info, render_plan, warn

Entries = list[dict[str, Any]]


def profile_and_args(state: State, args: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """The profile from -p, or (deprecated, as in 1.x) from the first argument."""
    if state.profile:
        return state.profile, args
    if args:
        try:
            found = state.planner.find_profile(args[0])
        except PlanError:
            found = None
        if found is not None:
            warn(
                "giving the profile as the first argument is deprecated and will be removed in 2.1; "
                f"use -p {args[0]!r} or set NEXTDNS_PROFILE"
            )
            return args[0], args[1:]
    raise click.UsageError("No profile given. Use -p/--profile NAME or set NEXTDNS_PROFILE.")


def normalize_arguments(values: tuple[str, ...]) -> list[str]:
    domains, errors = [], []
    for value in values:
        try:
            domains.append(domain_from_argument(value))
        except InvalidDomainError as e:
            errors.append(f"  {value}: {e}")
    if errors:
        raise click.ClickException("invalid domain(s):\n" + "\n".join(errors))
    return list(dict.fromkeys(domains))


def change_list(
    state: State,
    profile_name: str,
    list_name: str,
    build: Callable[[Entries], Entries],
    needs_confirmation: bool = False,
    yes: bool = False,
) -> None:
    """Plan `build(live entries)` as the new list and apply it, like a one-section apply."""
    planner = state.planner
    profile = planner.require_profile(profile_name)
    api_profile = planner.live(profile["id"])
    live_entries = canonicalize(api_profile).get(list_name) or []
    resolved = Resolved(overlay={list_name: build(live_entries)})
    plan = planner.plan_against(profile["name"], profile["id"], resolved, api_profile)
    if not plan.has_changes:
        info("No changes needed.")
        return
    if not state.json:
        for line in render_plan(plan, state.verbose):
            click.echo(line)
    if state.dry_run:
        info("Dry run: nothing was changed.")
        return
    if needs_confirmation:
        confirm([plan], yes)
    run_plans(state, [plan])
    if not state.json:
        info(f"View at: https://my.nextdns.io/{profile['id']}/{list_name}")


def add_entries(
    domains: list[str], active: bool, update_existing: bool
) -> tuple[Callable[[Entries], Entries], dict[str, list[str]]]:
    """A builder adding `domains`, plus a report of what was skipped (filled in when it runs)."""
    report: dict[str, list[str]] = {"present": [], "mismatched": []}

    def build(live: Entries) -> Entries:
        by_id = {e["id"]: dict(e) for e in live}
        for domain in domains:
            if domain not in by_id:
                by_id[domain] = {"id": domain, "active": active}
            elif by_id[domain]["active"] == active:
                report["present"].append(domain)
            elif update_existing:
                by_id[domain]["active"] = active
            else:
                report["mismatched"].append(domain)
        return list(by_id.values())

    return build, report


def report_skipped(report: dict[str, list[str]]) -> None:
    if report.get("present"):
        info(f"Already present: {len(report['present'])}")
    if report.get("mismatched"):
        info(
            f"Present with the other active/inactive state, left unchanged: {len(report['mismatched'])} "
            "(use --update-existing to change them)"
        )
    if report.get("missing"):
        info(f"Not in the list: {len(report['missing'])}")


def filter_entries(entries: Entries, active_only: bool, inactive_only: bool) -> Entries:
    if active_only and inactive_only:
        raise click.UsageError("--active-only and --inactive-only are mutually exclusive")
    if active_only:
        return [e for e in entries if e.get("active", True)]
    if inactive_only:
        return [e for e in entries if not e.get("active", True)]
    return entries


def make_list_group(list_name: str, verb: str) -> click.Group:
    @cli.group(list_name)
    def group() -> None:
        pass

    group.help = f"Quick edits to the {list_name} ({verb}) without a profile file."

    @group.command("list")
    @click.argument("args", nargs=-1, metavar="")
    @click.option("--active-only", is_flag=True, help="Only active entries.")
    @click.option("--inactive-only", is_flag=True, help="Only inactive entries.")
    @pass_state
    def list_cmd(state: State, args: tuple[str, ...], active_only: bool, inactive_only: bool) -> None:
        """List the entries."""
        profile_name, rest = profile_and_args(state, args)
        if rest:
            raise click.UsageError(f"unexpected argument {rest[0]!r}")
        profile = state.planner.require_profile(profile_name)
        entries = filter_entries(state.client.get_items(profile["id"], list_name), active_only, inactive_only)
        if state.json:
            emit_json([{"domain": e["id"], "active": e.get("active", True)} for e in entries])
            return
        for entry in entries:
            click.echo(entry["id"] + ("" if entry.get("active", True) else " (inactive)"))
        info(f"Total: {len(entries)}")

    @group.command("add")
    @click.argument("args", nargs=-1, required=True, metavar="DOMAIN...")
    @click.option("--inactive", is_flag=True, help=f"Add as inactive (listed but not {verb}).")
    @click.option("--update-existing", is_flag=True, help="Also change the active state of existing entries.")
    @pass_state
    def add_cmd(state: State, args: tuple[str, ...], inactive: bool, update_existing: bool) -> None:
        """Add domains (a URL is reduced to its host)."""
        profile_name, rest = profile_and_args(state, args)
        if not rest:
            raise click.UsageError("No domains given.")
        build, report = add_entries(normalize_arguments(rest), not inactive, update_existing)
        change_list(state, profile_name, list_name, build)
        report_skipped(report)

    @group.command("remove")
    @click.argument("args", nargs=-1, required=True, metavar="DOMAIN...")
    @pass_state
    def remove_cmd(state: State, args: tuple[str, ...]) -> None:
        """Remove domains."""
        profile_name, rest = profile_and_args(state, args)
        if not rest:
            raise click.UsageError("No domains given.")
        domains = set(normalize_arguments(rest))
        report: dict[str, list[str]] = {"missing": []}

        def build(live: Entries) -> Entries:
            present = {e["id"] for e in live}
            report["missing"] = sorted(domains - present)
            return [e for e in live if e["id"] not in domains]

        change_list(state, profile_name, list_name, build)
        report_skipped(report)

    @group.command("import")
    @click.argument("args", nargs=-1, required=True, metavar="SOURCE")
    @click.option(
        "--format",
        "fmt",
        type=click.Choice(sources_mod.FORMATS),
        default="auto",
        show_default=True,
        help="Source format.",
    )
    @click.option("--skip-invalid", is_flag=True, help="Skip invalid lines instead of failing.")
    @click.option("--inactive", is_flag=True, help=f"Add as inactive (listed but not {verb}).")
    @click.option("--update-existing", is_flag=True, help="Also change the active state of existing entries.")
    @pass_state
    def import_cmd(
        state: State, args: tuple[str, ...], fmt: str, skip_invalid: bool, inactive: bool, update_existing: bool
    ) -> None:
        """Add every domain from a file or URL (plain, hosts or adblock format)."""
        profile_name, rest = profile_and_args(state, args)
        if len(rest) != 1:
            raise click.UsageError("Give exactly one SOURCE (file path or URL).")
        result = sources_mod.load(rest[0], fmt)
        if result.format == "hosts":
            info("Note: a hosts entry blocks one exact name; in NextDNS it also covers its subdomains.")
        if result.errors:
            lines = [f"  line {e.line}: {e.reason}: {e.text}" for e in result.errors[:20]]
            if len(result.errors) > 20:
                lines.append(f"  … and {len(result.errors) - 20} more")
            message = f"{len(result.errors)} invalid line(s) in {rest[0]} (format: {result.format}):\n" + "\n".join(
                lines
            )
            if not skip_invalid:
                raise click.ClickException(message + "\nFix them, or pass --skip-invalid.")
            warn(message)
        domains = list(dict.fromkeys(d for d, _ in result.domains))
        if not domains:
            info("No domains found in the source.")
            return
        build, report = add_entries(domains, not inactive, update_existing)
        change_list(state, profile_name, list_name, build)
        report_skipped(report)

    @group.command("export")
    @click.argument("args", nargs=-1, metavar="[OUTPUT]")
    @click.option("--active-only", is_flag=True, help="Only active entries.")
    @click.option("--inactive-only", is_flag=True, help="Only inactive entries.")
    @pass_state
    def export_cmd(state: State, args: tuple[str, ...], active_only: bool, inactive_only: bool) -> None:
        """Write the domains to a file, one per line (stdout by default)."""
        profile_name, rest = profile_and_args(state, args)
        output: Optional[str] = rest[0] if rest else None
        profile = state.planner.require_profile(profile_name)
        entries = filter_entries(state.client.get_items(profile["id"], list_name), active_only, inactive_only)
        text = "".join(e["id"] + "\n" for e in entries)
        if output in (None, "-"):
            click.echo(text, nl=False)
        else:
            with open(output, "w", encoding="utf-8") as f:
                f.write(text)
            info(f"Exported {len(entries)} domains to {output}")

    @group.command("clear")
    @click.argument("args", nargs=-1, metavar="")
    @click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
    @pass_state
    def clear_cmd(state: State, args: tuple[str, ...], yes: bool) -> None:
        """Remove every entry."""
        profile_name, rest = profile_and_args(state, args)
        if rest:
            raise click.UsageError(f"unexpected argument {rest[0]!r}")
        change_list(state, profile_name, list_name, lambda live: [], needs_confirmation=True, yes=yes)

    return group


denylist = make_list_group("denylist", "blocked")
allowlist = make_list_group("allowlist", "allowed")
