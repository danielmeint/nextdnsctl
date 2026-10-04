"""Turning live profiles into a nextdns.yaml document (`nextdnsctl pull`)."""

from __future__ import annotations

from typing import Any

import yaml

from .config import KNOWN_VALUES
from .model import ARRAY_SPECS, OPAQUE_PATHS, Path, canonicalize

HEADER = """\
# NextDNS profiles, managed with nextdnsctl (https://github.com/danielmeint/nextdnsctl).
#
#   nextdnsctl plan    show what would change
#   nextdnsctl apply   make the profiles match this file
#
# A section that is present is managed; a section that is absent is left alone.
# Lists are complete: entries missing here are removed from NextDNS on apply.
"""

_DAY = 86400
_DURATION_UNITS = [("y", 365 * _DAY), ("w", 7 * _DAY), ("d", _DAY), ("h", 3600), ("m", 60)]


def profile_document(api_profile: dict[str, Any], pin_id: bool = True) -> dict[str, Any]:
    """The YAML body for one profile."""
    live = canonicalize(api_profile)
    body: dict[str, Any] = {}
    if pin_id:
        body["id"] = api_profile["id"]
    for section in ("security", "privacy", "parentalControl", "settings"):
        if isinstance(live.get(section), dict):
            body[section] = _object((section,), live[section])
    for name in ("denylist", "allowlist"):
        body[name] = [_domain_entry(e) for e in live.get(name) or []]
    body["rewrites"] = [{"name": r["name"], "content": r["content"]} for r in live.get("rewrites") or []]
    return body


class DuplicateNameError(ValueError):
    pass


def document(api_profiles: list[dict[str, Any]], pin_ids: bool = True) -> str:
    seen: dict[str, str] = {}
    for profile in api_profiles:
        lowered = profile["name"].lower()
        if lowered in seen:
            raise DuplicateNameError(
                f"profiles {seen[lowered]} and {profile['id']} are both named {profile['name']!r}; "
                "rename one in NextDNS, or pull them one at a time"
            )
        seen[lowered] = profile["id"]
    profiles = {p["name"]: profile_document(p, pin_ids) for p in api_profiles}
    text = yaml.dump(
        {"version": 1, "profiles": profiles},
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return HEADER + "\n" + text


def _object(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in value.items():
        child_path = path + (key,)
        spec = ARRAY_SPECS.get(child_path)
        if spec is not None:
            result[key] = [_catalog_entry(spec.defaults, spec.key, e) for e in child or []]
        elif isinstance(child, dict) and child_path not in OPAQUE_PATHS:
            result[key] = _object(child_path, child)
        elif child_path == ("settings", "logs", "retention") and isinstance(child, int):
            result[key] = _format_duration(child)
        elif child_path in KNOWN_VALUES or isinstance(child, (bool, int, str)) or child_path in OPAQUE_PATHS:
            result[key] = child
    return result


def _domain_entry(entry: dict[str, Any]) -> Any:
    return entry["id"] if entry.get("active", True) else {"domain": entry["id"], "active": False}


def _catalog_entry(defaults: dict[str, Any], key: str, entry: dict[str, Any]) -> Any:
    extra = {k: v for k, v in entry.items() if k != key and defaults.get(k) != v}
    return {key: entry[key], **extra} if extra else entry[key]


def _format_duration(seconds: int) -> Any:
    for unit, size in _DURATION_UNITS:
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return seconds


class _Dumper(yaml.SafeDumper):
    """Indent list items under their key, like most hand-written YAML."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)
