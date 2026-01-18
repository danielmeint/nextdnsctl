import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import click
import requests

from .config import save_api_key, load_api_key
from .api import (
    get_profiles,
    get_denylist,
    add_to_denylist,
    remove_from_denylist,
    get_allowlist,
    add_to_allowlist,
    remove_from_allowlist,
    DEFAULT_RETRIES,
    DEFAULT_DELAY,
    DEFAULT_TIMEOUT,
    RateLimitStillActiveError,
)

__version__ = "0.3.0"
DEFAULT_CONCURRENCY = 5


def _resolve_profile_id(ctx, profile_identifier):
    """
    Resolve a profile identifier (ID or name) to a profile ID.

    If the identifier matches an existing profile ID, return it directly.
    Otherwise, search for a profile with a matching name.
    Caches the profiles list in ctx.obj to avoid repeated API calls.
    """
    api_params = {
        "retries": ctx.obj["retry_attempts"],
        "delay": ctx.obj["retry_delay"],
        "timeout": ctx.obj["timeout"],
    }

    # Get or fetch profiles (cache in ctx.obj)
    if "profiles_cache" not in ctx.obj:
        try:
            ctx.obj["profiles_cache"] = get_profiles(**api_params)
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
    available = ", ".join(
        f"'{p.get('name')}' ({p.get('id')})" for p in profiles
    )
    raise click.ClickException(
        f"Profile '{profile_identifier}' not found. "
        f"Available profiles: {available}"
    )


# Helper function to perform operations on a list of domains
def _perform_domain_operations(
    ctx,
    domains_to_process,
    operation_callable,
    item_name_singular="domain",
    action_verb="process",
):
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
        return _perform_domain_operations_dry_run(
            domains_to_process, item_name_singular, action_verb
        )

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
    domains_to_process,
    item_name_singular,
    action_verb,
):
    """Dry-run mode: show what would be done without making changes."""
    click.echo(f"[DRY-RUN] Would {action_verb} {len(domains_to_process)} {item_name_singular}(s):")
    for domain in domains_to_process:
        click.echo(f"  - {domain}")
    click.echo("\n[DRY-RUN] No changes made.", err=True)
    return True


def _perform_domain_operations_sequential(
    ctx,
    domains_to_process,
    operation_callable,
    item_name_singular,
    action_verb,
):
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
            f"\nWarning: {failure_count} {item_name_singular}(s) could not be {action_verb}ed "
            f"due to other errors.",
            err=True,
        )
    return all_successful


def _perform_domain_operations_parallel(
    ctx,
    domains_to_process,
    operation_callable,
    item_name_singular,
    action_verb,
    concurrency,
):
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

        with click.progressbar(
            length=submitted_count,
            label=f"Processing {item_name_singular}s",
            show_pos=True,
        ) as bar:
            for future in as_completed(futures):
                domain = futures[future]
                try:
                    future.result()
                    results["success"] += 1
                except RateLimitStillActiveError as e:
                    rate_limit_hit.set()
                    rate_limit_aborted = True
                    results["failed"] += 1
                    errors.append(
                        f"CRITICAL: '{domain}' - persistent rate limiting: {e}"
                    )
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
    try:
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        profiles = get_profiles(**api_params)
        if not profiles:
            click.echo("No profiles found.")
            return
        for profile in profiles:
            click.echo(f"{profile['id']}: {profile['name']}")
    except Exception as e:
        click.echo(f"Error fetching profiles: {e}", err=True)
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
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_denylist(profile_id, **api_params)
        if not entries:
            click.echo("Denylist is empty.")
            return

        # Filter by active status if requested
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
        click.echo(f"Error fetching denylist: {e}", err=True)
        raise click.Abort()


@denylist.command("add")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.pass_context
def denylist_add(ctx, profile, domains, inactive):
    """Add domains to the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)

    def operation(domain_name):
        return add_to_denylist(
            profile_id,
            domain_name,
            active=not inactive,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx, domains, operation, item_name_singular="domain", action_verb="add"
    )
    if not success:
        ctx.exit(1)


@denylist.command("remove")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.pass_context
def denylist_remove(ctx, profile, domains):
    """Remove domains from the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)

    def operation(domain_name):
        return remove_from_denylist(
            profile_id,
            domain_name,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx, domains, operation, item_name_singular="domain", action_verb="remove"
    )
    if not success:
        ctx.exit(1)


@denylist.command("import")
@click.argument("profile")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.pass_context
def denylist_import(ctx, profile, source, inactive):
    """Import domains from a file or URL to the NextDNS denylist."""
    profile_id = _resolve_profile_id(ctx, profile)

    try:
        content = read_source(source)
    except Exception as e:
        click.echo(f"Error reading source: {e}", err=True)
        raise click.Abort()

    domains_to_import = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not domains_to_import:
        click.echo("No domains found in source.", err=True)
        return

    def operation(domain_name):
        return add_to_denylist(
            profile_id,
            domain_name,
            active=not inactive,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx,
        domains_to_import,
        operation,
        item_name_singular="domain",
        action_verb="add",
    )
    if not success:
        ctx.exit(1)


@denylist.command("export")
@click.argument("profile")
@click.argument("output", type=click.Path(), default="-")
@click.option("--active-only", is_flag=True, help="Export only active entries")
@click.option("--inactive-only", is_flag=True, help="Export only inactive entries")
@click.pass_context
def denylist_export(ctx, profile, output, active_only, inactive_only):
    """Export denylist domains to a file (or stdout with -)."""
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_denylist(profile_id, **api_params)
        if not entries:
            click.echo("Denylist is empty, nothing to export.", err=True)
            return

        # Filter by active status if requested
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
        click.echo(f"Error exporting denylist: {e}", err=True)
        raise click.Abort()


