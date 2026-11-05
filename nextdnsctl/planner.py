"""Turning a desired state into a plan of API writes.

Write strategy (docs/v2-design.md, "Planning the writes"):

1. Every changed section that the profile PATCH accepts goes into a PATCH, which NextDNS
   applies all-or-nothing (A1). Arrays are sent in full, values alone (A2).
2. If they don't all fit in one request (A3), they are packed into as few PATCHes as fit.
3. A domain list too large for any one request is sent as one PATCH holding every entry
   it keeps plus as many new ones as fit, and the rest are added entry by entry, paced to
   the rate limit (A11). If even the kept entries don't fit, it is applied entry by entry,
   unless `allow_gaps` lets some kept entries be dropped briefly and re-added.
4. A list with a single change uses the entry's own endpoint: same cost, and it can't
   overwrite an entry someone else added meanwhile, as a full-list PATCH would.
5. Rewrites are always applied entry by entry (A10).
"""

from __future__ import annotations

import copy
import difflib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

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
PREFETCH_WORKERS = 4
# Only suggest --allow-gaps when it saves at least this many writes (about a minute).
GAP_HINT_MIN_SAVING = 60
LIST_ENDPOINTS = {"denylist", "allowlist", "rewrites"}


def _fetching_message(profiles: list[dict[str, Any]], done: int) -> str:
    if len(profiles) == 1:
        return f"Fetching profile {profiles[0].get('name', profiles[0]['id'])}"
    return f"Fetching {len(profiles)} profiles ({done}/{len(profiles)} done)"


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
class Patch:
    """One atomic PATCH /profiles/:id."""

    body: dict[str, Any]
    origins: dict[str, Origin] = field(default_factory=dict)  # JSON pointer prefix -> origin
    sections: list[str] = field(default_factory=list)


@dataclass
class GapHint:
    """What --allow-gaps would change for a list that is applied entry by entry."""

    section: str
    writes: int  # writes with --allow-gaps
    gap: int  # kept entries that would be missing until re-added


@dataclass
class Resolved:
    """A profile's overlay with sources fetched and merged, plus where each value came from."""

    overlay: dict[str, Any]
    origins: dict[Path, Origin] = field(default_factory=dict)
    entry_origins: dict[tuple[str, str], Origin] = field(default_factory=dict)  # (list, domain) -> origin
    unchecked: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report: dict[str, list[str]] = field(default_factory=dict)  # quick edits: what was skipped and why


@dataclass
class ProfilePlan:
    key: str
    name: str
    profile_id: Optional[str]  # None when the profile will be created
    resolved: Resolved
    diff: Diff = field(default_factory=Diff)
    patches: list[Patch] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    incremental_sections: list[str] = field(default_factory=list)  # lists applied entry by entry
    partial_sections: list[str] = field(default_factory=list)  # one PATCH, then the rest entry by entry
    gap: int = 0  # kept entries dropped by a PATCH and re-added afterwards (allow_gaps)
    gap_hints: list[GapHint] = field(default_factory=list)
    # The API state the plan was made from, when it was read, and how to plan again from a
    # fresh copy (the executor re-checks before a write that could overwrite other changes).
    api_profile: dict[str, Any] = field(default_factory=dict)
    read_at: float = 0.0
    rebase: Optional[Callable[[dict[str, Any]], "ProfilePlan"]] = field(default=None, repr=False)

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
        return len(self.patches) + len(self.ops)

    @property
    def full_list_paths(self) -> list[Path]:
        """Arrays some PATCH replaces wholesale, which would overwrite concurrent changes to them."""
        return [path for path in ARRAY_SPECS if any(get_path(p.body, path) is not None for p in self.patches)]

    @property
    def estimated_seconds(self) -> float:
        return self.writes * WRITE_INTERVAL

    @property
    def warnings(self) -> list[str]:
        return self.resolved.warnings


