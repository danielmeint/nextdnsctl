"""nextdnsctl command line."""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

import click

from .. import __version__
from ..auth import PROFILE_ENV_VAR, NoAPIKeyError, load_api_key
from ..client import DEFAULT_TIMEOUT, Client, NextDNSError
from ..config import DEFAULT_FILE, ConfigError
from ..planner import Planner, PlanError
from ..pull import DuplicateNameError
from ..sources import SourceError

EXIT_ERROR = 1
EXIT_CHANGES = 2  # plan: there are changes
EXIT_PARTIAL = 3  # apply: some but not all changes were applied

EXPECTED_ERRORS = (ConfigError, PlanError, NextDNSError, NoAPIKeyError, SourceError, DuplicateNameError)


def make_client(api_key: str, timeout: float = DEFAULT_TIMEOUT) -> Client:
    """Create the API client. Tests replace this to talk to a fake API."""
    return Client(api_key, timeout=timeout)


class State:
    """Shared per-invocation state, created lazily so `--help` and `auth` need no API key."""

    def __init__(
        self, *, profile: Optional[str], file: str, json_output: bool, verbose: bool, timeout: float, dry_run: bool
    ):
        self.profile = profile
        self.file = file
        self.json = json_output
        self.verbose = verbose
        self.timeout = timeout
        self.dry_run = dry_run
        self._client: Optional[Client] = None
        self._planner: Optional[Planner] = None

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = make_client(load_api_key(), self.timeout)
        return self._client

    @property
    def planner(self) -> Planner:
        if self._planner is None:
            self._planner = Planner(self.client)
        return self._planner

    def require_profile(self) -> str:
        if not self.profile:
            raise click.UsageError(f"No profile given. Use -p/--profile NAME or set {PROFILE_ENV_VAR}.")
        return self.profile


class RootGroup(click.Group):
    """Turns expected errors into a clean message and exit code 1 instead of a traceback."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except EXPECTED_ERRORS as e:
            raise click.ClickException(str(e)) from e


def _deprecated_noop(ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    if value is not None:
        click.echo(
            f"warning: --{param.name.replace('_', '-')} has no effect since nextdnsctl 2.0 "
            "(writes are paced to NextDNS's rate limit automatically)",
            err=True,
        )
    return value


@click.group(cls=RootGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="nextdnsctl")
@click.option("-p", "--profile", envvar=PROFILE_ENV_VAR, help=f"Profile name or ID (or set {PROFILE_ENV_VAR}).")
@click.option(
    "-f",
    "--file",
    "file",
    default=DEFAULT_FILE,
    envvar="NEXTDNS_FILE",
    show_default=True,
    help="Profile file for pull/plan/apply.",
)
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output on stdout.")
@click.option("-v", "--verbose", is_flag=True, help="Show every entry and every request.")
@click.option("-q", "--quiet", is_flag=True, help="Only show errors.")
@click.option("--timeout", type=float, default=DEFAULT_TIMEOUT, show_default=True, help="Request timeout in seconds.")
@click.option("--dry-run", is_flag=True, help="Show what would change without changing anything.")
@click.option("--concurrency", type=int, hidden=True, callback=_deprecated_noop, expose_value=False)
@click.option("--retry-attempts", type=int, hidden=True, callback=_deprecated_noop, expose_value=False)
@click.option("--retry-delay", type=float, hidden=True, callback=_deprecated_noop, expose_value=False)
@click.pass_context
def cli(ctx: click.Context, profile, file, json_output, verbose, quiet, timeout, dry_run) -> None:
    """Manage NextDNS profiles from the command line, or declaratively from a file.

    \b
    Declarative:   pull → edit nextdns.yaml → plan → apply
    Quick edits:   denylist / allowlist / rewrites
    Observability: logs, why
    """
    level = logging.ERROR if quiet else logging.DEBUG if verbose else logging.WARNING
    _configure_logging(level)
    ctx.obj = State(
        profile=profile, file=file, json_output=json_output, verbose=verbose, timeout=timeout, dry_run=dry_run
    )


def _configure_logging(level: int) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter())
    root = logging.getLogger("nextdnsctl")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno >= logging.ERROR:
            return f"error: {message}"
        if record.levelno >= logging.WARNING:
            return f"warning: {message}"
        return message


pass_state = click.make_pass_decorator(State)


def main() -> None:
    cli(prog_name="nextdnsctl")


# Register commands (imported for their side effects on `cli`).
from . import declarative, lists, misc  # noqa: E402,F401
