import re
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import idna
import requests
from requests.exceptions import RequestException

from . import __version__

API_BASE = "https://api.nextdns.io/"
DEFAULT_RETRIES = 4
DEFAULT_DELAY = 1  # For general errors or Retry-After scenarios
DEFAULT_TIMEOUT = 10
USER_AGENT = f"nextdnsctl/{__version__}"
DEFAULT_PATIENT_RETRY_PAUSE_SECONDS = 60  # Pause for unspecific 429s

# Domain validation regex, aligned with what the NextDNS API accepts: lowercase labels of
# letters, digits, hyphens and underscores (no leading/trailing hyphen), at least two labels,
# and a TLD that is either letters or punycode (xn--...).
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
DOMAIN_REGEX = re.compile(rf"^{_LABEL}(\.{_LABEL})*\.(?:[a-z]{{2,63}}|xn--[a-z0-9-]{{1,59}})$")


class RateLimitStillActiveError(Exception):
    """Raised when API rate limit persists after all retry attempts."""

    pass


class InvalidDomainError(Exception):
    """Raised when a domain name is invalid."""

    pass


class APIError(Exception):
    """Raised when the NextDNS API returns an error response."""

    pass


def _extract_api_error_detail(error_data: Any) -> str:
    """Extract a user-readable detail from a NextDNS API error payload."""
    if not isinstance(error_data, dict):
        return "Unknown error"

    errors = error_data.get("errors")
    if not errors:
        return "Unknown error"

    first_error = errors[0]
    if not isinstance(first_error, dict):
        return str(first_error)

    return first_error.get("detail") or first_error.get("title") or first_error.get("code") or str(first_error)


def _warn(message: str) -> None:
    """Print a diagnostic to stderr so it never mixes with command output on stdout."""
    print(message, file=sys.stderr)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header (delay in seconds or an HTTP date). None if absent/invalid."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def validate_domain(domain: str) -> str:
    """
    Validate a domain name format.

    Accepts full URLs and extracts the domain portion. Handles:
    - Protocol prefixes (http://, https://, ftp://, etc.)
    - Paths after the domain (/path/to/something)
    - Port numbers (example.com:8080)
    - A single trailing dot (example.com. is the same name as example.com)
    - Internationalized names, converted to punycode (münchen.de -> xn--mnchen-3ya.de)

    Args:
        domain: The domain name or URL to validate

    Returns:
        The validated domain (lowercase, stripped)

    Raises:
        InvalidDomainError: If the domain format is invalid
    """
    domain = domain.strip().lower()
    if not domain:
        raise InvalidDomainError("Domain cannot be empty")

    # Strip protocol prefix (e.g., https://, http://, ftp://)
    if "://" in domain:
        domain = domain.split("://", 1)[1]

    # Strip path (everything after first /)
    if "/" in domain:
        domain = domain.split("/", 1)[0]

    # Strip port number (e.g., :8080)
    if ":" in domain:
        domain = domain.split(":", 1)[0]

    if domain.endswith(".") and not domain.endswith(".."):
        domain = domain[:-1]

    if not domain:
        raise InvalidDomainError("Domain cannot be empty")
    if not domain.isascii():
        try:
            domain = idna.encode(domain, uts46=True).decode("ascii")
        except idna.IDNAError:
            raise InvalidDomainError(f"Invalid domain format: {domain}")
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

        self._session_local = threading.local()
        self._sessions: List[requests.Session] = []
        self._sessions_lock = threading.Lock()

        # When one thread hits a rate limit, the others hold off too instead of each
        # sending requests into the same limit.
        self._pause_lock = threading.Lock()
        self._pause_until = 0.0
        self._pause_owner: Optional[int] = None

        # Create a main-thread session for connection reuse and backwards compatibility.
        self.session = self._create_session()
        self._session_local.session = self.session

    def _create_session(self) -> requests.Session:
        """Create a configured session and track it for cleanup."""
        session = requests.Session()
        session.headers.update(
            {
                "X-Api-Key": self.api_key,
                "User-Agent": USER_AGENT,
            }
        )
        with self._sessions_lock:
            self._sessions.append(session)
        return session

    def _get_session(self) -> requests.Session:
        """Return a thread-local session."""
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = self._create_session()
            self._session_local.session = session
        return session

    def _pause_all(self, seconds: float) -> None:
        """Ask every thread using this client to wait until the rate limit has passed."""
        with self._pause_lock:
            until = time.time() + seconds
            if until > self._pause_until:
                self._pause_until = until
                self._pause_owner = threading.get_ident()

    def _wait_for_pause(self) -> None:
        """Block while another thread's rate-limit pause is in effect."""
        with self._pause_lock:
            if self._pause_owner == threading.get_ident():
                return  # the pausing thread sleeps on its own
            remaining = self._pause_until - time.time()
        if remaining > 0:
            time.sleep(remaining)

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
                self._wait_for_pause()
                response = self._get_session().request(method, url, json=data, timeout=timeout)

                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    if attempt < retries:
                        if retry_after is not None:
                            sleep_time = retry_after
                            _warn(
                                f"Rate limited by API (Retry-After: {sleep_time:g}s). "
                                f"Retrying attempt {attempt + 1}/{retries + 1}..."
                            )
                        else:
                            sleep_time = DEFAULT_PATIENT_RETRY_PAUSE_SECONDS
                            _warn(
                                f"Rate limit hit (no Retry-After). "
                                f"Pausing for {sleep_time}s before attempt {attempt + 1}/{retries + 1}..."
                            )
                        self._pause_all(sleep_time)
                        time.sleep(sleep_time)
                        continue
                    else:
                        raise RateLimitStillActiveError(
                            f"API rate limit still active after {retries + 1} attempts and significant pauses."
                        )

                if response.status_code not in (200, 201, 204):
                    if response.status_code >= 500 and attempt < retries:
                        current_delay = delay * (2**attempt)
                        _warn(
                            f"Server error ({response.status_code}). Retrying in {current_delay}s "
                            f"(attempt {attempt + 1}/{retries + 1})..."
                        )
                        time.sleep(current_delay)
                        continue

                    try:
                        error_data = response.json()
                        detail = _extract_api_error_detail(error_data)
                        raise APIError(f"API error: {detail} (Status: {response.status_code})")
                    except ValueError:
                        raise APIError(
                            f"API request failed with status {response.status_code} " f"and non-JSON response."
                        )

                if response.status_code == 204:
                    return None
                response_data = response.json()
                if isinstance(response_data, dict) and response_data.get("errors"):
                    detail = _extract_api_error_detail(response_data)
                    raise APIError(f"API error: {detail} (Status: {response.status_code})")
                return response_data

            except RequestException as e:
                if attempt < retries:
                    current_delay = delay * (2**attempt)
                    _warn(
                        f"Network error ({e}). Retrying in {current_delay}s "
                        f"(attempt {attempt + 1}/{retries + 1})..."
                    )
                    time.sleep(current_delay)
                    continue
                else:
                    raise Exception(f"Network error after {retries + 1} attempts: {e}")

        raise Exception(f"API call failed after {retries + 1} attempts for an unknown reason.")

    def close(self) -> None:
        """Close the session and release resources."""
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

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

    def remove_from_domain_list(self, profile_id: str, list_type: str, domain: str) -> str:
        """Remove a domain from a list (denylist/allowlist)."""
        self.call("DELETE", f"profiles/{profile_id}/{list_type}/{domain}")
        return f"Removed {domain}"

    def update_domain_list_entry(self, profile_id: str, list_type: str, domain: str, active: bool) -> str:
        """Update a domain entry in a list (denylist/allowlist)."""
        self.call("PATCH", f"profiles/{profile_id}/{list_type}/{domain}", data={"active": active})
        return f"Updated {domain} to {'active' if active else 'inactive'}"


