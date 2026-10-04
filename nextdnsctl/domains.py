"""Domain name validation and normalisation, aligned with what NextDNS accepts (A8, A16)."""

from __future__ import annotations

import re

import idna

# Lowercase labels of letters, digits, hyphens and underscores (no leading/trailing hyphen),
# at least two labels, and a TLD of letters or punycode. NextDNS doesn't check TLDs
# against a list (A16), so neither do we.
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
DOMAIN_REGEX = re.compile(rf"^{_LABEL}(\.{_LABEL})*\.(?:[a-z]{{2,63}}|xn--[a-z0-9-]{{1,59}})$")
MAX_DOMAIN_LENGTH = 253


class InvalidDomainError(ValueError):
    """The input is not a domain NextDNS would accept."""


def normalize_domain(value: str) -> str:
    """Return the canonical form of a domain, or raise InvalidDomainError.

    Only exact transformations are applied: lowercase, internationalized names to
    punycode, and removing a single trailing dot (an FQDN is the same name).
    """
    domain = value.strip().lower()
    if domain.endswith(".") and not domain.endswith(".."):
        domain = domain[:-1]
    if not domain:
        raise InvalidDomainError("empty domain")
    if not domain.isascii():
        try:
            domain = idna.encode(domain, uts46=True).decode("ascii")
        except idna.IDNAError:
            raise InvalidDomainError(f"not a valid internationalized domain: {value.strip()}")
    if len(domain) > MAX_DOMAIN_LENGTH:
        raise InvalidDomainError(f"domain longer than {MAX_DOMAIN_LENGTH} characters")
    if "." not in domain:
        raise InvalidDomainError(f"not a domain (needs at least two labels): {value.strip()}")
    if not DOMAIN_REGEX.match(domain):
        raise InvalidDomainError(f"not a valid domain: {value.strip()}")
    return domain


def domain_from_argument(value: str) -> str:
    """Normalise a domain given on the command line. A URL with a scheme is reduced to its host.

    `https://example.com/path` unambiguously names example.com. A bare `example.com/path` is
    rejected: it's not clear whether the path was meant to be part of the block.
    """
    text = value.strip()
    if "://" in text:
        rest = text.split("://", 1)[1]
        host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        if host.startswith("["):
            raise InvalidDomainError(f"not a domain (IP address): {text}")
        host = host.split(":", 1)[0]
        return normalize_domain(host)
    return normalize_domain(text)
