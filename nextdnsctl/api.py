import requests
import time
from requests.exceptions import RequestException

from .config import load_api_key

API_BASE = "https://api.nextdns.io"
DEFAULT_RETRIES = 4
DEFAULT_DELAY = 1  # For general errors or Retry-After scenarios
DEFAULT_TIMEOUT = 10
USER_AGENT = "nextdnsctl/0.3.0"
DEFAULT_PATIENT_RETRY_PAUSE_SECONDS = 60  # Added: Pause for unspecific 429s


# Custom Exception for persistent rate limits
class RateLimitStillActiveError(Exception):  # Added
    pass


def api_call(
        method,
        endpoint,
        data=None,
        retries=DEFAULT_RETRIES,
        delay=DEFAULT_DELAY,
        timeout=DEFAULT_TIMEOUT,
):
    """Make an API request to NextDNS."""
    api_key = load_api_key()
    headers = {"X-Api-Key": api_key, "User-Agent": USER_AGENT}
    url = f"{API_BASE}{endpoint}"

    for attempt in range(retries + 1):
        try:
            response = requests.request(
                method, url, json=data, headers=headers, timeout=timeout
            )

            if response.status_code == 429:
                retry_after_header = response.headers.get("Retry-After")
                if attempt < retries:  # Check if retries are left
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
                    continue  # Retry the current request
                else:  # No retries left
                    # If it's still a 429 on the last attempt, even without Retry-After, it's a persistent issue
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

            # Accept 200, 201, and 204 as success statuses
            if response.status_code not in (200, 201, 204):
                # For server errors (5xx), retry with exponential backoff if retries are available
                if response.status_code >= 500 and attempt < retries:
                    current_delay = delay * (2 ** attempt)
                    print(
                        f"Server error ({response.status_code}). Retrying in {current_delay}s "
                        f"(attempt {attempt + 1}/{retries + 1})..."
                    )
                    time.sleep(current_delay)
                    continue  # Retry the current request

                # For other client or server errors that are not retried or have exhausted retries
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
                current_delay = delay * (2 ** attempt)
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


def get_profiles(**kwargs):
    """Retrieve all NextDNS profiles."""
    return api_call("GET", "/profiles", **kwargs)["data"]


# Generic domain list functions
def get_domain_list(profile_id, list_type, **kwargs):
    """Retrieve the current list (denylist/allowlist) for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/{list_type}", **kwargs)["data"]


def add_to_domain_list(profile_id, list_type, domain, active=True, **kwargs):
    """Add a domain to a list (denylist/allowlist)."""
    data = {"id": domain, "active": active}
    api_call("POST", f"/profiles/{profile_id}/{list_type}", data=data, **kwargs)
    return f"Added {domain} as {'active' if active else 'inactive'}"


def remove_from_domain_list(profile_id, list_type, domain, **kwargs):
    """Remove a domain from a list (denylist/allowlist)."""
    api_call("DELETE", f"/profiles/{profile_id}/{list_type}/{domain}", **kwargs)
    return f"Removed {domain}"


# Convenience wrappers for backwards compatibility
def get_denylist(profile_id, **kwargs):
    """Retrieve the current denylist for a profile."""
    return get_domain_list(profile_id, "denylist", **kwargs)


def add_to_denylist(profile_id, domain, active=True, **kwargs):
    """Add a domain to the denylist."""
    return add_to_domain_list(profile_id, "denylist", domain, active, **kwargs)


def remove_from_denylist(profile_id, domain, **kwargs):
    """Remove a domain from the denylist."""
    return remove_from_domain_list(profile_id, "denylist", domain, **kwargs)


def get_allowlist(profile_id, **kwargs):
    """Retrieve the current allowlist for a profile."""
    return get_domain_list(profile_id, "allowlist", **kwargs)


def add_to_allowlist(profile_id, domain, active=True, **kwargs):
    """Add a domain to the allowlist."""
    return add_to_domain_list(profile_id, "allowlist", domain, active, **kwargs)


def remove_from_allowlist(profile_id, domain, **kwargs):
    """Remove a domain from the allowlist."""
    return remove_from_domain_list(profile_id, "allowlist", domain, **kwargs)