# Module-level client for backwards compatibility
# This is set by the CLI when it initializes
_client: Optional[APIClient] = None


def _get_client(**kwargs: Any) -> APIClient:
    """Get or create an API client instance."""
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


def get_domain_list(profile_id: str, list_type: str, **kwargs: Any) -> List[Dict[str, Any]]:
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


def remove_from_domain_list(profile_id: str, list_type: str, domain: str, **kwargs: Any) -> str:
    """Remove a domain from a list (denylist/allowlist)."""
    client = _get_client(**kwargs)
    return client.remove_from_domain_list(profile_id, list_type, domain)


def update_domain_list_entry(
    profile_id: str,
    list_type: str,
    domain: str,
    active: bool,
    **kwargs: Any,
) -> str:
    """Update a domain entry in a list (denylist/allowlist)."""
    client = _get_client(**kwargs)
    return client.update_domain_list_entry(profile_id, list_type, domain, active)


# Convenience wrappers for backwards compatibility
def get_denylist(profile_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Retrieve the current denylist for a profile."""
    return get_domain_list(profile_id, "denylist", **kwargs)


def add_to_denylist(profile_id: str, domain: str, active: bool = True, **kwargs: Any) -> str:
    """Add a domain to the denylist."""
    return add_to_domain_list(profile_id, "denylist", domain, active, **kwargs)


def remove_from_denylist(profile_id: str, domain: str, **kwargs: Any) -> str:
    """Remove a domain from the denylist."""
    return remove_from_domain_list(profile_id, "denylist", domain, **kwargs)


def update_denylist_entry(profile_id: str, domain: str, active: bool, **kwargs: Any) -> str:
    """Update a denylist entry."""
    return update_domain_list_entry(profile_id, "denylist", domain, active, **kwargs)


def get_allowlist(profile_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Retrieve the current allowlist for a profile."""
    return get_domain_list(profile_id, "allowlist", **kwargs)


def add_to_allowlist(profile_id: str, domain: str, active: bool = True, **kwargs: Any) -> str:
    """Add a domain to the allowlist."""
    return add_to_domain_list(profile_id, "allowlist", domain, active, **kwargs)


def remove_from_allowlist(profile_id: str, domain: str, **kwargs: Any) -> str:
    """Remove a domain from the allowlist."""
    return remove_from_domain_list(profile_id, "allowlist", domain, **kwargs)


def update_allowlist_entry(profile_id: str, domain: str, active: bool, **kwargs: Any) -> str:
    """Update an allowlist entry."""
    return update_domain_list_entry(profile_id, "allowlist", domain, active, **kwargs)
