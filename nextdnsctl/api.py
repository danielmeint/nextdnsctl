import requests


from .config import load_api_key

API_BASE = "https://api.nextdns.io"


def api_call(method, endpoint, data=None):
    """Make an API request to NextDNS."""
    api_key = load_api_key()
    headers = {"X-Api-Key": api_key}
    url = f"{API_BASE}{endpoint}"
    response = requests.request(method, url, json=data, headers=headers)

    # Accept 200, 201, and 204 as success statuses
    if response.status_code not in (200, 201, 204):
        try:
            error_data = response.json()
            errors = error_data.get("errors", [{"detail": "Unknown error"}])
            # Ensure we get a string detail, even if the structure is odd
            detail = errors[0].get("detail", "Unknown error") if errors else "Unknown error"
            raise Exception(f"API error: {detail}")
        except ValueError:
            raise Exception(f"API request failed with status {response.status_code}")

    # Return JSON if available, None for 204
    if response.status_code == 204:
        return None
    return response.json()


def get_profiles():
    """Retrieve all NextDNS profiles."""
    return api_call("GET", "/profiles")["data"]


def get_denylist(profile_id):
    """Retrieve the current denylist for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/denylist")["data"]


def add_to_denylist(profile_id, domain, active=True):
    """Add a domain to the denylist."""
    data = {"id": domain, "active": active}
    api_call("POST", f"/profiles/{profile_id}/denylist", data=data)
    return f"Added {domain} as {'active' if active else 'inactive'}"


def remove_from_denylist(profile_id, domain):
    """Remove a domain from the denylist."""
    api_call("DELETE", f"/profiles/{profile_id}/denylist/{domain}")
    return f"Removed {domain}"
