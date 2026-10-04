"""pull / plan / apply."""

from __future__ import annotations

import os
import sys
from typing import Optional

import click

from .. import config as config_mod
from .. import pull as pull_mod
from ..executor import ApplyResult, Executor
from ..planner import Op, ProfilePlan
from . import EXIT_CHANGES, EXIT_ERROR, EXIT_PARTIAL, ConfirmationRequired, State, cli, pass_state
from .output import emit_json, info, plan_to_json, render_plan, render_result, result_to_json, warn
from .status import status, working


PROGRESS_BAR_MIN_OPS = 10


@cli.command()
@click.argument("profiles", nargs=-1)
@click.option("--stdout", "to_stdout", is_flag=True, help="Print the YAML instead of writing the file.")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@pass_state
def pull(state: State, profiles: tuple[str, ...], to_stdout: bool, force: bool) -> None:
    """Write the live profiles to the profile file (default: all profiles).

    The result is a complete description of each profile; edit it, then use
    'plan' and 'apply'. Device setup (linked IP, DDNS token) is never written.
    """
    planner = state.planner
    names = profiles or ((state.profile,) if state.profile else ())
    with working("Fetching the profile list"):
        targets = [planner.require_profile(n) for n in names] if names else planner.profiles()
        planner.prefetch(targets)
        text = pull_mod.document([planner.live(p["id"]) for p in targets])

    if to_stdout:
        click.echo(text, nl=False)
        return
    if os.path.exists(state.file):
        if not force:
            raise click.ClickException(
                f"{state.file} already exists. Use 'nextdnsctl plan' to compare it with NextDNS, "
                "or --force to overwrite it."
            )
        try:
            existing = config_mod.load(state.file)
        except config_mod.ConfigError:
            existing = None
        if existing and any(spec.sources for p in existing.profiles for spec in p.lists.values()):
            raise click.ClickException(
                f"{state.file} uses list sources, which pull can't reproduce (it would inline every domain). "
                "Use 'nextdnsctl plan' instead, or pull with --stdout."
            )
    tmp = state.file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, state.file)
    info(f"Wrote {len(targets)} profile(s) to {state.file}")


def _make_plans(state: State, profiles: tuple[str, ...]) -> list[ProfilePlan]:
    cfg = config_mod.load(state.file)
    selected = []
    for name in profiles or ((state.profile,) if state.profile else (None,)):
        for profile in cfg.select(name):
            if profile not in selected:
                selected.append(profile)
    planner = state.planner
    with working("Fetching the profile list") as line:
        found = [planner.find_profile(p.key, p.id) for p in selected]
        planner.prefetch([f for f in found if f is not None])
        plans = []
        for profile in selected:
            line.update(f"Planning {profile.key}")
            plans.append(planner.plan(profile))
    return plans


def _show_plans(state: State, plans: list[ProfilePlan]) -> None:
    for plan in plans:
        for warning in plan.warnings:
            warn(warning)
    if state.json:
        return
    for plan in plans:
        for line in render_plan(plan, state.verbose):
            click.echo(line)


@cli.command()
@click.argument("profiles", nargs=-1)
@pass_state
def plan(state: State, profiles: tuple[str, ...]) -> None:
    """Show what 'apply' would change. Exit code 2 when there are changes."""
    plans = _make_plans(state, profiles)
    _show_plans(state, plans)
    if state.json:
        emit_json({"profiles": [plan_to_json(p) for p in plans]})
    if any(p.has_changes for p in plans):
        sys.exit(EXIT_CHANGES)


@cli.command()
@click.argument("profiles", nargs=-1)
@click.option("-y", "--yes", is_flag=True, help="Don't ask for confirmation.")
@pass_state
def apply(state: State, profiles: tuple[str, ...], yes: bool) -> None:
    """Make the profiles match the profile file."""
    plans = _make_plans(state, profiles)
    _show_plans(state, plans)
    pending = [p for p in plans if p.has_changes]
    if not pending:
        if state.json:
            emit_json({"profiles": []})
        else:
            info("No changes.")
        return
    if state.dry_run:
        if state.json:
            emit_json({"profiles": [plan_to_json(p) for p in plans]})
        return
    confirm(pending, yes)
    run_plans(state, pending)


def confirm(plans: list[ProfilePlan], yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise ConfirmationRequired("Not asking for confirmation without a terminal; pass --yes to apply.")
    removals = sum(p.removals for p in plans)
    created = sum(1 for p in plans if p.create)
    notes = []
    if removals:
        notes.append(f"removes {removals} entr{'y' if removals == 1 else 'ies'}")
    if created:
        notes.append(f"creates {created} profile(s)")
    question = "Apply these changes?" + (f" This {' and '.join(notes)}." if notes else "")
    click.confirm(question, abort=True, err=True)


def run_plans(state: State, plans: list[ProfilePlan]) -> list[ApplyResult]:
    executor = Executor(state.client, state.planner)
    results = []
    for plan in plans:
        results.append(_apply_one(state, executor, plan))
    if state.json:
        emit_json({"profiles": [result_to_json(r) for r in results]})
    else:
        for result in results:
            for line in render_result(result):
                click.echo(line, err=not result.ok)
    if any(r.partial for r in results):
        sys.exit(EXIT_PARTIAL)
    if not all(r.ok for r in results):
        sys.exit(EXIT_ERROR)
    return results


def _apply_one(state: State, executor: Executor, plan: ProfilePlan) -> ApplyResult:
    if len(plan.ops) < PROGRESS_BAR_MIN_OPS or state.json or not sys.stderr.isatty():
        with working(f"Applying changes to {plan.key}"):
            return executor.apply(plan)
    with (
        status.paused(),
        click.progressbar(
            length=len(plan.ops), label=f"{plan.key}: writing", file=sys.stderr, show_eta=True, show_pos=True
        ) as bar,
    ):

        def progress(op: Op, error: Optional[str]) -> None:
            bar.update(1)

        return executor.apply(plan, progress)
