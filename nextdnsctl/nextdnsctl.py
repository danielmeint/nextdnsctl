import atexit
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
)  # noqa: F401

import click
import requests

from . import __version__
from .config import save_api_key, load_api_key
from .api import (
    APIClient,
    set_client,
    clear_client,
    validate_domain,
    InvalidDomainError,
    DEFAULT_RETRIES,
    DEFAULT_DELAY,
    DEFAULT_TIMEOUT,
    RateLimitStillActiveError,
)

DEFAULT_CONCURRENCY = 5
INVALID_PREVIEW_LIMIT = 10


@dataclass(frozen=True)
class DomainAddPlan:
    """Planned changes for add/import operations."""

    to_add: list[str]
    to_update: list[str]
    already_present: list[str]
    skipped_mismatched: list[str]
    duplicate_input: list[str]


@dataclass(frozen=True)
class DomainRemovalPlan:
    """Planned changes for remove operations."""

    to_remove: list[str]
    missing: list[str]
    duplicate_input: list[str]


def _resolve_profile_id(ctx: click.Context, profile_identifier: str) -> str:
    """
    Resolve a profile identifier (ID or name) to a profile ID.

    If the identifier matches an existing profile ID, return it directly.
    Otherwise, search for a profile with a matching name.
    Caches the profiles list in ctx.obj to avoid repeated API calls.
    """
    client: APIClient = ctx.obj["client"]

    # Get or fetch profiles (cache in ctx.obj)
    if "profiles_cache" not in ctx.obj:
        try:
            ctx.obj["profiles_cache"] = client.get_profiles()
        except Exception as e:
            raise click.ClickException(f"Failed to fetch profiles: {e}")

    profiles = ctx.obj["profiles_cache"]

    # First, check if it's a direct ID match
    for profile in profiles:
        if profile.get("id") == profile_identifier:
            return profile_identifier

    # Otherwise, search by name (case-insensitive)
    matches = [p for p in profiles if p.get("name", "").lower() == profile_identifier.lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) > 1:
        ids = ", ".join(p["id"] for p in matches)
        raise click.ClickException(
            f"Profile name '{profile_identifier}' is ambiguous (matches {ids}). Use the profile ID instead."
        )

    # No match found
    available = ", ".join(f"'{p.get('name')}' ({p.get('id')})" for p in profiles)
    raise click.ClickException(f"Profile '{profile_identifier}' not found. " f"Available profiles: {available}")


def _fetch_domain_list(client: APIClient, profile_id: str, list_type: str) -> list[dict[str, Any]]:
    """Fetch a list, turning API failures into a clean CLI error instead of a traceback."""
    try:
        return client.get_domain_list(profile_id, list_type)
    except Exception as e:
        raise click.ClickException(f"Failed to fetch {list_type}: {e}")


