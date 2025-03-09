import click
import requests
import json

from .config import save_api_key, load_api_key
from .api import (
    get_profiles, add_to_denylist, remove_from_denylist,
    add_to_allowlist, remove_from_allowlist
)
from .sync import (
    load_sync_config, save_sync_config, sync_profile,
    sync_all_profiles, get_current_config
)


__version__ = "0.2.0"


@click.group()
@click.version_option(__version__)
def cli():
    """nextdnsctl: A CLI tool for managing NextDNS profiles."""
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
def profile_list():
    """List all NextDNS profiles."""
    try:
        profiles = get_profiles()
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
def denylist_add(profile_id, domains, inactive):
    """Add domains to the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    for domain in domains:
        try:
            result = add_to_denylist(profile_id, domain, active=not inactive)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to add {domain}: {e}", err=True)


@denylist.command("remove")
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
def denylist_remove(profile_id, domains):
    """Remove domains from the NextDNS denylist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    for domain in domains:
        try:
            result = remove_from_denylist(profile_id, domain)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to remove {domain}: {e}", err=True)


@denylist.command("import")
@click.argument("profile_id")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not blocked)")
def denylist_import(profile_id, source, inactive):
    """Import domains from a file or URL to the NextDNS denylist."""
    try:
        content = read_source(source)
    except Exception as e:
        click.echo(f"Error reading source: {e}", err=True)
        raise click.Abort()

    domains = [line.strip() for line in content.splitlines() if line.strip()
               and not line.strip().startswith('#')]
    if not domains:
        click.echo("No domains found in source.", err=True)
        return

    for domain in domains:
        try:
            result = add_to_denylist(profile_id, domain, active=not inactive)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to add {domain}: {e}", err=True)


def read_source(source):
    """Read content from a file or URL."""
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source)
        response.raise_for_status()
        return response.text
    else:
        with open(source, 'r') as f:
            return f.read()


@cli.group("allowlist")
def allowlist():
    """Manage the NextDNS allowlist."""


@allowlist.command("add")
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
def allowlist_add(profile_id, domains, inactive):
    """Add domains to the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    for domain in domains:
        try:
            result = add_to_allowlist(profile_id, domain, active=not inactive)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to add {domain}: {e}", err=True)


@allowlist.command("remove")
@click.argument("profile_id")
@click.argument("domains", nargs=-1)
def allowlist_remove(profile_id, domains):
    """Remove domains from the NextDNS allowlist."""
    if not domains:
        click.echo("No domains provided.", err=True)
        raise click.Abort()

    for domain in domains:
        try:
            result = remove_from_allowlist(profile_id, domain)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to remove {domain}: {e}", err=True)


@allowlist.command("import")
@click.argument("profile_id")
@click.argument("source")
@click.option("--inactive", is_flag=True, help="Add domains as inactive (not allowed)")
def allowlist_import(profile_id, source, inactive):
    """Import domains from a file or URL to the NextDNS allowlist."""
    try:
        content = read_source(source)
    except Exception as e:
        click.echo(f"Error reading source: {e}", err=True)
        raise click.Abort()

    domains = [line.strip() for line in content.splitlines() if line.strip()
               and not line.strip().startswith('#')]
    if not domains:
        click.echo("No domains found in source.", err=True)
        return

    for domain in domains:
        try:
            result = add_to_allowlist(profile_id, domain, active=not inactive)
            click.echo(result)
        except Exception as e:
            click.echo(f"Failed to add {domain}: {e}", err=True)


@cli.group("sync")
def sync():
    """Manage NextDNS configuration sync."""
    pass


@sync.command("export")
@click.argument("profile_id")
@click.argument("output_file", type=click.Path())
def sync_export(profile_id, output_file):
    """Export current profile configuration to a file."""
    try:
        config = get_current_config(profile_id)
        with open(output_file, "w") as f:
            json.dump({"profiles": {profile_id: config}}, f, indent=2)
        click.echo(f"Configuration exported to {output_file}")
    except Exception as e:
        click.echo(f"Error exporting configuration: {e}", err=True)
        raise click.Abort()


@sync.command("import")
@click.argument("config_file", type=click.Path(exists=True))
def sync_import(config_file):
    """Import and save configuration for sync."""
    try:
        with open(config_file, "r") as f:
            config = json.load(f)
        save_sync_config(config)
        click.echo("Configuration imported successfully")
    except Exception as e:
        click.echo(f"Error importing configuration: {e}", err=True)
        raise click.Abort()


@sync.command("apply")
@click.argument("profile_id", required=False)
def sync_apply(profile_id):
    """Apply configuration to one or all profiles."""
    try:
        if profile_id:
            config = load_sync_config()
            if profile_id not in config.get("profiles", {}):
                click.echo(f"Profile {profile_id} not found in sync config", err=True)
                raise click.Abort()
            changes = sync_profile(profile_id, config["profiles"][profile_id])
            for change in changes:
                click.echo(f"[{profile_id}] {change}")
        else:
            results = sync_all_profiles()
            for pid, changes in results.items():
                for change in changes:
                    click.echo(f"[{pid}] {change}")
    except Exception as e:
        click.echo(f"Error applying configuration: {e}", err=True)
        raise click.Abort()


if __name__ == "__main__":
    cli()
