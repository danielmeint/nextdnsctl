import atexit
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    for profile in profiles:
        if profile.get("name", "").lower() == profile_identifier.lower():
            return profile["id"]

    # No match found
    available = ", ".join(f"'{p.get('name')}' ({p.get('id')})" for p in profiles)
    raise click.ClickException(f"Profile '{profile_identifier}' not found. " f"Available profiles: {available}")


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
    for item_value in domains_to_process:
        try:
            result = operation_callable(item_value)
            click.echo(result)
        except RateLimitStillActiveError as e:
            click.echo(
                f"\nCRITICAL ERROR: Domain '{item_value}' could not be {action_verb}ed "
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
            f"\nWarning: {failure_count} {item_name_singular}(s) could not be {action_verb}ed " f"due to other errors.",
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

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for domain in domains_to_process:
            if rate_limit_hit.is_set():
                results["skipped"] += 1
                continue
            futures[executor.submit(operation_callable, domain)] = domain

        submitted_count = len(futures)

        progress_bar: Any = click.progressbar(
            length=submitted_count,
            label=f"Processing {item_name_singular}s",
            show_pos=True,
        )
        with progress_bar as bar:
            for future in as_completed(futures):
                domain = futures[future]
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
@click.argument("api_key")
def auth(api_key):
    """Save your NextDNS API key."""
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

    def operation(domain_name):
        return client.add_to_domain_list(
            profile_id,
            list_type,
            domain_name,
            active=not inactive,
        )

    success = _perform_domain_operations(ctx, valid_domains, operation, item_name_singular="domain", action_verb="add")
    if not success:
        ctx.exit(1)


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

    profile_id = _resolve_profile_id(ctx, profile)
    client: APIClient = ctx.obj["client"]

    def operation(domain_name):
        return client.remove_from_domain_list(
            profile_id,
            list_type,
            domain_name,
        )

    success = _perform_domain_operations(ctx, domains, operation, item_name_singular="domain", action_verb="remove")
    if not success:
        ctx.exit(1)


def _handle_import_command(
    ctx: click.Context,
    profile: str,
    list_type: str,
    source: str,
    inactive: bool,
) -> None:
    """Shared handler for import commands."""
    if "client" not in ctx.obj:
        raise click.ClickException("No API key configured. Run 'nextdnsctl auth <api_key>' first.")
    profile_id = _resolve_profile_id(ctx, profile)
    client: APIClient = ctx.obj["client"]

    try:
        # Use generator to stream file/URL and collect domains
        # This avoids loading raw file content into memory
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
        click.echo(f"Skipped {len(invalid_domains)} invalid domain(s).", err=True)

    if not valid_domains:
        click.echo("No valid domains to import.", err=True)
        return

    def operation(domain_name):
        return client.add_to_domain_list(
            profile_id,
            list_type,
            domain_name,
            active=not inactive,
        )

    success = _perform_domain_operations(
        ctx,
        valid_domains,
        operation,
        item_name_singular="domain",
        action_verb="add",
    )
    if not success:
        ctx.exit(1)


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
    except click.Abort:
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
@click.pass_context
def denylist_add(ctx, profile, domains, inactive):
    """Add domains to the NextDNS denylist."""
    _handle_add_command(ctx, profile, "denylist", domains, inactive)


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
@click.pass_context
def denylist_import(ctx, profile, source, inactive):
    """Import domains from a file or URL to the NextDNS denylist."""
    _handle_import_command(ctx, profile, "denylist", source, inactive)


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
@click.pass_context
def allowlist_add(ctx, profile, domains, inactive):
    """Add domains to the NextDNS allowlist."""
    _handle_add_command(ctx, profile, "allowlist", domains, inactive)


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
@click.pass_context
def allowlist_import(ctx, profile, source, inactive):
    """Import domains from a file or URL to the NextDNS allowlist."""
    _handle_import_command(ctx, profile, "allowlist", source, inactive)


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
