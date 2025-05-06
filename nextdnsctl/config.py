import json
import os

CONFIG_DIR = os.path.expanduser("~/.nextdnsctl")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def save_api_key(api_key):
    """Save the NextDNS API key to a local config file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": api_key}, f)


def load_api_key():
    """Load the NextDNS API key from the config file."""
    if not os.path.exists(CONFIG_FILE):
        raise ValueError("No API key found. Run 'nextdnsctl auth <api_key>' first.")
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        if "api_key" not in config:
            raise ValueError(
                "Invalid config file. Run 'nextdnsctl auth <api_key>' to set up."
            )
        return config["api_key"]
