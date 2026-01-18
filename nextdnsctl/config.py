import json
import os
import stat

CONFIG_DIR: str = os.path.expanduser("~/.nextdnsctl")
CONFIG_FILE: str = os.path.join(CONFIG_DIR, "config.json")
ENV_VAR_NAME: str = "NEXTDNS_API_KEY"


def save_api_key(api_key: str) -> None:
    """Save the NextDNS API key to a local config file with secure permissions."""
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": api_key}, f)
    # Set file permissions to read/write for owner only (600)
    os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_api_key() -> str:
    """
    Load the NextDNS API key.

    Priority:
    1. NEXTDNS_API_KEY environment variable
    2. Config file (~/.nextdnsctl/config.json)
    """
    # Check environment variable first
    env_key = os.environ.get(ENV_VAR_NAME)
    if env_key:
        return env_key

    # Fall back to config file
    if not os.path.exists(CONFIG_FILE):
        raise ValueError(
            f"No API key found. Set {ENV_VAR_NAME} environment variable "
            "or run 'nextdnsctl auth <api_key>'."
        )
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        if "api_key" not in config:
            raise ValueError(
                "Invalid config file. Run 'nextdnsctl auth <api_key>' to set up."
            )
        return config["api_key"]
