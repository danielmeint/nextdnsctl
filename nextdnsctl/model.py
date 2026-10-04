"""Canonical profile state, overlays and diffs.

A *canonical* profile is the API's profile object with read-only and device-specific
fields removed (A12) and defaults filled in, so two states can be compared directly.

An *overlay* has the same shape but contains only what the user manages. Applying it to
the live state gives the *target* state:

- objects merge key by key (only keys present in the overlay are managed),
- arrays replace wholesale (a managed array is authoritative), but fields an entry
  doesn't mention keep their live value (e.g. a service's `recreation` flag).

That mirrors how the API itself treats a profile PATCH (A2).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

Path = tuple[str, ...]

# Top-level keys of a profile we never manage or emit. `setup` holds the linked-IP
# update token, which is a credential (A12).
UNMANAGED_TOP_LEVEL = {"id", "fingerprint", "role", "setup"}

# Objects treated as a single opaque value (replaced, not merged).
OPAQUE_PATHS: set[Path] = {("parentalControl", "recreation")}


@dataclass(frozen=True)
class ArraySpec:
    """How an array section of a profile behaves."""

    path: Path
    key: str = "id"
    defaults: dict[str, Any] = field(default_factory=dict)  # writable fields and their defaults
    readonly: tuple[str, ...] = ()
    catalog: Optional[str] = None  # public catalog endpoint with valid ids (A15)
    domains: bool = False  # entries are domain names
    item_endpoints: bool = False  # supports per-item POST/PATCH/DELETE for incremental updates
    patchable: bool = True  # can be set through the profile PATCH (A1, A10)

    @property
    def name(self) -> str:
        return ".".join(self.path)


ARRAY_SPECS: dict[Path, ArraySpec] = {
    spec.path: spec
    for spec in [
        ArraySpec(("denylist",), defaults={"active": True}, domains=True, item_endpoints=True),
        ArraySpec(("allowlist",), defaults={"active": True}, domains=True, item_endpoints=True),
        ArraySpec(("security", "tlds"), catalog="security/tlds", readonly=("spamhaus",)),
        ArraySpec(
            ("privacy", "blocklists"),
            catalog="privacy/blocklists",
            readonly=("name", "website", "description", "entries", "updatedOn"),
        ),
        ArraySpec(("privacy", "natives"), catalog="privacy/natives"),
        ArraySpec(
            ("parentalControl", "services"),
            defaults={"active": True, "recreation": False},
            readonly=("website",),
            catalog="parentalControl/services",
        ),
        ArraySpec(
            ("parentalControl", "categories"),
            defaults={"active": True, "recreation": False},
            catalog="parentalControl/categories",
        ),
        # A10: rewrites have server-assigned ids, no PUT, and are not part of the profile PATCH.
        ArraySpec(("rewrites",), key="name", readonly=("id", "type"), patchable=False, item_endpoints=True),
    ]
}

TOP_LEVEL_SECTIONS = ("name", "security", "privacy", "parentalControl", "settings", "denylist", "allowlist", "rewrites")


# ── canonical form ─────────────────────────────────────────────────────────


def canonicalize(api_profile: dict[str, Any]) -> dict[str, Any]:
    """Turn a profile as returned by GET /profiles/:id into canonical form."""
    result: dict[str, Any] = {}
    for key, value in api_profile.items():
        if key in UNMANAGED_TOP_LEVEL:
            continue
        result[key] = _canonical_value((key,), value)
    return result


def _canonical_value(path: Path, value: Any) -> Any:
    spec = ARRAY_SPECS.get(path)
    if spec is not None:
        return [canonical_entry(spec, entry) for entry in value or []]
    if isinstance(value, dict) and path not in OPAQUE_PATHS:
        return {k: _canonical_value(path + (k,), v) for k, v in value.items()}
    return copy.deepcopy(value)


def canonical_entry(spec: ArraySpec, entry: Any, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Normalise one array entry: drop read-only fields, fill unspecified fields from `base` or defaults."""
    if not isinstance(entry, dict):
        entry = {spec.key: entry}
    result = {k: v for k, v in entry.items() if k not in spec.readonly}
    for name, default in spec.defaults.items():
        if name not in result:
            result[name] = base[name] if base is not None and name in base else default
    return result