def _validate_domains(domains: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    Validate a list of domains.

    Returns:
        Tuple of (valid_domains, invalid_domains)
    """
    valid = []
    invalid = []
    for domain in domains:
        try:
            validated = validate_domain(domain)
            valid.append(validated)
        except InvalidDomainError as e:
            invalid.append(str(e))
    return valid, invalid


def _dedupe_domains(domains: Sequence[str]) -> tuple[list[str], list[str]]:
    """Deduplicate domains while preserving first-seen order."""
    seen = set()
    unique = []
    duplicates = []
    for domain in domains:
        if domain in seen:
            duplicates.append(domain)
            continue
        seen.add(domain)
        unique.append(domain)
    return unique, duplicates


def _existing_domain_states(entries: Sequence[dict[str, Any]]) -> dict[str, bool]:
    """Map existing list entries to their active state."""
    states = {}
    for entry in entries:
        domain = entry.get("id")
        if not domain:
            continue
        states[str(domain).lower()] = bool(entry.get("active", True))
    return states


def _plan_domain_additions(
    domains: Sequence[str],
    existing_entries: Sequence[dict[str, Any]],
    desired_active: bool,
    update_existing: bool,
) -> DomainAddPlan:
    """Plan add/import work against the current remote list."""
    unique_domains, duplicate_input = _dedupe_domains(domains)
    existing_states = _existing_domain_states(existing_entries)

    to_add = []
    to_update = []
    already_present = []
    skipped_mismatched = []

    for domain in unique_domains:
        if domain not in existing_states:
            to_add.append(domain)
            continue

        if existing_states[domain] == desired_active:
            already_present.append(domain)
        elif update_existing:
            to_update.append(domain)
        else:
            skipped_mismatched.append(domain)

    return DomainAddPlan(
        to_add=to_add,
        to_update=to_update,
        already_present=already_present,
        skipped_mismatched=skipped_mismatched,
        duplicate_input=duplicate_input,
    )


def _plan_domain_removals(
    domains: Sequence[str],
    existing_entries: Sequence[dict[str, Any]],
) -> DomainRemovalPlan:
    """Plan remove work against the current remote list."""
    unique_domains, duplicate_input = _dedupe_domains(domains)
    existing_domains = set(_existing_domain_states(existing_entries))

    to_remove = []
    missing = []
    for domain in unique_domains:
        if domain in existing_domains:
            to_remove.append(domain)
        else:
            missing.append(domain)

    return DomainRemovalPlan(to_remove=to_remove, missing=missing, duplicate_input=duplicate_input)


def _past_tense(action_verb: str) -> str:
    """Return the past tense used in operation summaries."""
    return {
        "add": "added",
        "remove": "removed",
        "update": "updated",
        "process": "processed",
    }.get(action_verb, f"{action_verb}ed")


def _echo_plan_items(label: str, domains: Sequence[str], limit: int = 20) -> None:
    """Echo a short domain preview for dry-run plans."""
    if not domains:
        return

    click.echo(f"  {label}: {len(domains)}")
    for domain in domains[:limit]:
        click.echo(f"    - {domain}")
    remaining = len(domains) - limit
    if remaining > 0:
        click.echo(f"    ... {remaining} more")


def _echo_add_plan_summary(
    plan: DomainAddPlan,
    list_type: str,
    update_existing: bool,
    dry_run: bool,
) -> None:
    """Print a user-facing add/import plan summary."""
    prefix = "[DRY-RUN] " if dry_run else ""
    click.echo(f"{prefix}{list_type.capitalize()} plan:")

    if dry_run:
        _echo_plan_items("New domains to add", plan.to_add)
        _echo_plan_items("Existing domains to update", plan.to_update)
        _echo_plan_items("Already present", plan.already_present)
        _echo_plan_items("State mismatches skipped", plan.skipped_mismatched)
        _echo_plan_items("Duplicate input skipped", plan.duplicate_input)
    else:
        click.echo(f"  New domains to add: {len(plan.to_add)}")
        if plan.to_update:
            click.echo(f"  Existing domains to update: {len(plan.to_update)}")
        if plan.already_present:
            click.echo(f"  Already present: {len(plan.already_present)}")
        if plan.duplicate_input:
            click.echo(f"  Duplicate input skipped: {len(plan.duplicate_input)}")
        if plan.skipped_mismatched:
            click.echo(f"  State mismatches skipped: {len(plan.skipped_mismatched)}")
            if not update_existing:
                click.echo("  Use --update-existing to update active/inactive state.")

    if not plan.to_add and not plan.to_update:
        click.echo("  No changes needed.")


def _echo_removal_plan_summary(plan: DomainRemovalPlan, list_type: str, dry_run: bool) -> None:
    """Print a user-facing remove plan summary."""
    prefix = "[DRY-RUN] " if dry_run else ""
    click.echo(f"{prefix}{list_type.capitalize()} removal plan:")

    if dry_run:
        _echo_plan_items("Domains to remove", plan.to_remove)
        _echo_plan_items("Missing domains skipped", plan.missing)
        _echo_plan_items("Duplicate input skipped", plan.duplicate_input)
    else:
        click.echo(f"  Domains to remove: {len(plan.to_remove)}")
        if plan.missing:
            click.echo(f"  Missing domains skipped: {len(plan.missing)}")
        if plan.duplicate_input:
            click.echo(f"  Duplicate input skipped: {len(plan.duplicate_input)}")

    if not plan.to_remove:
        click.echo("  No changes needed.")


# Helper function to perform operations on a list of domains
def _perform_domain_operations(
    ctx: click.Context,
    domains_to_process: Sequence[str],
    operation_callable: Callable[[str], str],
    item_name_singular: str = "domain",
    action_verb: str = "process",
) -> bool:
    """
    Iterates over a list of items (e.g., domains) and performs an operation on each.
    Returns True if all non-critical operations were successful, False otherwise.
    Exits script if RateLimitStillActiveError is encountered.

    Supports parallel execution when concurrency > 1.
    Supports dry-run mode to show what would be done without making changes.
    """
    dry_run = ctx.obj.get("dry_run", False)
    concurrency = ctx.obj.get("concurrency", DEFAULT_CONCURRENCY)

    # Dry-run mode: just show what would be done
    if dry_run:
        return _perform_domain_operations_dry_run(domains_to_process, item_name_singular, action_verb)

    # Sequential mode (concurrency == 1): preserve original verbose behavior
    if concurrency == 1:
        return _perform_domain_operations_sequential(
            ctx, domains_to_process, operation_callable, item_name_singular, action_verb
        )

    # Parallel mode
    return _perform_domain_operations_parallel(
        ctx,
        domains_to_process,
        operation_callable,
        item_name_singular,
        action_verb,
        concurrency,
    )


def _perform_domain_operations_dry_run(
    domains_to_process: Sequence[str],
    item_name_singular: str,
    action_verb: str,
) -> bool:
    """Dry-run mode: show what would be done without making changes."""
    click.echo(f"[DRY-RUN] Would {action_verb} {len(domains_to_process)} {item_name_singular}(s):")
    for domain in domains_to_process:
        click.echo(f"  - {domain}")
    click.echo("\n[DRY-RUN] No changes made.", err=True)
    return True


def _perform_domain_operations_sequential(
    ctx: click.Context,
    domains_to_process: Sequence[str],
    operation_callable: Callable[[str], str],
    item_name_singular: str,
    action_verb: str,
) -> bool:
    """Sequential execution with verbose per-domain output (original behavior)."""
    all_successful = True
    failure_count = 0
    action_past_tense = _past_tense(action_verb)
    for item_value in domains_to_process:
        try:
            result = operation_callable(item_value)
            click.echo(result)
        except RateLimitStillActiveError as e:
            click.echo(
                f"\nCRITICAL ERROR: Domain '{item_value}' could not be {action_past_tense} "
                f"due to persistent rate limiting.",
                err=True,
            )
            click.echo(f"Detail: {e}", err=True)
            click.echo("Aborting further operations for this command.", err=True)
            ctx.exit(1)
        except Exception as e:
            all_successful = False
            failure_count += 1
            click.echo(
                f"Failed to {action_verb} {item_name_singular} '{item_value}': {e}",
                err=True,
            )
    if not all_successful and failure_count > 0:
        click.echo(
            f"\nWarning: {failure_count} {item_name_singular}(s) could not be {action_past_tense} "
            f"due to other errors.",
            err=True,
        )
    return all_successful


def _perform_domain_operations_parallel(
    ctx: click.Context,
    domains_to_process: Sequence[str],
    operation_callable: Callable[[str], str],
    item_name_singular: str,
    action_verb: str,
    concurrency: int,
) -> bool:
    """Parallel execution with progress bar and summary output."""
    rate_limit_hit = threading.Event()
    results = {"success": 0, "failed": 0, "skipped": 0}
    errors = []  # Collect errors to print after progress bar
    rate_limit_aborted = False

    total_domains = len(domains_to_process)
    domain_iterator = iter(domains_to_process)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}

        def submit_next() -> bool:
            if rate_limit_hit.is_set():
                return False
            try:
                domain = next(domain_iterator)
            except StopIteration:
                return False
            futures[executor.submit(operation_callable, domain)] = domain
            return True

        for _ in range(min(concurrency, total_domains)):
            submit_next()

        progress_bar: Any = click.progressbar(
            length=total_domains,
            label=f"Processing {item_name_singular}s",
            show_pos=True,
        )
        with progress_bar as bar:
            while futures:
                completed_futures, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed_futures:
                    domain = futures.pop(future)
                    try:
                        future.result()
                        results["success"] += 1
                    except RateLimitStillActiveError as e:
                        rate_limit_hit.set()
                        rate_limit_aborted = True
                        results["failed"] += 1
                        errors.append(f"CRITICAL: '{domain}' - persistent rate limiting: {e}")
                    except Exception as e:
                        results["failed"] += 1
                        errors.append(f"Failed to {action_verb} '{domain}': {e}")
                    bar.update(1)

                    if not rate_limit_hit.is_set():
                        submit_next()

            if rate_limit_hit.is_set():
                skipped = sum(1 for _ in domain_iterator)
                results["skipped"] += skipped
                if skipped:
                    bar.update(skipped)

    # Print any errors that occurred
    for error in errors:
        click.echo(error, err=True)

    # Print summary
    click.echo(
        f"\nCompleted: {results['success']}, "
        f"Failed: {results['failed']}, "
        f"Skipped: {results['skipped']} "
        f"(of {total_domains} total)"
    )

    if rate_limit_aborted:
        click.echo(
            "Operation aborted due to persistent rate limiting. "
            f"{results['skipped']} {item_name_singular}(s) were not attempted.",
            err=True,
        )
        ctx.exit(1)

    return results["failed"] == 0


@click.group()
@click.version_option(__version__)
@click.option(
    "--retry-attempts",
    type=int,
    default=DEFAULT_RETRIES,
    help=f"Number of retry attempts for API calls. Default: {DEFAULT_RETRIES}",
    show_default=True,
)
@click.option(
    "--retry-delay",
    type=float,
    default=DEFAULT_DELAY,
    help=f"Initial delay (in seconds) between retries. Default: {DEFAULT_DELAY}",
    show_default=True,
)
@click.option(
    "--timeout",
    type=float,
    default=DEFAULT_TIMEOUT,
    help=f"Request timeout (in seconds) for API calls. Default: {DEFAULT_TIMEOUT}",
    show_default=True,
)
@click.option(
    "--concurrency",
    type=click.IntRange(1, 20),
    default=DEFAULT_CONCURRENCY,
    help=f"Number of concurrent API requests. Default: {DEFAULT_CONCURRENCY}",
    show_default=True,
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be done without making changes",
)
@click.pass_context
def cli(ctx, retry_attempts, retry_delay, timeout, concurrency, dry_run):
    """nextdnsctl: A CLI tool for managing NextDNS profiles."""
    ctx.obj = {
        "retry_attempts": retry_attempts,
        "retry_delay": retry_delay,
        "timeout": timeout,
        "concurrency": concurrency,
        "dry_run": dry_run,
    }

    # Initialize API client once (except for auth command which doesn't need it)
    # The client will be created lazily on first API call if not set here
    if ctx.invoked_subcommand != "auth":
        try:
            api_key = load_api_key()
            client = APIClient(
                api_key,
                retries=retry_attempts,
                delay=retry_delay,
                timeout=timeout,
            )
            ctx.obj["client"] = client
            set_client(client)
            # Register cleanup on exit
            atexit.register(clear_client)
        except ValueError:
            # No API key configured - will fail later with helpful message
            # if a command actually needs it
            pass


@cli.command()
@click.argument("api_key", required=False)
def auth(api_key):
    """Save your NextDNS API key.

    Run without an argument to be prompted for the key (or pipe it on stdin), so it
    doesn't end up in your shell history.
    """
    if api_key:
        click.echo(
            "Warning: passing the API key as an argument stores it in your shell history. "
            "Run 'nextdnsctl auth' without an argument to be prompted instead.",
            err=True,
        )
    elif sys.stdin.isatty():
        api_key = click.prompt("NextDNS API key", hide_input=True)
    else:
        api_key = sys.stdin.readline()
    api_key = api_key.strip()
    if not api_key:
        raise click.ClickException("No API key provided.")
    try:
        save_api_key(api_key)
        # Verify it works by making a test call
        load_api_key()
        click.echo("API key saved successfully.")
    except Exception as e:
        click.echo(f"Error saving API key: {e}", err=True)
        raise click.Abort()


@cli.command("profile-list")
@click.pass_context
def profile_list(ctx):
    """List all NextDNS profiles."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    try:
        client: APIClient = ctx.obj["client"]
        profiles = client.get_profiles()
        if not profiles:
            click.echo("No profiles found.")
            return
        for profile in profiles:
            click.echo(f"{profile['id']}: {profile['name']}")
    except Exception as e:
        click.echo(f"Error fetching profiles: {e}", err=True)
        raise click.Abort()


def read_domains_from_source(source: str) -> Iterator[str]:
    """
    Read domains from a file or URL, yielding one domain per line.

    Handles:
    - Comment lines (starting with #)
    - Inline comments (e.g., "example.com # bad site")
    - Empty lines and whitespace
    - Streaming for memory efficiency with large files
    """
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source, stream=True, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if line:
                domain = _parse_domain_line(line)
                if domain:
                    yield domain
    else:
        with open(source, "r") as f:
            for line in f:
                domain = _parse_domain_line(line)
                if domain:
                    yield domain


def _parse_domain_line(line: str) -> Optional[str]:
    """Parse a single line, handling comments and whitespace."""
    # Strip inline comments (e.g., "example.com # bad site" -> "example.com")
    line = line.split("#")[0].strip()
    return line if line else None


def _execute_add_plan(
    ctx: click.Context,
    client: APIClient,
    profile_id: str,
    list_type: str,
    plan: DomainAddPlan,
    desired_active: bool,
    update_existing: bool,
) -> None:
    """Execute or dry-run an add/import delta plan."""
    dry_run = ctx.obj.get("dry_run", False)
    _echo_add_plan_summary(plan, list_type, update_existing, dry_run)

    if dry_run:
        click.echo("\n[DRY-RUN] No changes made.", err=True)
        return

    if not plan.to_add and not plan.to_update:
        return

    if plan.to_add:

        def add_operation(domain_name):
            return client.add_to_domain_list(
                profile_id,
                list_type,
                domain_name,
                active=desired_active,
            )

        success = _perform_domain_operations(
            ctx,
            plan.to_add,
            add_operation,
            item_name_singular="domain",
            action_verb="add",
        )
        if not success:
            ctx.exit(1)

    if plan.to_update:

        def update_operation(domain_name):
            return client.update_domain_list_entry(
                profile_id,
                list_type,
                domain_name,
                active=desired_active,
            )

        success = _perform_domain_operations(
            ctx,
            plan.to_update,
            update_operation,
            item_name_singular="domain",
            action_verb="update",
        )
        if not success:
            ctx.exit(1)


def _execute_removal_plan(
    ctx: click.Context,
    client: APIClient,
    profile_id: str,
    list_type: str,
    plan: DomainRemovalPlan,
) -> None:
    """Execute or dry-run a remove delta plan."""
    dry_run = ctx.obj.get("dry_run", False)
    _echo_removal_plan_summary(plan, list_type, dry_run)

    if dry_run:
        click.echo("\n[DRY-RUN] No changes made.", err=True)
        return

    if not plan.to_remove:
        return

    def operation(domain_name):
        return client.remove_from_domain_list(
            profile_id,
            list_type,
            domain_name,
        )

    success = _perform_domain_operations(
        ctx,
        plan.to_remove,
        operation,
        item_name_singular="domain",
        action_verb="remove",
    )
    if not success:
        ctx.exit(1)


# Shared command handlers for denylist/allowlist
def _handle_list_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    active_only: bool,
    inactive_only: bool,
) -> None:
    """Shared handler for list commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        client: APIClient = ctx.obj["client"]
        entries = client.get_domain_list(profile_id, list_type)
        if not entries:
            click.echo(f"{list_type.capitalize()} is empty.")
            return

        if active_only:
            entries = [e for e in entries if e.get("active", True)]
        elif inactive_only:
            entries = [e for e in entries if not e.get("active", True)]

        if not entries:
            click.echo("No matching entries found.")
            return

        for entry in entries:
            domain = entry.get("id", "unknown")
            active = entry.get("active", True)
            status = "" if active else " (inactive)"
            click.echo(f"{domain}{status}")

        click.echo(f"\nTotal: {len(entries)} entries", err=True)
    except Exception as e:
        click.echo(f"Error fetching {list_type}: {e}", err=True)
        raise click.Abort()


def _handle_add_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    domains: Tuple[str, ...],
    inactive: bool,
    update_existing: bool,
) -> None:
    """Shared handler for add commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    # Validate domains
    valid_domains, invalid_domains = _validate_domains(domains)
    if invalid_domains:
        click.echo("Invalid domains skipped:", err=True)
        for error in invalid_domains:
            click.echo(f"  - {error}", err=True)

    if not valid_domains:
        click.echo("No valid domains to add.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)
    client: APIClient = ctx.obj["client"]
    existing_entries = _fetch_domain_list(client, profile_id, list_type)
    desired_active = not inactive
    plan = _plan_domain_additions(valid_domains, existing_entries, desired_active, update_existing)

    _execute_add_plan(ctx, client, profile_id, list_type, plan, desired_active, update_existing)

    if not ctx.obj.get("dry_run", False):
        click.echo(f"\nView at: https://my.nextdns.io/{profile_id}/{list_type}")


def _handle_remove_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    domains: Tuple[str, ...],
) -> None:
    """Shared handler for remove commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    valid_domains, invalid_domains = _validate_domains(domains)
    if invalid_domains:
        click.echo("Invalid domains skipped:", err=True)
        for error in invalid_domains:
            click.echo(f"  - {error}", err=True)

    if not valid_domains:
        click.echo("No valid domains to remove.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)
    client: APIClient = ctx.obj["client"]
    existing_entries = _fetch_domain_list(client, profile_id, list_type)
    plan = _plan_domain_removals(valid_domains, existing_entries)

    _execute_removal_plan(ctx, client, profile_id, list_type, plan)


def _handle_import_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    source: str,
    inactive: bool,
    update_existing: bool,
) -> None:
    """Shared handler for import commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    profile_id = _resolve_profile_id(ctx, profile)
    client: APIClient = ctx.obj["client"]

    try:
        # Parse through the streaming source reader, then plan against the current list.
        raw_domains = list(read_domains_from_source(source))
    except Exception as e:
        click.echo(f"Error reading source: {e}", err=True)
        raise click.Abort()

    if not raw_domains:
        click.echo("No domains found in source.", err=True)
        return

    # Validate domains
    valid_domains, invalid_domains = _validate_domains(raw_domains)
    if invalid_domains:
        click.echo(f"Skipped {len(invalid_domains)} invalid domain(s):", err=True)
        for error in invalid_domains[:INVALID_PREVIEW_LIMIT]:
            click.echo(f"  - {error}", err=True)
        if len(invalid_domains) > INVALID_PREVIEW_LIMIT:
            click.echo(f"  ... {len(invalid_domains) - INVALID_PREVIEW_LIMIT} more", err=True)
        click.echo(
            "Warning: in nextdnsctl 2.0, invalid lines will make the import fail instead of being skipped.",
            err=True,
        )

    if not valid_domains:
        click.echo("No valid domains to import.", err=True)
        return

    existing_entries = _fetch_domain_list(client, profile_id, list_type)
    desired_active = not inactive
    plan = _plan_domain_additions(valid_domains, existing_entries, desired_active, update_existing)
    _execute_add_plan(ctx, client, profile_id, list_type, plan, desired_active, update_existing)

    if not ctx.obj.get("dry_run", False):
        click.echo(f"\nView at: https://my.nextdns.io/{profile_id}/{list_type}")


