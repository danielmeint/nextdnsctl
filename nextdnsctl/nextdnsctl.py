import click
import requests

from .config import save_api_key, load_api_key
from .api import (
    get_profiles,
    add_to_denylist,
    remove_from_denylist,
    add_to_allowlist,
    remove_from_allowlist,
    DEFAULT_RETRIES,
    DEFAULT_DELAY,
    DEFAULT_TIMEOUT,
    RateLimitStillActiveError,
)

__version__ = "0.2.0"


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
    """
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
@click.pass_context
def cli(ctx, retry_attempts, retry_delay, timeout):
    """nextdnsctl: A CLI tool for managing NextDNS profiles."""
    ctx.obj = {
        "retry_attempts": retry_attempts,
        "retry_delay": retry_delay,
        "timeout": timeout,
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


@denylist.command("add")
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.pass_context
def denylist_add(ctx, profile_id, domains, inactive):
    """Add domains to the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

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
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
@click.pass_context
def denylist_remove(ctx, profile_id, domains):
    """Remove domains from the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

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
@click.argument("profile_id")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
@click.pass_context
def denylist_import(ctx, profile_id, source, inactive):
    """Import domains from a file or URL to the NextDNS denylist."""
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


@allowlist.command("add")
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.pass_context
def allowlist_add(ctx, profile_id, domains, inactive):
    """Add domains to the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

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
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
@click.pass_context
def allowlist_remove(ctx, profile_id, domains):
    """Remove domains from the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

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
@click.argument("profile_id")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
@click.pass_context
def allowlist_import(ctx, profile_id, source, inactive):
    """Import domains from a file or URL to the NextDNS allowlist."""
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


if __name__ == "__main__":
    cli()