class Planner:
    def __init__(
        self,
        client: Client,
        *,
        validate_catalogs: bool = True,
        allow_gaps: bool = False,
        progress: Optional[Callable[[str], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.client = client
        self.validate_catalogs = validate_catalogs
        self.allow_gaps = allow_gaps
        self.clock: Callable[[], float] = clock or client.clock
        self.progress = progress or (lambda message: None)
        self._profiles: Optional[list[dict[str, Any]]] = None
        self._catalogs: dict[str, Optional[set[str]]] = {}
        self._prefetched: dict[str, dict[str, Any]] = {}

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
        """The profile as returned by the API (not canonicalised).

        A prefetched copy is used once; later calls (e.g. verifying after apply) fetch afresh.
        """
        if profile_id in self._prefetched:
            return self._prefetched.pop(profile_id)
        return self.client.get_profile(profile_id)

    def live_for(self, profile: dict[str, Any], sections: Any) -> dict[str, Any]:
        """The parts of a profile needed to plan changes to `sections` (top-level keys).

        Changes that only touch the denylist, allowlist or rewrites read those endpoints
        instead of the whole profile, which NextDNS throttles much sooner (A11).
        """
        wanted = set(sections)
        if wanted and wanted <= LIST_ENDPOINTS:
            partial: dict[str, Any] = {"id": profile["id"], "name": profile.get("name", profile["id"])}
            for section in sorted(wanted):
                partial[section] = self.client.get_items(profile["id"], section)
            return partial
        return self.live(profile["id"])

    def prefetch(self, profiles: list[dict[str, Any]]) -> None:
        """Fetch several profiles in parallel. NextDNS sometimes takes many seconds per
        profile, so the wait becomes the slowest profile rather than the sum."""
        pending = [p for p in profiles if p["id"] not in self._prefetched]
        if not pending:
            return
        done = 0
        self.progress(_fetching_message(pending, done))
        with ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(pending))) as pool:
            futures = {pool.submit(self.client.get_profile, p["id"]): p for p in pending}
            for future in as_completed(futures):
                self._prefetched[futures[future]["id"]] = future.result()
                done += 1
                self.progress(_fetching_message(pending, done))

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
        return self.plan_with(key, profile_id, lambda api: resolved, api_profile)

    def plan_with(
        self,
        key: str,
        profile_id: str,
        make_resolved: Callable[[dict[str, Any]], Resolved],
        api_profile: dict[str, Any],
    ) -> ProfilePlan:
        """Plan against `api_profile`, with a desired state that may depend on it (quick edits)."""
        resolved = make_resolved(api_profile)
        live = canonicalize(api_profile)
        self._check_unchecked(resolved, live)
        self._check_catalog_ids(resolved, live)
        target = apply_overlay(live, resolved.overlay)
        plan = ProfilePlan(key, str(api_profile.get("name", key)), profile_id, resolved)
        plan.diff = diff(live, target)
        plan.api_profile = api_profile
        plan.read_at = self.clock()
        plan.rebase = lambda fresh: self.plan_with(key, profile_id, make_resolved, fresh)
        self._build_writes(plan, api_profile)
        return plan

    def refresh(self, plan: ProfilePlan) -> Optional[dict[str, Any]]:
        """Re-read what the plan's PATCHes would replace wholesale.

        Returns the API profile with fresh copies of those sections if any of them changed
        since the plan was made, otherwise None.
        """
        paths = plan.full_list_paths
        if plan.profile_id is None or not paths:
            return None
        sections = {path[0] for path in paths}
        fresh = self.live_for({"id": plan.profile_id, "name": plan.name}, sections)
        before, after = canonicalize(plan.api_profile), canonicalize(fresh)
        if all(_same_entries(get_path(before, p), get_path(after, p)) for p in paths):
            return None
        return {**plan.api_profile, **{k: v for k, v in fresh.items() if k in sections}}

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
        self.progress(f"Reading {source.location}")
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
        units: list[Patch] = []  # one per changed section, packed into PATCHes below
        partial: list[Patch] = []  # large lists: PATCHes of their own
        restores: list[Op] = []  # kept entries a PATCH drops (allow_gaps), re-added first
        incremental: list[ArrayChange] = []
        adds: list[Op] = []

        values: dict[str, Any] = {}
        for value in plan.diff.values:
            set_path(values, value.path, value.after)
        if values:
            units.append(Patch(values, sections=sorted(values)))

        for change in plan.diff.arrays:
            spec = change.spec
            if not spec.patchable or (spec.item_endpoints and _change_count(change) == 1):
                incremental.append(change)
                continue
            body: dict[str, Any] = {}
            set_path(body, change.path, [_api_entry(spec, e) for e in change.after])
            if not spec.item_endpoints or len(encode_body(body)) <= SAFE_BODY_BYTES:
                units.append(Patch(body, sections=[spec.name]))
                continue
            split = self._split_large(change)
            if split is None:
                incremental.append(change)
                plan.incremental_sections.append(spec.name)
                continue
            patch, gap_entries, rest = split
            partial.append(patch)
            plan.partial_sections.append(spec.name)
            plan.gap += len(gap_entries)
            restores += [_add_op(spec, e) for e in gap_entries]
            adds += [_add_op(spec, e) for e in rest]

        plan.patches = _pack(units, plan.key) + partial
        for patch in plan.patches:
            patch.origins = _origins(patch.body, plan)
        plan.ops = restores + _incremental_ops(incremental, api_profile) + adds
        for change in plan.diff.arrays:
            if change.spec.name in plan.incremental_sections:
                hint = self._gap_hint(change)
                if hint:
                    plan.gap_hints.append(hint)

    def _split_large(self, change: ArrayChange) -> Optional[tuple[Patch, list[dict], list[dict]]]:
        """For a list too large for one request: one PATCH plus per-entry adds, if that
        takes fewer writes than applying every change entry by entry.

        The PATCH holds every entry the list keeps, so none of them is ever missing, plus as
        many new entries as fit. When the kept entries alone don't fit, `allow_gaps` lets
        the PATCH drop some of them; they are re-added right after it.

        Returns (patch, kept entries to re-add, new entries to add) or None.
        """
        spec = change.spec
        kept, new = _kept_and_new(change)
        cost_incremental = _change_count(change)
        fit_kept = _prefix_fitting(spec, kept, SAFE_BODY_BYTES)
        if fit_kept < len(kept) and not self.allow_gaps:
            return None
        if fit_kept < len(kept):
            body_entries, gap, rest = kept[:fit_kept], kept[fit_kept:], new
        else:
            fit_new = _prefix_fitting(spec, new, SAFE_BODY_BYTES - _list_bytes(spec, kept))
            body_entries, gap, rest = kept + new[:fit_new], [], new[fit_new:]
        if 1 + len(gap) + len(rest) >= cost_incremental:
            return None
        body: dict[str, Any] = {}
        set_path(body, change.path, [_api_entry(spec, e) for e in body_entries])
        return Patch(body, sections=[spec.name]), gap, rest

    def _gap_hint(self, change: ArrayChange) -> Optional[GapHint]:
        if self.allow_gaps or not change.spec.item_endpoints:
            return None
        kept, new = _kept_and_new(change)
        fit = _prefix_fitting(change.spec, kept, SAFE_BODY_BYTES)
        if fit >= len(kept):
            return None
        writes = 1 + (len(kept) - fit) + len(new)
        if _change_count(change) - writes < GAP_HINT_MIN_SAVING:
            return None
        return GapHint(change.spec.name, writes, len(kept) - fit)


def _change_count(change: ArrayChange) -> int:
    return len(change.added) + len(change.removed) + len(change.updated)


def _kept_and_new(change: ArrayChange) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    key = change.spec.key
    before = {e[key] for e in change.before}
    return [e for e in change.after if e[key] in before], [e for e in change.after if e[key] not in before]


def _list_bytes(spec: ArraySpec, entries: list[dict[str, Any]]) -> int:
    """Bytes the entries add to a JSON array, separators included."""
    return sum(len(encode_body(_api_entry(spec, e))) + 1 for e in entries)


def _prefix_fitting(spec: ArraySpec, entries: list[dict[str, Any]], budget: int) -> int:
    """How many leading entries fit in a PATCH of this list within `budget` bytes."""
    empty: dict[str, Any] = {}
    set_path(empty, spec.path, [])
    remaining = budget - len(encode_body(empty))
    for count, entry in enumerate(entries):
        remaining -= len(encode_body(_api_entry(spec, entry))) + 1
        if remaining < 0:
            return count
    return len(entries)


def _pack(units: list[Patch], key: str) -> list[Patch]:
    """Pack section updates into as few PATCHes as fit (first fit, largest first)."""
    bins: list[Patch] = []
    for unit in sorted(units, key=lambda u: len(encode_body(u.body)), reverse=True):
        size = len(encode_body(unit.body))
        if size > SAFE_BODY_BYTES:
            raise PlanError(
                f"the changes to {', '.join(unit.sections)} in profile {key!r} don't fit in one request "
                f"({size} bytes, limit {MAX_BODY_BYTES}) and can't be split"
            )
        for patch in bins:
            merged = _merged(patch.body, unit.body)
            if len(encode_body(merged)) <= SAFE_BODY_BYTES:
                patch.body = merged
                patch.sections += unit.sections
                break
        else:
            bins.append(Patch(copy.deepcopy(unit.body), sections=list(unit.sections)))
    return bins


def _merged(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(a)
    for key, value in b.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merged(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _origins(body: dict[str, Any], plan: ProfilePlan) -> dict[str, Origin]:
    """JSON pointer prefixes in a PATCH body mapped to where the values came from (A9)."""
    origins: dict[str, Origin] = {}
    for value in plan.diff.values:
        origin = plan.resolved.origins.get(value.path)
        if origin and get_path(body, value.path) is not None:
            origins["/" + "/".join(value.path)] = origin
    for path, spec in ARRAY_SPECS.items():
        entries = get_path(body, path)
        if entries is None:
            continue
        section_origin = plan.resolved.origins.get(path)
        for index, entry in enumerate(entries):
            origin = plan.resolved.entry_origins.get((spec.name, str(entry.get(spec.key)))) or section_origin
            if origin:
                origins[f"/{'/'.join(path)}/{index}"] = origin
    return origins


def _same_entries(a: Optional[list[dict[str, Any]]], b: Optional[list[dict[str, Any]]]) -> bool:
    def frozen(entries: Optional[list[dict[str, Any]]]) -> list[str]:
        return sorted(json.dumps(e, sort_keys=True) for e in entries or [])

    return frozen(a) == frozen(b)


def _add_op(spec: ArraySpec, entry: dict[str, Any]) -> Op:
    key = entry[spec.key]
    return Op("add", spec.name, "POST", spec.name.replace(".", "/"), {spec.key: key, **_fields(spec, entry)}, key)


def _api_entry(spec: ArraySpec, entry: dict[str, Any]) -> dict[str, Any]:
    """An array entry as sent to the API. `active: true` is the default and omitted (A7)."""
    result = dict(entry)
    if spec.domains and result.get("active") is True:
        del result["active"]
    return result


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
            ops.append(_add_op(spec, entry))
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