def _handle_export_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    output: str,
    active_only: bool,
    inactive_only: bool,
) -> None:
    """Shared handler for export commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        client: APIClient = ctx.obj["client"]
        entries = client.get_domain_list(profile_id, list_type)
        if not entries:
            click.echo(f"{list_type.capitalize()} is empty, nothing to export.", err=True)
            return

        if active_only:
            entries = [e for e in entries if e.get("active", True)]
        elif inactive_only:
            entries = [e for e in entries if not e.get("active", True)]

        if not entries:
            click.echo("No matching entries to export.", err=True)
            return

        domains = [entry.get("id", "") for entry in entries if entry.get("id")]
        content = "\n".join(domains) + "\n"

        if output == "-":
            click.echo(content, nl=False)
        else:
            with open(output, "w") as f:
                f.write(content)
            click.echo(f"Exported {len(domains)} domains to {output}", err=True)
    except Exception as e:
        click.echo(f"Error exporting {list_type}: {e}", err=True)
        raise click.Abort()


def _handle_clear_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    yes: bool,
) -> None:
    """Shared handler for clear commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        client: APIClient = ctx.obj["client"]
        entries = client.get_domain_list(profile_id, list_type)
        if not entries:
            click.echo(f"{list_type.capitalize()} is already empty.")
            return

        domains: List[str] = [entry["id"] for entry in entries if entry.get("id")]
        if not domains:
            click.echo(f"{list_type.capitalize()} is already empty.")
            return

        dry_run = ctx.obj.get("dry_run", False)
        if not yes and not dry_run:
            click.confirm(
                f"This will remove {len(domains)} domains from the {list_type}. " "Continue?",
                abort=True,
            )

        def operation(domain_name):
            return client.remove_from_domain_list(
                profile_id,
                list_type,
                domain_name,
            )

        success = _perform_domain_operations(ctx, domains, operation, item_name_singular="domain", action_verb="remove")
        if not success:
            ctx.exit(1)
    except (click.Abort, click.exceptions.Exit):
        raise
    except Exception as e:
        click.echo(f"Error clearing {list_type}: {e}", err=True)
        raise click.Abort()


