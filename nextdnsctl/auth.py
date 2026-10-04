"""Where the API key comes from and where `auth login` stores it."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

ENV_VAR = "NEXTDNS_API_KEY"
PROFILE_ENV_VAR = "NEXTDNS_PROFILE"


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "nextdnsctl")


def config_file() -> str:
    return os.path.join(config_dir(), "config.json")


def legacy_config_file() -> str:
    """Where nextdnsctl 1.x stored the key."""
    return os.path.join(os.path.expanduser("~"), ".nextdnsctl", "config.json")


@dataclass(frozen=True)
class KeySource:
    key: str
    origin: str  # human-readable: "NEXTDNS_API_KEY" or a file path


class NoAPIKeyError(Exception):
    pass


def find_api_key() -> Optional[KeySource]:
    """Environment variable first, then the config file, then the 1.x location."""
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return KeySource(env, ENV_VAR)
    for path in (config_file(), legacy_config_file()):
        key = _read_key(path)
        if key:
            return KeySource(key, path)
    return None


def load_api_key() -> str:
    source = find_api_key()
    if source is None:
        raise NoAPIKeyError(f"No API key found. Set {ENV_VAR} or run 'nextdnsctl auth login'.")
    return source.key


def save_api_key(key: str) -> str:
    """Store the key readable only by the current user. Returns the file path."""
    path = config_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"api_key": key}, f)
    os.replace(tmp, path)
    return path


def delete_api_key() -> list[str]:
    """Remove stored keys (new and 1.x location). Returns the files removed."""
    removed = []
    for path in (config_file(), legacy_config_file()):
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def _read_key(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    key = data.get("api_key") if isinstance(data, dict) else None
    return key.strip() if isinstance(key, str) and key.strip() else None
