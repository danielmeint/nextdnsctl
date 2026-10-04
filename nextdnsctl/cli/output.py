"""Rendering plans and results, as text for people and JSON for scripts."""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

import click

from ..executor import ApplyResult
from ..model import Diff
from ..planner import ProfilePlan
from .status import status

PREVIEW = 20


def emit_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _style(text: str, **kwargs: Any) -> str:
    return click.style(text, **kwargs) if sys.stdout.isatty() else text


def _entry_label(section: str, entry: dict[str, Any]) -> str:
    if section == "rewrites":
        return f"{entry['name']} → {entry['content']}"
    key = entry.get("id", "?")
    flags = []
    if entry.get("active") is False:
        flags.append("inactive")
    if entry.get("recreation") is True:
        flags.append("recreation")
    return f"{key} ({', '.join(flags)})" if flags else str(key)


def _fmt_value(value: Any) -> str:
    if value is None:
        return "unset"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def render_diff(diff: Diff, verbose: bool = False, indent: str = "  ") -> list[str]:
    lines = []
    limit = None if verbose else PREVIEW
    for change in diff.arrays:
        section = change.spec.name
        counts = []
        if change.added:
            counts.append(_style(f"+{len(change.added)}", fg="green"))
        if change.updated:
            counts.append(_style(f"~{len(change.updated)}", fg="yellow"))
        if change.removed:
            counts.append(_style(f"−{len(change.removed)}", fg="red"))
        lines.append(f"{indent}{section}: {' '.join(counts)} ({len(change.before)} → {len(change.after)} entries)")
        items = (
            [("+", "green", _entry_label(section, e)) for e in change.added]
            + [("~", "yellow", _entry_label(section, a)) for _, a in change.updated]
            + [("−", "red", _entry_label(section, e)) for e in change.removed]
        )
        for sign, color, label in items[:limit]:
            lines.append(f"{indent}  {_style(sign + ' ' + label, fg=color)}")
        if limit is not None and len(items) > limit:
            lines.append(f"{indent}  … {len(items) - limit} more (use -v to list all)")
    for value in diff.values:
        name = ".".join(value.path)
        lines.append(f"{indent}{name}: {_fmt_value(value.before)} → {_style(_fmt_value(value.after), bold=True)}")
    return lines


def render_plan(plan: ProfilePlan, verbose: bool = False) -> list[str]:
    title = f"Profile {plan.key}" + (f" ({plan.profile_id})" if plan.profile_id else "")
    if plan.create:
        lines = [_style(f"{title}: will be created", bold=True)]
        lines += render_diff(plan.diff, verbose)
        return lines
    if not plan.has_changes:
        return [f"{title}: no changes"]
    lines = [_style(title, bold=True)]
    lines += render_diff(plan.diff, verbose)
    writes = []
    if plan.patch:
        writes.append("1 atomic update")
    if plan.ops:
        writes.append(f"{len(plan.ops)} individual write(s)")
    summary = f"  Writes: {' + '.join(writes)}"
    if plan.incremental_sections:
        summary += f" ({', '.join(plan.incremental_sections)} too large for one request)"
    if plan.estimated_seconds >= 30:
        summary += f", about {_duration(plan.estimated_seconds)} at NextDNS's rate limit"
    lines.append(summary)
    return lines


def plan_to_json(plan: ProfilePlan) -> dict[str, Any]:
    return {
        "key": plan.key,
        "name": plan.name,
        "id": plan.profile_id,
        "create": plan.create,
        "changes": diff_to_json(plan.diff),
        "writes": {
            "atomic_update": bool(plan.patch),
            "individual": len(plan.ops),
            "incremental_sections": plan.incremental_sections,
            "estimated_seconds": plan.estimated_seconds,
        },
        "warnings": plan.warnings,
    }


def diff_to_json(diff: Diff) -> dict[str, Any]:
    return {
        "arrays": [
            {
                "section": c.spec.name,
                "added": c.added,
                "updated": [{"before": b, "after": a} for b, a in c.updated],
                "removed": c.removed,
            }
            for c in diff.arrays
        ],
        "values": [{"path": ".".join(v.path), "before": v.before, "after": v.after} for v in diff.values],
    }


def result_to_json(result: ApplyResult) -> dict[str, Any]:
    return {
        "key": result.plan.key,
        "id": result.profile_id,
        "created": result.created,
        "updated": result.patched,
        "individual_writes": result.ops_done,
        "failures": [{"what": w, "error": e} for w, e in result.failures],
        "aborted": result.aborted,
        "drift": diff_to_json(result.drift) if result.drift else None,
        "ok": result.ok,
    }


def render_result(result: ApplyResult) -> list[str]:
    name = result.plan.key
    if result.ok:
        what = []
        if result.created:
            what.append("created")
        if result.patched:
            what.append("updated")
        if result.ops_done:
            what.append(f"{result.ops_done} individual write(s)")
        return [_style(f"✓ {name}: {', '.join(what) or 'nothing to do'}", fg="green")]
    lines = [_style(f"✗ {name}: not fully applied", fg="red", bold=True)]
    for failed, why in result.failures[:PREVIEW]:
        lines.append(f"  {failed}: {why}")
    if len(result.failures) > PREVIEW:
        lines.append(f"  … {len(result.failures) - PREVIEW} more failures")
    if result.aborted:
        lines.append(f"  stopped early: {result.aborted}")
    if result.drift:
        lines.append("  the profile doesn't match the file afterwards (changed by someone else meanwhile?):")
        lines += render_diff(result.drift, indent="    ")
    if result.partial:
        lines.append("  Some changes were applied. Run the same command again to finish; it only sends what's missing.")
    return lines


def warn(message: str) -> None:
    with status.suspended():
        click.echo("warning: " + message, err=True)


def info(message: Optional[str] = None) -> None:
    with status.suspended():
        click.echo(message or "", err=True)