@cli.group("denylist")
def denylist():
    """Manage the NextDNS denylist."""


@denylist.command("list")
@click.argument("profile")
@click.option("--active-only", is_flag=True, help="Show only active entries")
@click.option("--inactive-only", is_flag=True, help="Show only inactive entries")
@click.pass_context
def denylist_list(ctx, profile, active_only, inactive_only):
    """List all domains in the NextDNS denylist."""
    _handle_list_command(ctx, profile, "denylist", active_only, inactive_only)


@denylist.command("add")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.option("--update-existing", is_flag=True, help="Update active state for domains already in the list")
@click.pass_context
def denylist_add(ctx, profile, domains, inactive, update_existing):
    """Add domains to the NextDNS denylist."""
    _handle_add_command(ctx, profile, "denylist", domains, inactive, update_existing)


@denylist.command("remove")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.pass_context
def denylist_remove(ctx, profile, domains):
    """Remove domains from the NextDNS denylist."""
    _handle_remove_command(ctx, profile, "denylist", domains)


@denylist.command("import")
@click.argument("profile")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.option("--update-existing", is_flag=True, help="Update active state for domains already in the list")
@click.pass_context
def denylist_import(ctx, profile, source, inactive, update_existing):
    """Import domains from a file or URL to the NextDNS denylist."""
    _handle_import_command(ctx, profile, "denylist", source, inactive, update_existing)


