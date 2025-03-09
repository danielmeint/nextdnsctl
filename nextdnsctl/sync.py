import json
import os
from typing import Dict, Any, List

from . import api
from .config import CONFIG_DIR

SYNC_CONFIG_FILE = os.path.join(CONFIG_DIR, "sync.json")

def load_sync_config() -> Dict[str, Any]:
    """Load the sync configuration from file."""
    if not os.path.exists(SYNC_CONFIG_FILE):
        raise ValueError("No sync config found. Create a sync.json file first.")
    with open(SYNC_CONFIG_FILE, "r") as f:
        return json.load(f)

def save_sync_config(config: Dict[str, Any]) -> None:
    """Save the sync configuration to file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SYNC_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def get_current_config(profile_id: str) -> Dict[str, Any]:
    """Get the current configuration from NextDNS."""
    return {
        "settings": api.get_settings(profile_id),
        "security": api.get_security(profile_id),
        "privacy": api.get_privacy(profile_id),
        "parentalcontrol": api.get_parentalcontrol(profile_id),
        "denylist": api.get_denylist(profile_id),
        "allowlist": api.get_allowlist(profile_id),
        "rewrites": api.get_rewrites(profile_id)
    }

def sync_profile(profile_id: str, desired_config: Dict[str, Any]) -> List[str]:
    """Sync a profile with the desired configuration."""
    current_config = get_current_config(profile_id)
    changes = []

    # Sync settings
    if desired_config.get("settings") != current_config["settings"]:
        api.update_settings(profile_id, desired_config["settings"])
        changes.append("Updated settings")

    # Sync security
    if desired_config.get("security") != current_config["security"]:
        api.update_security(profile_id, desired_config["security"])
        changes.append("Updated security settings")

    # Sync privacy
    if desired_config.get("privacy") != current_config["privacy"]:
        api.update_privacy(profile_id, desired_config["privacy"])
        changes.append("Updated privacy settings")

    # Sync parental control
    if desired_config.get("parentalcontrol") != current_config["parentalcontrol"]:
        api.update_parentalcontrol(profile_id, desired_config["parentalcontrol"])
        changes.append("Updated parental control settings")

    # Sync denylist
    desired_denylist = {item["id"]: item["active"] for item in desired_config.get("denylist", [])}
    current_denylist = {item["id"]: item["active"] for item in current_config["denylist"]}
    
    for domain, active in desired_denylist.items():
        if domain not in current_denylist or current_denylist[domain] != active:
            api.add_to_denylist(profile_id, domain, active)
            changes.append(f"Added/updated denylist entry: {domain}")
    
    for domain in current_denylist:
        if domain not in desired_denylist:
            api.remove_from_denylist(profile_id, domain)
            changes.append(f"Removed denylist entry: {domain}")

    # Sync allowlist
    desired_allowlist = {item["id"]: item["active"] for item in desired_config.get("allowlist", [])}
    current_allowlist = {item["id"]: item["active"] for item in current_config["allowlist"]}
    
    for domain, active in desired_allowlist.items():
        if domain not in current_allowlist or current_allowlist[domain] != active:
            api.add_to_allowlist(profile_id, domain, active)
            changes.append(f"Added/updated allowlist entry: {domain}")
    
    for domain in current_allowlist:
        if domain not in desired_allowlist:
            api.remove_from_allowlist(profile_id, domain)
            changes.append(f"Removed allowlist entry: {domain}")

    # Sync rewrites
    desired_rewrites = {item["id"]: item for item in desired_config.get("rewrites", [])}
    current_rewrites = {item["id"]: item for item in current_config["rewrites"]}
    
    for domain, data in desired_rewrites.items():
        if domain not in current_rewrites or current_rewrites[domain] != data:
            api.add_rewrite(profile_id, domain, data)
            changes.append(f"Added/updated rewrite: {domain}")
    
    for domain in current_rewrites:
        if domain not in desired_rewrites:
            api.remove_rewrite(profile_id, domain)
            changes.append(f"Removed rewrite: {domain}")

    return changes

def sync_all_profiles() -> Dict[str, List[str]]:
    """Sync all profiles defined in the sync config."""
    config = load_sync_config()
    results = {}
    
    for profile_id, desired_config in config.get("profiles", {}).items():
        try:
            changes = sync_profile(profile_id, desired_config)
            results[profile_id] = changes
        except Exception as e:
            results[profile_id] = [f"Error: {str(e)}"]
    
    return results 