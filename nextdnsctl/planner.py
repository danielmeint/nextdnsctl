"""Turning a desired state into a plan of API writes.

Write strategy (docs/v2-design.md, "Planning the writes"):

1. Every changed section that the profile PATCH accepts goes into one request, which
   NextDNS applies all-or-nothing (A1). Arrays are sent in full, values alone (A2).
2. If that body would exceed the size limit (A3), the largest domain lists are taken out
   one at a time and applied entry by entry instead, paced to the rate limit (A11).
3. Rewrites are always applied entry by entry (A10).
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from . import sources as sources_mod
from .client import MAX_BODY_BYTES, WRITE_INTERVAL, Client, NextDNSError, encode_body
from .config import ConfigError, Origin, ProfileConfig, SourceSpec
from .model import (
    ARRAY_SPECS,
    ArrayChange,
    ArraySpec,
    Diff,
    Path,
    apply_overlay,
    canonicalize,
    diff,
    get_path,
    rewrite_ids,
    set_path,
)

log = logging.getLogger(__name__)

# Leave headroom below the hard limit for anything we don't account for.
SAFE_BODY_BYTES = MAX_BODY_BYTES - 4 * 1024
SOURCE_ERROR_PREVIEW = 20


class PlanError(Exception):
    """The desired state can't be planned (unknown profile, invalid source, …)."""


@dataclass
class Op:
    """One per-entry write, used for rewrites and for lists too large for the profile PATCH."""

    kind: str  # "add" | "update" | "remove"
    section: str  # e.g. "denylist", "rewrites"
    method: str
    path: str  # relative to the profile, e.g. "denylist/example.com"
    body: Optional[dict[str, Any]]
    label: str  # what to show the user, e.g. the domain


@dataclass
class Resolved:
    """A profile's overlay with sources fetched and merged, plus where each value came from."""

    overlay: dict[str, Any]
    origins: dict[Path, Origin] = field(default_factory=dict)
    entry_origins: dict[tuple[str, str], Origin] = field(default_factory=dict)  # (list, domain) -> origin
    unchecked: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ProfilePlan:
    key: str
    name: str
    profile_id: Optional[str]  # None when the profile will be created
    resolved: Resolved
    diff: Diff = field(default_factory=Diff)
    patch: dict[str, Any] = field(default_factory=dict)
    patch_origins: dict[str, Origin] = field(default_factory=dict)  # JSON pointer prefix -> origin
    ops: list[Op] = field(default_factory=list)
    incremental_sections: list[str] = field(default_factory=list)

    @property
    def create(self) -> bool:
        return self.profile_id is None

    @property
    def has_changes(self) -> bool:
        return self.create or bool(self.diff)

    @property
    def removals(self) -> int:
        count = sum(len(c.removed) for c in self.diff.arrays)
        return count

    @property
    def writes(self) -> int:
        return (1 if self.patch else 0) + len(self.ops)

    @property
    def estimated_seconds(self) -> float:
        return self.writes * WRITE_INTERVAL

    @property
    def warnings(self) -> list[str]:
        return self.resolved.warnings