@denylist.command("export")
@click.argument("profile")
@click.argument("output", type=click.Path(), default="-")
@click.option("--active-only", is_flag=True, help="Export only active entries")
@click.option("--inactive-only", is_flag=True, help="Export only inactive entries")
@click.pass_context
def denylist_export(ctx, profile, output, active_only, inactive_only):
    """Export denylist domains to a file (or stdout with -)."""
    _handle_export_command(ctx, profile, "denylist", output, active_only, inactive_only)


@denylist.command("clear")
@click.argument("profile")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def denylist_clear(ctx, profile, yes):
    """Remove all domains from the denylist."""
    _handle_clear_command(ctx, profile, "denylist", yes)


@cli.group("allowlist")
def allowlist():
    """Manage the NextDNS allowlist."""


@allowlist.command("list")
@click.argument("profile")
@click.option("--active-only", is_flag=True, help="Show only active entries")
@click.option("--inactive-only", is_flag=True, help="Show only inactive entries")
@click.pass_context
def allowlist_list(ctx, profile, active_only, inactive_only):
    """List all domains in the NextDNS allowlist."""
    _handle_list_command(ctx, profile, "allowlist", active_only, inactive_only)


@allowlist.command("add")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.option("--update-existing", is_flag=True, help="Update active state for domains already in the list")
@click.pass_context
def allowlist_add(ctx, profile, domains, inactive, update_existing):
    """Add domains to the NextDNS allowlist."""
    _handle_add_command(ctx, profile, "allowlist", domains, inactive, update_existing)


