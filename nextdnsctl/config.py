"""The declarative profile file (nextdns.yaml): loading, validation and line numbers."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import idna
import yaml

from .domains import InvalidDomainError, normalize_domain
from .model import ARRAY_SPECS, Path
from .sources import FORMATS, is_url

DEFAULT_FILE = "nextdns.yaml"
SUPPORTED_VERSIONS = (1,)

# Settings whose type we know. Keys that NextDNS adds later are still accepted when the
# live profile has them (checked at plan time), so new features don't need a release,
# while typos are still caught.
KNOWN_VALUES: dict[Path, type | tuple[type, ...]] = {
    **{
        ("security", key): bool
        for key in (
            "threatIntelligenceFeeds",
            "aiThreatDetection",
            "googleSafeBrowsing",
            "cryptojacking",
            "dnsRebinding",
            "idnHomographs",
            "typosquatting",
            "dga",
            "nrd",
            "newlyActiveDomains",
            "freeHostingDomains",
            "ddns",
            "tunnelingEndpoints",
            "dataDropServices",
            "residentialHosting",
            "untrustedCertificates",
            "fastFluxNetworks",
            "dnsDataExfiltration",
            "dnsPayloadDelivery",
            "decentralizedWebGateways",
            "highRiskTlds",
            "parking",
            "csam",
        )
    },
    ("privacy", "disguisedTrackers"): bool,
    ("privacy", "allowAffiliate"): bool,
    ("parentalControl", "safeSearch"): bool,
    ("parentalControl", "youtubeRestrictedMode"): bool,
    ("parentalControl", "blockBypass"): bool,
    ("parentalControl", "recreation"): dict,
    ("settings", "logs", "enabled"): bool,
    ("settings", "logs", "drop", "ip"): bool,
    ("settings", "logs", "drop", "domain"): bool,
    ("settings", "logs", "retention"): int,
    ("settings", "logs", "location"): str,
    ("settings", "blockPage", "enabled"): bool,
    ("settings", "performance", "ecs"): bool,
    ("settings", "performance", "cacheBoost"): bool,
    ("settings", "performance", "cnameFlattening"): bool,
    ("settings", "bav"): bool,
    ("settings", "web3"): bool,
}
OBJECT_SECTIONS = {"security", "privacy", "parentalControl", "settings"}
DOMAIN_LISTS = ("denylist", "allowlist")
_REWRITE_NAME = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)(\.(?!-)[a-z0-9_-]{1,63}(?<!-))*")
_DURATION = re.compile(r"^(\d+)\s*([smhdwy])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


class ConfigError(Exception):
    """A problem with the profile file, with its location when known."""

    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None):
        self.message = message
        self.file = file
        self.line = line
        where = f"{file}:{line}: " if file and line else (f"{file}: " if file else "")
        super().__init__(where + message)


# ── data ───────────────────────────────────────────────────────────────────


@dataclass
class Origin:
    """Where a value came from, for error messages."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class SourceSpec:
    location: str  # URL, or a path resolved relative to the profile file
    format: str
    skip_invalid: bool
    origin: Origin


@dataclass
class ListSpec:
    """A denylist/allowlist: inline entries plus sources, merged at plan time."""

    entries: list[tuple[str, bool, Origin]] = field(default_factory=list)  # (domain, active, origin)
    sources: list[SourceSpec] = field(default_factory=list)


@dataclass
class ProfileConfig:
    key: str
    id: Optional[str]
    origin: Origin
    overlay: dict[str, Any]  # everything except the domain lists
    lists: dict[str, ListSpec]
    origins: dict[Path, Origin]  # where each managed path is defined
    unchecked: list[Path]  # values not in KNOWN_VALUES; accepted only if the live profile has them


@dataclass
class Config:
    file: str
    profiles: list[ProfileConfig]

    def select(self, name_or_id: Optional[str]) -> list[ProfileConfig]:
        if name_or_id is None:
            return self.profiles
        wanted = name_or_id.lower()
        matches = [p for p in self.profiles if p.key.lower() == wanted or (p.id or "").lower() == wanted]
        if not matches:
            raise ConfigError(f"no profile {name_or_id!r} in the file", self.file)
        return matches


# ── YAML with line numbers ─────────────────────────────────────────────────


