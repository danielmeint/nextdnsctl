import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from requests.exceptions import RequestException

from . import __version__

API_BASE = "https://api.nextdns.io/"
DEFAULT_RETRIES = 4
DEFAULT_DELAY = 1  # For general errors or Retry-After scenarios
DEFAULT_TIMEOUT = 10
USER_AGENT = f"nextdnsctl/{__version__}"
DEFAULT_PATIENT_RETRY_PAUSE_SECONDS = 60  # Pause for unspecific 429s

# Domain validation regex - matches valid domain names
# Allows letters, numbers, hyphens, and dots. Must have at least one dot.
DOMAIN_REGEX = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,}$"
)


class RateLimitStillActiveError(Exception):
    """Raised when API rate limit persists after all retry attempts."""

    pass


class InvalidDomainError(Exception):
    """Raised when a domain name is invalid."""

    pass


def validate_domain(domain: str) -> str:
    """
    Validate a domain name format.

    Args:
        domain: The domain name to validate

    Returns:
        The validated domain (lowercase, stripped)

    Raises:
        InvalidDomainError: If the domain format is invalid
    """
    domain = domain.strip().lower()
    if not domain:
        raise InvalidDomainError("Domain cannot be empty")
    if len(domain) > 253:
        raise InvalidDomainError(f"Domain too long: {domain[:50]}...")
    if not DOMAIN_REGEX.match(domain):
        raise InvalidDomainError(f"Invalid domain format: {domain}")
    return domain


