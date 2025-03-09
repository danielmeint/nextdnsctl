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


def get_allowlist(profile_id):
    """Retrieve the current allowlist for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/allowlist")["data"]


def add_to_allowlist(profile_id, domain, active=True):
    """Add a domain to the allowlist."""
    data = {"id": domain, "active": active}
    api_call("POST", f"/profiles/{profile_id}/allowlist", data=data)
    return f"Added {domain} as {'active' if active else 'inactive'}"


def remove_from_allowlist(profile_id, domain):
    """Remove a domain from the allowlist."""
    api_call("DELETE", f"/profiles/{profile_id}/allowlist/{domain}")
    return f"Removed {domain}"


def get_settings(profile_id):
    """Retrieve all settings for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/settings")


def update_settings(profile_id, settings):
    """Update settings for a profile."""
    api_call("PATCH", f"/profiles/{profile_id}/settings", data=settings)
    return "Settings updated"


def get_security(profile_id):
    """Retrieve security settings for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/security")


def update_security(profile_id, settings):
    """Update security settings for a profile."""
    api_call("PATCH", f"/profiles/{profile_id}/security", data=settings)
    return "Security settings updated"


def get_privacy(profile_id):
    """Retrieve privacy settings for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/privacy")


def update_privacy(profile_id, settings):
    """Update privacy settings for a profile."""
    api_call("PATCH", f"/profiles/{profile_id}/privacy", data=settings)
    return "Privacy settings updated"


def get_parentalcontrol(profile_id):
    """Retrieve parental control settings for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/parentalcontrol")


def update_parentalcontrol(profile_id, settings):
    """Update parental control settings for a profile."""
    api_call("PATCH", f"/profiles/{profile_id}/parentalcontrol", data=settings)
    return "Parental control settings updated"


def get_rewrites(profile_id):
    """Retrieve DNS rewrites for a profile."""
    return api_call("GET", f"/profiles/{profile_id}/rewrites")["data"]


def add_rewrite(profile_id, domain, data):
    """Add a DNS rewrite."""
    api_call("POST", f"/profiles/{profile_id}/rewrites", data={"id": domain, **data})
    return f"Added rewrite for {domain}"


def remove_rewrite(profile_id, domain):
    """Remove a DNS rewrite."""
    api_call("DELETE", f"/profiles/{profile_id}/rewrites/{domain}")
    return f"Removed rewrite for {domain}"