@denylist.command("clear")
@click.argument("profile")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def denylist_clear(ctx, profile, yes):
    """Remove all domains from the denylist."""
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_denylist(profile_id, **api_params)
        if not entries:
            click.echo("Denylist is already empty.")
            return

        domains = [entry.get("id") for entry in entries if entry.get("id")]
        if not domains:
            click.echo("Denylist is already empty.")
            return

        dry_run = ctx.obj.get("dry_run", False)
        if not yes and not dry_run:
            click.confirm(
                f"This will remove {len(domains)} domains from the denylist. Continue?",
                abort=True,
            )

        def operation(domain_name):
            return remove_from_denylist(
                profile_id,
                domain_name,
                retries=ctx.obj["retry_attempts"],
                delay=ctx.obj["retry_delay"],
                timeout=ctx.obj["timeout"],
            )

        success = _perform_domain_operations(
            ctx, domains, operation, item_name_singular="domain", action_verb="remove"
        )
        if not success:
            ctx.exit(1)
    except click.Abort:
        raise
    except Exception as e:
        click.echo(f"Error clearing denylist: {e}", err=True)
        raise click.Abort()


def read_source(source):
    """Read content from a file or URL."""
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(
            source, timeout=DEFAULT_TIMEOUT
        )  # Using global default timeout
        response.raise_for_status()
        return response.text
    else:
        with open(source, "r") as f:
            return f.read()


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
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_allowlist(profile_id, **api_params)
        if not entries:
            click.echo("Allowlist is empty.")
            return

        # Filter by active status if requested
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
        click.echo(f"Error fetching allowlist: {e}", err=True)
        raise click.Abort()


@allowlist.command("add")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.pass_context
def allowlist_add(ctx, profile, domains, inactive):
    """Add domains to the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)

    def operation(domain_name):
        return add_to_allowlist(
            profile_id,
            domain_name,
            active=not inactive,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx, domains, operation, item_name_singular="domain", action_verb="add"
    )
    if not success:
        ctx.exit(1)


@allowlist.command("remove")
@click.argument("profile")
@click.argument("domains", nargs=-1)
@click.pass_context
def allowlist_remove(ctx, profile, domains):
    """Remove domains from the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    profile_id = _resolve_profile_id(ctx, profile)

    def operation(domain_name):
        return remove_from_allowlist(
            profile_id,
            domain_name,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx, domains, operation, item_name_singular="domain", action_verb="remove"
    )
    if not success:
        ctx.exit(1)


@allowlist.command("import")
@click.argument("profile")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.pass_context
def allowlist_import(ctx, profile, source, inactive):
    """Import domains from a file or URL to the NextDNS allowlist."""
    profile_id = _resolve_profile_id(ctx, profile)

    try:
        content = read_source(source)
    except Exception as e:
        click.echo(f"Error reading source: {e}", err=True)
        raise click.Abort()

    domains_to_import = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not domains_to_import:
        click.echo("No domains found in source.", err=True)
        return

    def operation(domain_name):
        return add_to_allowlist(
            profile_id,
            domain_name,
            active=not inactive,
            retries=ctx.obj["retry_attempts"],
            delay=ctx.obj["retry_delay"],
            timeout=ctx.obj["timeout"],
        )

    success = _perform_domain_operations(
        ctx,
        domains_to_import,
        operation,
        item_name_singular="domain",
        action_verb="add",
    )
    if not success:
        ctx.exit(1)


@allowlist.command("export")
@click.argument("profile")
@click.argument("output", type=click.Path(), default="-")
@click.option("--active-only", is_flag=True, help="Export only active entries")
@click.option("--inactive-only", is_flag=True, help="Export only inactive entries")
@click.pass_context
def allowlist_export(ctx, profile, output, active_only, inactive_only):
    """Export allowlist domains to a file (or stdout with -)."""
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_allowlist(profile_id, **api_params)
        if not entries:
            click.echo("Allowlist is empty, nothing to export.", err=True)
            return

        # Filter by active status if requested
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
        click.echo(f"Error exporting allowlist: {e}", err=True)
        raise click.Abort()


@allowlist.command("clear")
@click.argument("profile")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def allowlist_clear(ctx, profile, yes):
    """Remove all domains from the allowlist."""
    try:
        profile_id = _resolve_profile_id(ctx, profile)
        api_params = {
            "retries": ctx.obj["retry_attempts"],
            "delay": ctx.obj["retry_delay"],
            "timeout": ctx.obj["timeout"],
        }
        entries = get_allowlist(profile_id, **api_params)
        if not entries:
            click.echo("Allowlist is already empty.")
            return

        domains = [entry.get("id") for entry in entries if entry.get("id")]
        if not domains:
            click.echo("Allowlist is already empty.")
            return

        dry_run = ctx.obj.get("dry_run", False)
        if not yes and not dry_run:
            click.confirm(
                f"This will remove {len(domains)} domains from the allowlist. Continue?",
                abort=True,
            )

        def operation(domain_name):
            return remove_from_allowlist(
                profile_id,
                domain_name,
                retries=ctx.obj["retry_attempts"],
                delay=ctx.obj["retry_delay"],
                timeout=ctx.obj["timeout"],
            )

        success = _perform_domain_operations(
            ctx, domains, operation, item_name_singular="domain", action_verb="remove"
        )
        if not success:
            ctx.exit(1)
    except click.Abort:
        raise
    except Exception as e:
        click.echo(f"Error clearing allowlist: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