def rewrite_ids(api_profile: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(name, content, server id) for each rewrite; ids are needed to delete them (A10)."""
    return [(r.get("name", ""), r.get("content", ""), r.get("id", "")) for r in api_profile.get("rewrites") or []]


# ── overlays ───────────────────────────────────────────────────────────────


def apply_overlay(live: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return the target state: `live` with everything in `overlay` applied."""
    target = copy.deepcopy(live)
    _merge(target, overlay, ())
    return target


def _merge(target: dict[str, Any], overlay: dict[str, Any], path: Path) -> None:
    for key, value in overlay.items():
        child = path + (key,)
        spec = ARRAY_SPECS.get(child)
        if spec is not None:
            live_entries = {e.get(spec.key): e for e in target.get(key) or [] if isinstance(e, dict)}
            entries = []
            for entry in value:
                entry_key = entry.get(spec.key) if isinstance(entry, dict) else entry
                # Rewrites can repeat a name, so they never inherit fields from a namesake.
                base = live_entries.get(entry_key) if spec.patchable else None
                entries.append(canonical_entry(spec, entry, base))
            target[key] = entries
        elif isinstance(value, dict) and child not in OPAQUE_PATHS:
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge(target[key], value, child)
        else:
            target[key] = copy.deepcopy(value)


# ── diffs ──────────────────────────────────────────────────────────────────


@dataclass
class ArrayChange:
    spec: ArraySpec
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    updated: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # (before, after)

    @property
    def path(self) -> Path:
        return self.spec.path


@dataclass
class ValueChange:
    path: Path
    before: Any
    after: Any


@dataclass
class Diff:
    arrays: list[ArrayChange] = field(default_factory=list)
    values: list[ValueChange] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.arrays or self.values)


def diff(live: dict[str, Any], target: dict[str, Any]) -> Diff:
    """Compare two canonical states. Only paths present in `target` are considered."""
    result = Diff()
    _diff(live, target, (), result)
    return result


def _diff(live: Any, target: Any, path: Path, result: Diff) -> None:
    spec = ARRAY_SPECS.get(path)
    if spec is not None:
        change = _diff_array(spec, live or [], target or [])
        if change is not None:
            result.arrays.append(change)
        return
    if isinstance(target, dict) and path not in OPAQUE_PATHS:
        live_dict = live if isinstance(live, dict) else {}
        for key, value in target.items():
            if not path and key in UNMANAGED_TOP_LEVEL:
                continue
            _diff(live_dict.get(key), value, path + (key,), result)
        return
    if live != target:
        result.values.append(ValueChange(path, live, target))


def _diff_array(spec: ArraySpec, before: list[dict], after: list[dict]) -> Optional[ArrayChange]:
    if spec.patchable:
        before_by_key = {e[spec.key]: e for e in before}
        after_by_key = {e[spec.key]: e for e in after}
        added = [e for k, e in after_by_key.items() if k not in before_by_key]
        removed = [e for k, e in before_by_key.items() if k not in after_by_key]
        updated = [
            (before_by_key[k], e) for k, e in after_by_key.items() if k in before_by_key and before_by_key[k] != e
        ]
    else:
        # Multiset diff on the whole entry (rewrites: several records may share a name).
        remaining = [_freeze(e) for e in before]
        added = []
        for entry in after:
            frozen = _freeze(entry)
            if frozen in remaining:
                remaining.remove(frozen)
            else:
                added.append(entry)
        removed = [dict(e) for e in remaining]
        updated = []
    if not (added or removed or updated):
        return None
    return ArrayChange(spec, before, after, added, removed, updated)


def _freeze(entry: dict[str, Any]) -> tuple:
    return tuple(sorted(entry.items()))


def iter_paths(value: Any, path: Path = ()) -> Iterator[tuple[Path, Any]]:
    """Yield (path, leaf) for every non-dict leaf, treating arrays and opaque objects as leaves."""
    if isinstance(value, dict) and path not in OPAQUE_PATHS and path not in ARRAY_SPECS:
        for key, child in value.items():
            yield from iter_paths(child, path + (key,))
    else:
        yield path, value


def get_path(value: Any, path: Path) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def set_path(target: dict[str, Any], path: Path, value: Any) -> None:
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value