class APIClient:
    """
    NextDNS API client with connection pooling and retry logic.

    Uses a persistent session for HTTP Keep-Alive, reducing connection overhead
    for bulk operations.
    """

    def __init__(
        self,
        api_key: str,
        retries: int = DEFAULT_RETRIES,
        delay: float = DEFAULT_DELAY,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initialize the API client.

        Args:
            api_key: NextDNS API key
            retries: Number of retry attempts for failed requests
            delay: Initial delay between retries (exponential backoff)
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.retries = retries
        self.delay = delay
        self.timeout = timeout

        # Create persistent session for connection reuse
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Api-Key": api_key,
                "User-Agent": USER_AGENT,
            }
        )

    def call(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict[str, Any]] = None,
        retries: Optional[int] = None,
        delay: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Make an API request to NextDNS."""
        retries = retries if retries is not None else self.retries
        delay = delay if delay is not None else self.delay
        timeout = timeout if timeout is not None else self.timeout

        url = urljoin(API_BASE, endpoint.lstrip("/"))

        for attempt in range(retries + 1):
            try:
                response = self.session.request(method, url, json=data, timeout=timeout)

                if response.status_code == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    if attempt < retries:
                        if retry_after_header:
                            sleep_time = int(retry_after_header)
                            print(
                                f"Rate limited by API (Retry-After: {sleep_time}s). "
                                f"Retrying attempt {attempt + 1}/{retries + 1}..."
                            )
                        else:
                            sleep_time = DEFAULT_PATIENT_RETRY_PAUSE_SECONDS
                            print(
                                f"Rate limit hit (no Retry-After). "
                                f"Pausing for {sleep_time}s before attempt {attempt + 1}/{retries + 1}..."
                            )
                        time.sleep(sleep_time)
                        continue
                    else:
                        if not retry_after_header:
                            raise RateLimitStillActiveError(
                                "API rate limit still active after "
                                f"{retries + 1} attempts"
                                " and significant pauses."
                            )
                        else:
                            raise Exception(
                                "API rate limit exceeded after "
                                f"{retries + 1} attempts (Retry-After was "
                                f"{retry_after_header}s on last attempt)."
                            )

                if response.status_code not in (200, 201, 204):
                    if response.status_code >= 500 and attempt < retries:
                        current_delay = delay * (2**attempt)
                        print(
                            f"Server error ({response.status_code}). Retrying in {current_delay}s "
                            f"(attempt {attempt + 1}/{retries + 1})..."
                        )
                        time.sleep(current_delay)
                        continue

                    try:
                        error_data = response.json()
                        errors = error_data.get("errors", [{"detail": "Unknown error"}])
                        detail = (
                            errors[0].get("detail", "Unknown error")
                            if errors
                            else "Unknown error"
                        )
                        raise Exception(
                            f"API error: {detail} (Status: {response.status_code})"
                        )
                    except ValueError:
                        raise Exception(
                            f"API request failed with status {response.status_code} "
                            f"and non-JSON response."
                        )

                if response.status_code == 204:
                    return None
                return response.json()

            except RequestException as e:
                if attempt < retries:
                    current_delay = delay * (2**attempt)
                    print(
                        f"Network error ({e}). Retrying in {current_delay}s "
                        f"(attempt {attempt + 1}/{retries + 1})..."
                    )
                    time.sleep(current_delay)
                    continue
                else:
                    raise Exception(f"Network error after {retries + 1} attempts: {e}")

        raise Exception(
            f"API call failed after {retries + 1} attempts for an unknown reason."
        )

    def close(self) -> None:
        """Close the session and release resources."""
        self.session.close()

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # High-level API methods

    def get_profiles(self) -> List[Dict[str, Any]]:
        """Retrieve all NextDNS profiles."""
        response = self.call("GET", "profiles")
        if response is None:
            raise Exception("Unexpected empty response from profiles endpoint")
        return response["data"]

    def get_domain_list(self, profile_id: str, list_type: str) -> List[Dict[str, Any]]:
        """Retrieve the current list (denylist/allowlist) for a profile."""
        response = self.call("GET", f"profiles/{profile_id}/{list_type}")
        if response is None:
            raise Exception(f"Unexpected empty response from {list_type} endpoint")
        return response["data"]

    def add_to_domain_list(
        self,
        profile_id: str,
        list_type: str,
        domain: str,
        active: bool = True,
    ) -> str:
        """Add a domain to a list (denylist/allowlist)."""
        data = {"id": domain, "active": active}
        self.call("POST", f"profiles/{profile_id}/{list_type}", data=data)
        return f"Added {domain} as {'active' if active else 'inactive'}"

    def remove_from_domain_list(
        self, profile_id: str, list_type: str, domain: str
    ) -> str:
        """Remove a domain from a list (denylist/allowlist)."""
        self.call("DELETE", f"profiles/{profile_id}/{list_type}/{domain}")
        return f"Removed {domain}"


# Module-level client for backwards compatibility
# This is set by the CLI when it initializes
_client: Optional[APIClient] = None


def _get_client(**kwargs: Any) -> APIClient:
    """Get or create an API client instance."""
    global _client
    if _client is not None:
        return _client

    # Fallback for direct API usage (tests, scripts)
    from .config import load_api_key

    api_key = load_api_key()
    return APIClient(api_key, **kwargs)


def set_client(client: APIClient) -> None:
    """Set the module-level API client."""
    global _client
    _client = client


def clear_client() -> None:
    """Clear the module-level API client."""
    global _client
    if _client is not None:
        _client.close()
    _client = None


# Backwards-compatible function wrappers
def api_call(
    method: str,
    endpoint: str,
    data: Optional[Dict[str, Any]] = None,
    retries: int = DEFAULT_RETRIES,
    delay: float = DEFAULT_DELAY,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    """Make an API request to NextDNS (backwards-compatible wrapper)."""
    client = _get_client(retries=retries, delay=delay, timeout=timeout)
    return client.call(method, endpoint, data, retries, delay, timeout)


def get_profiles(**kwargs: Any) -> List[Dict[str, Any]]:
    """Retrieve all NextDNS profiles."""
    client = _get_client(**kwargs)
    return client.get_profiles()


def get_domain_list(
    profile_id: str, list_type: str, **kwargs: Any
) -> List[Dict[str, Any]]:
    """Retrieve the current list (denylist/allowlist) for a profile."""
    client = _get_client(**kwargs)
    return client.get_domain_list(profile_id, list_type)


def add_to_domain_list(
    profile_id: str,
    list_type: str,
    domain: str,
    active: bool = True,
    **kwargs: Any,
) -> str:
    """Add a domain to a list (denylist/allowlist)."""
    client = _get_client(**kwargs)
    return client.add_to_domain_list(profile_id, list_type, domain, active)


def remove_from_domain_list(
    profile_id: str, list_type: str, domain: str, **kwargs: Any
) -> str:
    """Remove a domain from a list (denylist/allowlist)."""
    client = _get_client(**kwargs)
    return client.remove_from_domain_list(profile_id, list_type, domain)


# Convenience wrappers for backwards compatibility
def get_denylist(profile_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Retrieve the current denylist for a profile."""
    return get_domain_list(profile_id, "denylist", **kwargs)


def add_to_denylist(
    profile_id: str, domain: str, active: bool = True, **kwargs: Any
) -> str:
    """Add a domain to the denylist."""
    return add_to_domain_list(profile_id, "denylist", domain, active, **kwargs)


def remove_from_denylist(profile_id: str, domain: str, **kwargs: Any) -> str:
    """Remove a domain from the denylist."""
    return remove_from_domain_list(profile_id, "denylist", domain, **kwargs)


def get_allowlist(profile_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Retrieve the current allowlist for a profile."""
    return get_domain_list(profile_id, "allowlist", **kwargs)


def add_to_allowlist(
    profile_id: str, domain: str, active: bool = True, **kwargs: Any
) -> str:
    """Add a domain to the allowlist."""
    return add_to_domain_list(profile_id, "allowlist", domain, active, **kwargs)


def remove_from_allowlist(profile_id: str, domain: str, **kwargs: Any) -> str:
    """Remove a domain from the allowlist."""
    return remove_from_domain_list(profile_id, "allowlist", domain, **kwargs)