@allowlist.command("remove")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.pass_context
def allowlist_remove(ctx, profile, domains):
    """Remove domains from the NextDNS allowlist."""
    _handle_remove_command(ctx, profile, "allowlist", domains)


@allowlist.command("import")
@click.argument("profile")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.option("--update-existing", is_flag=True, help="Update active state for domains already in the list")
@click.pass_context
def allowlist_import(ctx, profile, source, inactive, update_existing):
    """Import domains from a file or URL to the NextDNS allowlist."""
    _handle_import_command(ctx, profile, "allowlist", source, inactive, update_existing)


@allowlist.command("export")
@click.argument("profile")
@click.argument("output", type=click.Path(), default="-")
@click.option("--active-only", is_flag=True, help="Export only active entries")
@click.option("--inactive-only", is_flag=True, help="Export only inactive entries")
@click.pass_context
def allowlist_export(ctx, profile, output, active_only, inactive_only):
    """Export allowlist domains to a file (or stdout with -)."""
    _handle_export_command(ctx, profile, "allowlist", output, active_only, inactive_only)


@allowlist.command("clear")
@click.argument("profile")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def allowlist_clear(ctx, profile, yes):
    """Remove all domains from the allowlist."""
    _handle_clear_command(ctx, profile, "allowlist", yes)


if __name__ == "__main__":
    cli()
