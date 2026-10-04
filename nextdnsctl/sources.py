"""Reading domain lists from files and URLs, strictly.

Three formats are understood, each with an exact definition. A line that doesn't fit
the chosen format is an error, never a guess: silently adding the wrong domain (or
dropping part of a rule) is worse than failing loudly.

- plain:   one domain per line; a URL with a scheme is reduced to its host.
- hosts:   `<sinkhole address> <host> [<host>…]`, where the address is 0.0.0.0, 127.0.0.1,
           :: or ::1. A real address is a rewrite, not a block, so it's an error.
- adblock: exactly `||domain^`, which means "domain and its subdomains", the same as a
           NextDNS denylist entry. Exceptions, modifiers, wildcards, paths and cosmetic
           rules are errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests

from .domains import InvalidDomainError, domain_from_argument, normalize_domain

FORMATS = ("auto", "plain", "hosts", "adblock")
SINKHOLE_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::", "::1"}
# Standard boilerplate at the top of hosts files, skipped by exact name whatever the address.
HOSTS_BOILERPLATE = {
    "localhost",
    "localhost.localdomain",
    "local",
    "broadcasthost",
    "0.0.0.0",
    "ip6-localhost",
    "ip6-loopback",
    "ip6-localnet",
    "ip6-mcastprefix",
    "ip6-allnodes",
    "ip6-allrouters",
    "ip6-allhosts",
}
MAX_SOURCE_BYTES = 64 * 1024 * 1024
FETCH_TIMEOUT = 30.0

_IP_LIKE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:]*:[0-9a-fA-F:.%a-zA-Z0-9]*)$")
_ADBLOCK_DOMAIN_RULE = re.compile(r"^\|\|([^\^\$/|*@]+)\^$")


@dataclass(frozen=True)
class LineError:
    line: int
    text: str
    reason: str


@dataclass
class ParseResult:
    origin: str
    format: str
    domains: list[tuple[str, int]] = field(default_factory=list)  # (domain, line number)
    errors: list[LineError] = field(default_factory=list)
    boilerplate_skipped: int = 0


class SourceError(Exception):
    """A source could not be read, or its format could not be determined."""


# ── reading ────────────────────────────────────────────────────────────────


def is_url(location: str) -> bool:
    return location.startswith(("http://", "https://"))


def read_source(location: str) -> list[str]:
    """Return the lines of a file or URL."""
    if is_url(location):
        try:
            response = requests.get(location, timeout=FETCH_TIMEOUT, stream=True)
            response.raise_for_status()
            content = b""
            for chunk in response.iter_content(chunk_size=1 << 16):
                content += chunk
                if len(content) > MAX_SOURCE_BYTES:
                    raise SourceError(f"{location}: larger than {MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
        except requests.RequestException as e:
            raise SourceError(f"{location}: {e}") from e
        text = content.decode(response.encoding or "utf-8", errors="strict") if content else ""
    else:
        try:
            with open(location, encoding="utf-8-sig") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            raise SourceError(f"{location}: {e}") from e
    return text.splitlines()


def load(location: str, fmt: str = "auto") -> ParseResult:
    return parse(read_source(location), fmt, origin=location)


# ── parsing ────────────────────────────────────────────────────────────────


def parse(lines: Iterable[str], fmt: str = "auto", origin: str = "<input>") -> ParseResult:
    lines = list(lines)
    if fmt not in FORMATS:
        raise SourceError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")
    if fmt == "auto":
        fmt = detect_format(lines, origin)
    result = ParseResult(origin=origin, format=fmt)
    parse_line = {"plain": _parse_plain, "hosts": _parse_hosts, "adblock": _parse_adblock}[fmt]
    for number, raw in enumerate(lines, start=1):
        try:
            for domain in parse_line(raw, result):
                result.domains.append((domain, number))
        except InvalidDomainError as e:
            result.errors.append(LineError(number, raw.strip(), str(e)))
    return result


def detect_format(lines: list[str], origin: str = "<input>") -> str:
    """Pick the one format every non-comment line matches; mixed content is an error."""
    first_line: dict[str, int] = {}
    for number, raw in enumerate(lines, start=1):
        kind = _classify(raw)
        if kind is not None and kind not in first_line:
            first_line[kind] = number
    if not first_line:
        return "plain"
    if len(first_line) == 1:
        return next(iter(first_line))
    found = ", ".join(f"{kind} (first at line {line})" for kind, line in sorted(first_line.items(), key=lambda x: x[1]))
    raise SourceError(f"{origin}: lines in more than one format: {found}. Choose one with --format / format:")


def _classify(raw: str) -> Optional[str]:
    line = raw.strip()
    if not line or line.startswith("!") or (line.startswith("[") and line.endswith("]")):
        return None
    if line.startswith("#") and not line.startswith(("##", "#@#", "#?#")):
        return None
    if line.startswith(("||", "@@", "|")) or "##" in line or "#@#" in line or "#?#" in line:
        return "adblock"
    tokens = _strip_comment(line).split()
    if len(tokens) >= 2 and _IP_LIKE.match(tokens[0]):
        return "hosts"
    return "plain"


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _parse_plain(raw: str, result: ParseResult) -> list[str]:
    line = _strip_comment(raw.strip())
    if not line:
        return []
    if len(line.split()) > 1:
        raise InvalidDomainError("more than one token on the line (is this a hosts file? use --format hosts)")
    return [domain_from_argument(line)]


def _parse_hosts(raw: str, result: ParseResult) -> list[str]:
    line = _strip_comment(raw.strip())
    if not line:
        return []
    tokens = line.split()
    if len(tokens) < 2:
        raise InvalidDomainError("expected '<address> <host>'")
    address, hosts = tokens[0], tokens[1:]
    if all(h.lower() in HOSTS_BOILERPLATE for h in hosts):
        result.boilerplate_skipped += 1
        return []
    if address not in SINKHOLE_ADDRESSES:
        raise InvalidDomainError(
            f"maps to {address}, which is not a block address; redirecting a name is a rewrite, not a block"
        )
    return [normalize_domain(h) for h in hosts if h.lower() not in HOSTS_BOILERPLATE]


def _parse_adblock(raw: str, result: ParseResult) -> list[str]:
    line = raw.strip()
    if not line or line.startswith("!") or (line.startswith("[") and line.endswith("]")):
        return []
    if line.startswith("#") and not line.startswith(("##", "#@#", "#?#")):
        return []
    match = _ADBLOCK_DOMAIN_RULE.match(line)
    if match:
        return [normalize_domain(match.group(1))]
    if line.startswith("@@"):
        reason = "exception rule (@@); NextDNS expresses this with the allowlist"
    elif "##" in line or "#@#" in line or "#?#" in line:
        reason = "cosmetic rule; DNS can't hide page elements"
    elif "$" in line:
        reason = "rule with modifiers ($…); only plain ||domain^ rules can be expressed in NextDNS"
    elif "*" in line:
        reason = "wildcard rule; only plain ||domain^ rules can be expressed in NextDNS"
    elif "/" in line:
        reason = "rule with a path; DNS blocking works on whole domains"
    else:
        reason = "not a ||domain^ rule"
    raise InvalidDomainError(reason)