class _Map(dict):
    line: int = 0
    lines: dict[Any, int]


class _Seq(list):
    line: int = 0
    lines: list[int]


class _Loader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _Loader, node: yaml.MappingNode) -> _Map:
    loader.flatten_mapping(node)
    result = _Map()
    result.line = node.start_mark.line + 1
    result.lines = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise ConfigError(f"duplicate key {key!r}", loader.name, key_node.start_mark.line + 1)
        result[key] = loader.construct_object(value_node, deep=True)
        result.lines[key] = key_node.start_mark.line + 1
    return result


def _construct_sequence(loader: _Loader, node: yaml.SequenceNode) -> _Seq:
    result = _Seq(loader.construct_object(child, deep=True) for child in node.value)
    result.line = node.start_mark.line + 1
    result.lines = [child.start_mark.line + 1 for child in node.value]
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _construct_sequence)


# ── loading ────────────────────────────────────────────────────────────────


def load(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise ConfigError(str(e.strerror or e), path)
    return parse(text, path)


def parse(text: str, file: str = DEFAULT_FILE) -> Config:
    loader = _Loader(text)
    loader.name = file
    try:
        document = loader.get_single_data()
    except yaml.MarkedYAMLError as e:
        line = e.problem_mark.line + 1 if e.problem_mark else None
        raise ConfigError(f"invalid YAML: {e.problem}", file, line)
    finally:
        loader.dispose()

    if document is None:
        raise ConfigError("the file is empty", file)
    p = _Parser(file)
    root = p.mapping(document, "the file")
    p.only_keys(root, {"version", "profiles"}, "")
    version = root.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ConfigError(
            f"'version' must be one of {', '.join(map(str, SUPPORTED_VERSIONS))} (got {version!r})",
            file,
            root.lines.get("version", root.line),
        )
    profiles = p.mapping(root.get("profiles"), "'profiles'", root.lines.get("profiles", root.line))
    if not profiles:
        raise ConfigError("'profiles' is empty", file, profiles.line)
    return Config(file, [p.profile(str(key), value, profiles.lines[key]) for key, value in profiles.items()])


def parse_duration(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("expected a number of seconds or a duration like 30d")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _DURATION.match(value.strip())
        if match:
            return int(match.group(1)) * _DURATION_SECONDS[match.group(2)]
    raise ValueError(f"expected a number of seconds or a duration like 30d (got {value!r})")


class _Parser:
    def __init__(self, file: str):
        self.file = file
        self.base_dir = os.path.dirname(os.path.abspath(file))

    def error(self, message: str, line: Optional[int]) -> ConfigError:
        return ConfigError(message, self.file, line)

    def origin(self, line: int) -> Origin:
        return Origin(self.file, line)

    def mapping(self, value: Any, what: str, line: Optional[int] = None) -> _Map:
        if not isinstance(value, dict):
            raise self.error(f"{what} must be a mapping", getattr(value, "line", line))
        return value  # type: ignore[return-value]

    def sequence(self, value: Any, what: str, line: int) -> _Seq:
        if not isinstance(value, list):
            raise self.error(f"{what} must be a list", getattr(value, "line", line))
        return value  # type: ignore[return-value]

    def only_keys(self, mapping: _Map, allowed: set[str], where: str) -> None:
        for key in mapping:
            if key not in allowed:
                suffix = f" in {where}" if where else ""
                raise self.error(
                    f"unknown key {key!r}{suffix} (expected one of: {', '.join(sorted(allowed))})",
                    mapping.lines[key],
                )

    # ── profiles ───────────────────────────────────────────────────────────

    def profile(self, key: str, value: Any, line: int) -> ProfileConfig:
        body = self.mapping(value if value is not None else _empty_map(line), f"profile {key!r}", line)
        if "api_key" in body or "apiKey" in body:
            raise self.error("the API key must not be stored in this file; use NEXTDNS_API_KEY or 'auth'", line)
        self.only_keys(body, {"id"} | set(OBJECT_SECTIONS) | set(DOMAIN_LISTS) | {"rewrites"}, f"profile {key!r}")
        profile = ProfileConfig(
            key=key,
            id=None,
            origin=self.origin(line),
            overlay={},
            lists={},
            origins={},
            unchecked=[],
        )
        if "id" in body:
            if not isinstance(body["id"], str) or not body["id"]:
                raise self.error("'id' must be a profile id string", body.lines["id"])
            profile.id = body["id"]
        for section in OBJECT_SECTIONS:
            if section in body:
                profile.overlay[section] = self.object_section((section,), body[section], body.lines[section], profile)
        for name in DOMAIN_LISTS:
            if name in body:
                profile.lists[name] = self.domain_list(name, body[name], body.lines[name])
                profile.origins[(name,)] = self.origin(body.lines[name])
        if "rewrites" in body:
            profile.overlay["rewrites"] = self.rewrites(body["rewrites"], body.lines["rewrites"])
            profile.origins[("rewrites",)] = self.origin(body.lines["rewrites"])
        return profile

    def object_section(self, path: Path, value: Any, line: int, profile: ProfileConfig) -> dict[str, Any]:
        mapping = self.mapping(value, f"'{'.'.join(path)}'", line)
        result: dict[str, Any] = {}
        for key, child in mapping.items():
            child_path = path + (key,)
            child_line = mapping.lines[key]
            profile.origins[child_path] = self.origin(child_line)
            if child_path in ARRAY_SPECS:
                result[key] = self.catalog_array(child_path, child, child_line)
            elif child_path in KNOWN_VALUES:
                result[key] = self.value(child_path, child, child_line)
            elif isinstance(child, dict) and any(k[: len(child_path)] == child_path for k in KNOWN_VALUES):
                result[key] = self.object_section(child_path, child, child_line, profile)
            else:
                # Possibly a setting NextDNS added after this release; checked against the live profile.
                profile.unchecked.append(child_path)
                result[key] = _plain(child)
        return result

    def value(self, path: Path, value: Any, line: int) -> Any:
        expected = KNOWN_VALUES[path]
        if path == ("settings", "logs", "retention"):
            try:
                return parse_duration(value)
            except ValueError as e:
                raise self.error(f"'{'.'.join(path)}': {e}", line)
        if expected is bool and not isinstance(value, bool):
            raise self.error(f"'{'.'.join(path)}' must be true or false (got {value!r})", line)
        if expected is not bool and (isinstance(value, bool) or not isinstance(value, expected)):
            name = getattr(expected, "__name__", str(expected))
            raise self.error(f"'{'.'.join(path)}' must be a {name} (got {value!r})", line)
        return _plain(value)

    def catalog_array(self, path: Path, value: Any, line: int) -> list[dict[str, Any]]:
        spec = ARRAY_SPECS[path]
        items = self.sequence(value, f"'{spec.name}'", line)
        allowed_fields = {spec.key} | set(spec.defaults)
        result = []
        seen: dict[str, int] = {}
        for item, item_line in zip(items, items.lines):
            if isinstance(item, str):
                entry: dict[str, Any] = {spec.key: item}
            elif isinstance(item, dict):
                self.only_keys(item, allowed_fields, f"'{spec.name}' entry")  # type: ignore[arg-type]
                if not isinstance(item.get(spec.key), str):
                    raise self.error(f"'{spec.name}' entry needs an {spec.key!r}", item_line)
                for name in spec.defaults:
                    if name in item and not isinstance(item[name], bool):
                        raise self.error(f"{name!r} must be true or false", item.lines[name])  # type: ignore
                entry = dict(item)
            else:
                raise self.error(f"'{spec.name}' entries must be ids or mappings", item_line)
            entry_id = entry[spec.key]
            if entry_id in seen:
                raise self.error(
                    f"{entry_id!r} is listed twice in '{spec.name}' (first at line {seen[entry_id]})", item_line
                )
            seen[entry_id] = item_line
            result.append(entry)
        return result

    def domain_list(self, name: str, value: Any, line: int) -> ListSpec:
        if isinstance(value, list):
            value = _wrap({"domains": value}, line)
        body = self.mapping(value, f"'{name}'", line)
        self.only_keys(body, {"domains", "sources"}, f"'{name}'")
        spec = ListSpec()
        if "domains" in body:
            items = self.sequence(body["domains"], f"'{name}.domains'", body.lines.get("domains", line))
            for item, item_line in zip(items, items.lines):
                active = True
                if isinstance(item, dict):
                    self.only_keys(item, {"domain", "active"}, f"'{name}' entry")  # type: ignore[arg-type]
                    if "active" in item:
                        if not isinstance(item["active"], bool):
                            raise self.error("'active' must be true or false", item_line)
                        active = item["active"]
                    item = item.get("domain")
                if not isinstance(item, str):
                    raise self.error(f"'{name}' entries must be domains or {{domain, active}} mappings", item_line)
                try:
                    domain = normalize_domain(item)
                except InvalidDomainError as e:
                    raise self.error(f"'{name}': {e}", item_line)
                spec.entries.append((domain, active, self.origin(item_line)))
        if "sources" in body:
            items = self.sequence(body["sources"], f"'{name}.sources'", body.lines.get("sources", line))
            for item, item_line in zip(items, items.lines):
                spec.sources.append(self.source(name, item, item_line))
        return spec

    def source(self, list_name: str, value: Any, line: int) -> SourceSpec:
        if isinstance(value, str):
            value = _wrap({"url" if is_url(value) else "file": value}, line)
        body = self.mapping(value, f"'{list_name}.sources' entry", line)
        self.only_keys(body, {"url", "file", "format", "skip_invalid"}, f"'{list_name}.sources' entry")
        if ("url" in body) == ("file" in body):
            raise self.error("a source needs exactly one of 'url' or 'file'", line)
        if "url" in body:
            location = body["url"]
            if not isinstance(location, str) or not is_url(location):
                raise self.error("'url' must be an http(s) URL", body.lines["url"])
        else:
            location = body["file"]
            if not isinstance(location, str) or not location:
                raise self.error("'file' must be a path", body.lines["file"])
            location = os.path.normpath(os.path.join(self.base_dir, os.path.expanduser(location)))
        fmt = body.get("format", "auto")
        if fmt not in FORMATS:
            raise self.error(f"'format' must be one of {', '.join(FORMATS)}", body.lines.get("format", line))
        skip_invalid = body.get("skip_invalid", False)
        if not isinstance(skip_invalid, bool):
            raise self.error("'skip_invalid' must be true or false", body.lines.get("skip_invalid", line))
        return SourceSpec(location, fmt, skip_invalid, self.origin(line))

    def rewrites(self, value: Any, line: int) -> list[dict[str, str]]:
        items = self.sequence(value, "'rewrites'", line)
        result = []
        for item, item_line in zip(items, items.lines):
            body = self.mapping(item, "'rewrites' entry", item_line)
            self.only_keys(body, {"name", "content"}, "'rewrites' entry")
            name, content = body.get("name"), body.get("content")
            if not isinstance(name, str) or not name or not isinstance(content, str) or not content:
                raise self.error("a rewrite needs a 'name' and a 'content' (IP address or domain)", item_line)
            try:
                result.append({"name": normalize_rewrite_name(name), "content": normalize_rewrite_content(content)})
            except InvalidDomainError as e:
                raise self.error(f"'rewrites': {e}", item_line)
        return result


def normalize_rewrite_name(name: str) -> str:
    """Rewrite names may be single labels like `nas` (A17), unlike denylist entries."""
    text = name.strip().lower()
    if text.endswith("."):
        text = text[:-1]
    if not text.isascii():
        try:
            text = idna.encode(text, uts46=True).decode("ascii")
        except idna.IDNAError:
            raise InvalidDomainError(f"not a valid rewrite name: {name.strip()}")
    if not _REWRITE_NAME.fullmatch(text):
        raise InvalidDomainError(f"not a valid rewrite name: {name.strip()}")
    return text


def normalize_rewrite_content(content: str) -> str:
    """An IPv4/IPv6 address (A or AAAA record) or a domain (CNAME), as NextDNS infers it (A17)."""
    text = content.strip()
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    try:
        return normalize_domain(text)
    except InvalidDomainError:
        raise InvalidDomainError(f"rewrite content must be an IP address or a domain: {text}")


def _wrap(value: dict[str, Any], line: int) -> _Map:
    result = _Map(value)
    result.line = line
    result.lines = {k: line for k in value}
    return result


def _empty_map(line: int) -> _Map:
    return _wrap({}, line)


def _plain(value: Any) -> Any:
    """Strip the line-number wrappers."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