class Planner:
    def __init__(self, client: Client, *, validate_catalogs: bool = True):
        self.client = client
        self.validate_catalogs = validate_catalogs
        self._profiles: Optional[list[dict[str, Any]]] = None
        self._catalogs: dict[str, Optional[set[str]]] = {}

    # ── profiles ───────────────────────────────────────────────────────────

    def profiles(self) -> list[dict[str, Any]]:
        if self._profiles is None:
            self._profiles = self.client.list_profiles()
        return self._profiles

    def forget_profiles(self) -> None:
        self._profiles = None

    def find_profile(self, name_or_id: str, pinned_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Find a profile by pinned id, or by id or name (case-insensitive). Ambiguity is an error."""
        profiles = self.profiles()
        if pinned_id:
            for profile in profiles:
                if profile.get("id") == pinned_id:
                    return profile
            raise PlanError(f"profile id {pinned_id!r} not found")
        for profile in profiles:
            if profile.get("id") == name_or_id:
                return profile
        matches = [p for p in profiles if str(p.get("name", "")).lower() == name_or_id.lower()]
        if len(matches) > 1:
            ids = ", ".join(p["id"] for p in matches)
            raise PlanError(f"profile name {name_or_id!r} is ambiguous (matches {ids}); pin it with 'id:'")
        return matches[0] if matches else None

    def require_profile(self, name_or_id: str) -> dict[str, Any]:
        profile = self.find_profile(name_or_id)
        if profile is None:
            available = ", ".join(f"{p.get('name')!r} ({p.get('id')})" for p in self.profiles())
            raise PlanError(f"profile {name_or_id!r} not found. Available: {available or 'none'}")
        return profile

    def live(self, profile_id: str) -> dict[str, Any]:
        """The profile as returned by the API (not canonicalised)."""
        return self.client.get_profile(profile_id)

    # ── planning ───────────────────────────────────────────────────────────

    def plan(self, profile: ProfileConfig) -> ProfilePlan:
        resolved = self.resolve(profile)
        found = self.find_profile(profile.key, profile.id)
        if found is None:
            plan = ProfilePlan(profile.key, profile.key, None, resolved)
            plan.diff = diff({}, apply_overlay({}, resolved.overlay))
            return plan
        overlay = resolved.overlay
        if profile.id and found.get("name") != profile.key:
            overlay = {**overlay, "name": profile.key}  # pinned by id: the key is the name
        resolved.overlay = overlay
        return self.plan_against(profile.key, found["id"], resolved, self.live(found["id"]))

    def plan_against(self, key: str, profile_id: str, resolved: Resolved, api_profile: dict[str, Any]) -> ProfilePlan:
        live = canonicalize(api_profile)
        self._check_unchecked(resolved, live)
        self._check_catalog_ids(resolved, live)
        target = apply_overlay(live, resolved.overlay)
        plan = ProfilePlan(key, str(api_profile.get("name", key)), profile_id, resolved)
        plan.diff = diff(live, target)
        self._build_writes(plan, api_profile)
        return plan

    # ── resolving the overlay ──────────────────────────────────────────────

    def resolve(self, profile: ProfileConfig) -> Resolved:
        resolved = Resolved(overlay=dict(profile.overlay), origins=dict(profile.origins))
        resolved.unchecked = list(profile.unchecked)
        for list_name, spec in profile.lists.items():
            entries: dict[str, tuple[bool, Origin]] = {}

            def add(domain: str, active: bool, origin: Origin) -> None:
                if domain in entries and entries[domain][0] != active:
                    other = entries[domain][1]
                    raise ConfigError(
                        f"{domain!r} is in {list_name} as both active and inactive (also at {other})",
                        origin.file,
                        origin.line,
                    )
                entries.setdefault(domain, (active, origin))

            for domain, active, origin in spec.entries:
                add(domain, active, origin)
            for source in spec.sources:
                for domain, origin in self._read_source(source, resolved.warnings):
                    add(domain, True, origin)
            resolved.overlay[list_name] = [{"id": d, "active": a} for d, (a, _) in entries.items()]
            for domain, (_, origin) in entries.items():
                resolved.entry_origins[(list_name, domain)] = origin
        return resolved

    def _read_source(self, source: SourceSpec, warnings: list[str]) -> list[tuple[str, Origin]]:
        try:
            result = sources_mod.load(source.location, source.format)
        except sources_mod.SourceError as e:
            raise ConfigError(f"source: {e}", source.origin.file, source.origin.line)
        if result.errors:
            lines = [
                f"  {source.location}:{e.line}: {e.reason}: {e.text}" for e in result.errors[:SOURCE_ERROR_PREVIEW]
            ]
            more = len(result.errors) - SOURCE_ERROR_PREVIEW
            if more > 0:
                lines.append(f"  … and {more} more")
            summary = f"{len(result.errors)} invalid line(s) in {source.location} (format: {result.format})"
            if not source.skip_invalid:
                raise ConfigError(
                    summary + ":\n" + "\n".join(lines) + "\nFix them, or set 'skip_invalid: true' on this source.",
                    source.origin.file,
                    source.origin.line,
                )
            warnings.append(summary + ", skipped:\n" + "\n".join(lines))
        return [(domain, Origin(source.location, line)) for domain, line in result.domains]

    def _check_unchecked(self, resolved: Resolved, live: dict[str, Any]) -> None:
        for path in resolved.unchecked:
            origin = resolved.origins.get(path)
            wanted = get_path(resolved.overlay, path)
            current = get_path(live, path)
            name = ".".join(path)
            if current is None:
                raise ConfigError(
                    f"unknown setting {name!r}",
                    origin.file if origin else None,
                    origin.line if origin else None,
                )
            if type(current) is not type(wanted) and not (isinstance(current, dict) and isinstance(wanted, dict)):
                raise ConfigError(
                    f"{name!r} must be a {type(current).__name__} (got {wanted!r})",
                    origin.file if origin else None,
                    origin.line if origin else None,
                )

    def _check_catalog_ids(self, resolved: Resolved, live: dict[str, Any]) -> None:
        if not self.validate_catalogs:
            return
        for path, spec in ARRAY_SPECS.items():
            if spec.catalog is None:
                continue
            wanted = get_path(resolved.overlay, path)
            if not wanted:
                continue
            valid = self._catalog(spec.catalog)
            if valid is None:
                continue
            existing = {e.get(spec.key) for e in get_path(live, path) or []}
            for entry in wanted:
                entry_id = entry[spec.key]
                if entry_id in valid or entry_id in existing:
                    continue
                hint = difflib.get_close_matches(entry_id, sorted(valid), n=1)
                suggestion = f" Did you mean {hint[0]!r}?" if hint else ""
                origin = resolved.origins.get(path)
                raise ConfigError(
                    f"unknown id {entry_id!r} in {spec.name}.{suggestion} "
                    f"See 'nextdnsctl catalog {spec.path[-1]}'.",
                    origin.file if origin else None,
                    origin.line if origin else None,
                )

    def _catalog(self, endpoint: str) -> Optional[set[str]]:
        if endpoint not in self._catalogs:
            try:
                self._catalogs[endpoint] = {str(e["id"]) for e in self.client.catalog(endpoint) if "id" in e}
            except NextDNSError as e:
                log.warning("Could not load the %s catalog (%s); ids will be checked by NextDNS instead", endpoint, e)
                self._catalogs[endpoint] = None
        return self._catalogs[endpoint]

    # ── writes ─────────────────────────────────────────────────────────────

    def _build_writes(self, plan: ProfilePlan, api_profile: dict[str, Any]) -> None:
        patch: dict[str, Any] = {}
        origins: dict[str, Origin] = {}
        incremental: list[ArrayChange] = []

        for value in plan.diff.values:
            set_path(patch, value.path, value.after)
            origin = plan.resolved.origins.get(value.path)
            if origin:
                origins["/" + "/".join(value.path)] = origin

        patchable: list[ArrayChange] = []
        for change in plan.diff.arrays:
            if change.spec.patchable:
                patchable.append(change)
            else:
                incremental.append(change)

        for change in patchable:
            set_path(patch, change.path, [_api_entry(change.spec, e) for e in change.after])

        # Too big for one request (A3)? Move the largest domain lists out, one at a time.
        while patch and len(encode_body(patch)) > SAFE_BODY_BYTES:
            movable = [c for c in patchable if c.spec.item_endpoints and get_path(patch, c.path) is not None]
            if not movable:
                raise PlanError(
                    f"the changes to profile {plan.key!r} don't fit in one request "
                    f"({len(encode_body(patch))} bytes, limit {MAX_BODY_BYTES}) and can't be split further"
                )
            largest = max(movable, key=lambda c: len(encode_body(get_path(patch, c.path))))
            _remove_path(patch, largest.path)
            incremental.append(largest)
            plan.incremental_sections.append(largest.spec.name)

        for change in patchable:
            if get_path(patch, change.path) is None:
                continue
            for index, entry in enumerate(change.after):
                key = str(entry.get(change.spec.key))
                origin = plan.resolved.entry_origins.get((change.spec.name, key)) or plan.resolved.origins.get(
                    change.path
                )
                if origin:
                    origins[f"/{'/'.join(change.path)}/{index}"] = origin

        plan.patch = patch
        plan.patch_origins = origins
        plan.ops = _incremental_ops(incremental, api_profile)


def _api_entry(spec: ArraySpec, entry: dict[str, Any]) -> dict[str, Any]:
    """An array entry as sent to the API. `active: true` is the default and omitted (A7)."""
    result = dict(entry)
    if spec.domains and result.get("active") is True:
        del result["active"]
    return result


def _remove_path(target: dict[str, Any], path: Path) -> None:
    parents = [target]
    for key in path[:-1]:
        parents.append(parents[-1][key])
    del parents[-1][path[-1]]
    for depth in range(len(path) - 1, 0, -1):
        if not parents[depth]:
            del parents[depth - 1][path[depth - 1]]


def _incremental_ops(changes: list[ArrayChange], api_profile: dict[str, Any]) -> list[Op]:
    ops: list[Op] = []
    for change in changes:
        spec = change.spec
        section = spec.name.replace(".", "/")
        if spec.path == ("rewrites",):
            available = rewrite_ids(api_profile)
            for entry in change.removed:
                for i, (name, content, rewrite_id) in enumerate(available):
                    if name == entry["name"] and content == entry["content"]:
                        ops.append(
                            Op("remove", "rewrites", "DELETE", f"rewrites/{rewrite_id}", None, _rewrite_label(entry))
                        )
                        del available[i]
                        break
            for entry in change.added:
                body = {"name": entry["name"], "content": entry["content"]}
                ops.append(Op("add", "rewrites", "POST", "rewrites", body, _rewrite_label(entry)))
            continue
        for entry in change.added:
            key = entry[spec.key]
            ops.append(Op("add", spec.name, "POST", section, {spec.key: key, **_fields(spec, entry)}, key))
        for before, after in change.updated:
            key = after[spec.key]
            ops.append(Op("update", spec.name, "PATCH", f"{section}/{key}", _fields(spec, after), key))
        for entry in change.removed:
            key = entry[spec.key]
            ops.append(Op("remove", spec.name, "DELETE", f"{section}/{key}", None, key))
    return ops


def _fields(spec: ArraySpec, entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != spec.key}


def _rewrite_label(entry: dict[str, Any]) -> str:
    return f"{entry['name']} → {entry['content']}"
