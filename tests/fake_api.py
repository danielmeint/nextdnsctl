"""An in-memory NextDNS API that behaves like the real one, including its rough edges.

Every behaviour here was observed against the live API; the A-numbers refer to the
"API facts" table in docs/v2-design.md. Use it as the `session` of a Client, with a
FakeClock shared between the two so rate limits and pacing run in simulated time.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import secrets
from typing import Any, Optional
from urllib.parse import urlparse

API_KEY = "test-key"
MAX_BODY_BYTES = 100 * 1024  # A3
WRITES_PER_WINDOW = 60  # A11
READS_PER_WINDOW = 300
WINDOW_SECONDS = 60.0

DOMAIN = re.compile(
    r"^(?!-)[a-z0-9_-]{1,63}(?<!-)(\.(?!-)[a-z0-9_-]{1,63}(?<!-))*\.(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)

CATALOGS = {
    "privacy/blocklists": [
        {"id": "nextdns-recommended", "name": None, "website": None, "description": None, "entries": 73481},
        {"id": "oisd", "name": "OISD", "website": "https://oisd.nl", "description": "", "entries": 200000},
        {"id": "hagezi-pro", "name": "HaGeZi Pro", "website": "", "description": "", "entries": 150000},
    ],
    "privacy/natives": [{"id": "apple"}, {"id": "windows"}, {"id": "samsung"}],
    "parentalControl/services": [
        {"id": "tiktok", "website": "https://www.tiktok.com"},
        {"id": "instagram", "website": "https://www.instagram.com"},
    ],
    "parentalControl/categories": [{"id": "gambling"}, {"id": "porn"}, {"id": "dating"}],
    "security/tlds": [{"id": "zip", "spamhaus": 0}, {"id": "mov", "spamhaus": 0}, {"id": "xyz", "spamhaus": 1}],
}

SECURITY_KEYS = [
    "threatIntelligenceFeeds",
    "aiThreatDetection",
    "googleSafeBrowsing",
    "cryptojacking",
    "dnsRebinding",
    "idnHomographs",
    "typosquatting",
    "dga",
    "nrd",
    "csam",
]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.now += seconds
            self.slept += seconds


class FakeResponse:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def new_profile(profile_id: str, name: str) -> dict[str, Any]:
    return {
        "id": profile_id,
        "fingerprint": "fp" + profile_id,
        "role": "owner",
        "name": name,
        "setup": {"linkedIp": {"updateToken": "secret-token-" + profile_id}},
        "security": {**{k: False for k in SECURITY_KEYS}, "tlds": []},
        "privacy": {"blocklists": [], "natives": [], "disguisedTrackers": False, "allowAffiliate": False},
        "parentalControl": {
            "services": [],
            "categories": [],
            "safeSearch": False,
            "youtubeRestrictedMode": False,
            "blockBypass": False,
            "recreation": {"times": {}, "timezone": None},
        },
        # A13: logging is off on new profiles.
        "settings": {
            "logs": {"enabled": False, "drop": {"ip": False, "domain": False}, "retention": 7776000, "location": "us"},
            "blockPage": {"enabled": False},
            "performance": {"ecs": True, "cacheBoost": False, "cnameFlattening": False},
            "bav": False,
            "web3": False,
        },
        "denylist": [],
        "allowlist": [],
        "rewrites": [],
    }


class FakeNextDNS:
    """Use as `Client(..., session=fake, clock=clock, sleep=clock.sleep)`."""

    def __init__(self, clock: Optional[FakeClock] = None, rate_limits: bool = True):
        self.clock = clock or FakeClock()
        self.rate_limits = rate_limits
        self.headers: dict[str, str] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, Any]] = []  # (method, path, body)
        self.fail_next: list[FakeResponse] = []  # injected responses, consumed first
        self._windows = {"read": [0.0, 0], "write": [0.0, 0]}

    # ── test helpers ───────────────────────────────────────────────────────

    def add_profile(self, name: str, profile_id: Optional[str] = None, **sections: Any) -> dict[str, Any]:
        profile_id = profile_id or secrets.token_hex(3)
        profile = new_profile(profile_id, name)
        for key, value in sections.items():
            if isinstance(value, dict) and isinstance(profile.get(key), dict):
                _deep_merge(profile[key], value)
            else:
                profile[key] = value
        self.profiles[profile_id] = profile
        return profile

    def writes(self) -> list[tuple[str, str, Any]]:
        return [r for r in self.requests if r[0] != "GET"]

    # ── transport ──────────────────────────────────────────────────────────

    def request(self, method, url, data=None, params=None, headers=None, timeout=None):
        path = urlparse(url).path.strip("/")
        body = json.loads(data) if data else None
        self.requests.append((method, path, body))
        if self.fail_next:
            return self.fail_next.pop(0)
        is_catalog = path in CATALOGS
        if not is_catalog and self.headers.get("X-Api-Key") != API_KEY:
            return FakeResponse(403, {"errors": [{"code": "forbidden"}]})
        if self.rate_limits and not is_catalog and self._rate_limited("read" if method == "GET" else "write"):
            return FakeResponse(429, {"errors": [{"code": "tooManyRequests"}]})  # A11: no Retry-After
        if data and len(data) > MAX_BODY_BYTES:
            return FakeResponse(500, {"errors": [{"code": "internalServerError"}]})  # A3
        return self._route(method, path, body, params or {})

    def _rate_limited(self, kind: str) -> bool:
        window = self._windows[kind]
        if self.clock.now - window[0] >= WINDOW_SECONDS:
            window[0], window[1] = self.clock.now, 0
        limit = READS_PER_WINDOW if kind == "read" else WRITES_PER_WINDOW
        if window[1] >= limit:
            return True
        window[1] += 1
        return False

    # ── routing ────────────────────────────────────────────────────────────

    def _route(self, method: str, path: str, body: Any, params: dict) -> FakeResponse:
        if path in CATALOGS and method == "GET":
            return FakeResponse(200, {"data": copy.deepcopy(CATALOGS[path])})
        parts = path.split("/")
        if parts == ["profiles"]:
            if method == "GET":
                return FakeResponse(200, {"data": [_summary(p) for p in self.profiles.values()]})
            if method == "POST":
                profile = self.add_profile(body["name"])
                return FakeResponse(200, {"data": _summary(profile)})
        if len(parts) < 2 or parts[0] != "profiles" or parts[1] not in self.profiles:
            return FakeResponse(404, {"errors": [{"code": "notFound"}]})
        profile = self.profiles[parts[1]]
        rest = parts[2:]
        if not rest:
            if method == "GET":
                return FakeResponse(200, {"data": copy.deepcopy(profile)})
            if method == "DELETE":
                del self.profiles[parts[1]]
                return FakeResponse(204)
            if method == "PATCH":
                return self._patch_profile(profile, body)
        if rest == ["logs"] and method == "GET":
            return self._logs(parts[1], params)
        if rest[0] == "rewrites":
            return self._rewrites(profile, method, rest[1:], body)
        if rest[0] in ("denylist", "allowlist"):
            return self._domain_list(profile, rest[0], method, rest[1:], body)
        if method == "GET":
            value = profile
            for key in rest:
                if not isinstance(value, dict) or key not in value:
                    return FakeResponse(404, {"errors": [{"code": "notFound"}]})
                value = value[key]
            return FakeResponse(200, {"data": copy.deepcopy(value)})
        return FakeResponse(404, {"errors": [{"code": "notFound"}]})

    # ── PATCH /profiles/:id (A1, A2, A9) ───────────────────────────────────

    PATCHABLE = {"name", "security", "privacy", "parentalControl", "settings", "denylist", "allowlist"}

    def _patch_profile(self, profile: dict[str, Any], body: Any) -> FakeResponse:
        if not isinstance(body, dict):
            return _error(400, "type", "")
        for key in body:
            if key not in self.PATCHABLE:
                return _error(400, "extraneous", f"/{key}")  # A10: rewrites, setup, …
        staged = copy.deepcopy(profile)
        for key, value in body.items():
            if key in ("denylist", "allowlist"):
                entries, error = _validate_domain_entries(value, f"/{key}")
                if error:
                    return error
                staged[key] = entries
            elif key == "name":
                staged["name"] = value
            else:
                error = self._merge_section(staged[key], value, f"/{key}")
                if error:
                    return error
        profile.clear()
        profile.update(staged)
        return FakeResponse(204)

    def _merge_section(self, target: dict[str, Any], value: Any, pointer: str) -> Optional[FakeResponse]:
        if not isinstance(value, dict):
            return _error(400, "type", pointer)
        for key, child in value.items():
            child_pointer = f"{pointer}/{key}"
            catalog = child_pointer.strip("/")
            if catalog in CATALOGS:
                if not isinstance(child, list):
                    return _error(400, "type", child_pointer)
                valid = {e["id"]: e for e in CATALOGS[catalog]}
                ids = [e.get("id") if isinstance(e, dict) else None for e in child]
                if len(set(ids)) != len(ids):
                    return FakeResponse(200, {"errors": [{"code": "duplicate"}]})
                if any(i not in valid for i in ids):
                    return FakeResponse(400, {"errors": [{"code": "invalid"}]})  # A9: no pointer
                target[key] = [_catalog_entry(catalog, valid[e["id"]], e) for e in child]
            elif key not in target:
                return _error(400, "extraneous", child_pointer)
            elif isinstance(target[key], dict) and key != "recreation":
                error = self._merge_section(target[key], child, child_pointer)
                if error:
                    return error
            else:
                if isinstance(target[key], bool) and not isinstance(child, bool):
                    return _error(400, "type", child_pointer, f"`{child_pointer}` must be boolean.")
                target[key] = copy.deepcopy(child)
        return None

    # ── /denylist, /allowlist ──────────────────────────────────────────────

    def _domain_list(self, profile: dict, name: str, method: str, rest: list[str], body: Any) -> FakeResponse:
        entries = profile[name]
        if not rest:
            if method == "GET":
                return FakeResponse(200, {"data": copy.deepcopy(entries)})
            if method == "PUT":
                if isinstance(body, list):
                    ids = [e.get("id") for e in body if isinstance(e, dict)]
                    if len(set(ids)) != len(ids):
                        profile[name] = []  # A5: a duplicate empties the list
                        return FakeResponse(200, {"errors": [{"code": "duplicate"}]})
                new, error = _validate_domain_entries(body, "")
                if error:
                    return error
                profile[name] = new
                return FakeResponse(204)
            if method == "POST":
                if not isinstance(body, dict):
                    return _error(400, "type", "", "`` must be object.")
                new, error = _validate_domain_entries([body], "")
                if error:
                    return error
                if any(e["id"] == new[0]["id"] for e in entries):
                    return FakeResponse(200, {"errors": [{"code": "duplicate"}]})  # A6: not an upsert
                entries.insert(0, new[0])
                return FakeResponse(204)
        if len(rest) == 1:
            match = [e for e in entries if e["id"] == rest[0]]
            if not match:
                return FakeResponse(404, {"errors": [{"code": "notFound"}]})
            if method == "DELETE":
                entries.remove(match[0])
                return FakeResponse(204)
            if method == "PATCH":
                if "active" in body:
                    match[0]["active"] = bool(body["active"])
                return FakeResponse(204)
        return FakeResponse(404, {"errors": [{"code": "notFound"}]})

    # ── /rewrites (A10, A17) ───────────────────────────────────────────────

    def _rewrites(self, profile: dict, method: str, rest: list[str], body: Any) -> FakeResponse:
        rewrites = profile["rewrites"]
        if not rest:
            if method == "GET":
                return FakeResponse(200, {"data": copy.deepcopy(rewrites)})
            if method == "POST":
                if "type" in body:
                    return _error(400, "extraneous", "/type")
                record_type = _record_type(body.get("content", ""))
                if record_type is None:
                    return FakeResponse(200, {"errors": [{"code": "invalid", "source": {"pointer": "content"}}]})
                record = {
                    "id": secrets.token_hex(4),
                    "name": body["name"],
                    "type": record_type,
                    "content": body["content"],
                }
                rewrites.append(record)
                return FakeResponse(200, {"data": record})
            return FakeResponse(404, {"errors": [{"code": "notFound"}]})
        match = [r for r in rewrites if r["id"] == rest[0]]
        if method == "DELETE" and match:
            rewrites.remove(match[0])
            return FakeResponse(204)
        return FakeResponse(404, {"errors": [{"code": "notFound"}]})

    # ── /logs (A14) ────────────────────────────────────────────────────────

    def _logs(self, profile_id: str, params: dict) -> FakeResponse:
        limit = int(params.get("limit", 100))
        if limit < 10:
            return FakeResponse(
                400,
                {"errors": [{"code": "minimum", "source": {"parameter": "limit"}, "detail": "`limit` must be >= 10."}]},
            )
        entries = list(self.logs.get(profile_id, []))
        if params.get("status"):
            entries = [e for e in entries if e["status"] == params["status"]]
        if params.get("search"):
            entries = [e for e in entries if params["search"] in e["domain"]]
        start = int(params.get("cursor", 0))
        page = entries[start : start + limit]
        cursor = str(start + limit) if start + limit < len(entries) else None
        return FakeResponse(200, {"data": page, "meta": {"pagination": {"cursor": cursor}, "stream": {"id": "s1"}}})


def _summary(profile: dict[str, Any]) -> dict[str, Any]:
    return {k: profile[k] for k in ("id", "fingerprint", "role", "name")}


def _validate_domain_entries(value: Any, pointer: str) -> tuple[list[dict[str, Any]], Optional[FakeResponse]]:
    if not isinstance(value, list):
        return [], _error(400, "type", pointer)
    result = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            return [], _error(400, "type", f"{pointer}/{index}", f"`{pointer}/{index}` must be object.")
        domain = entry.get("id")
        if not isinstance(domain, str) or not DOMAIN.match(domain):
            return [], _error(
                400, "format", f"{pointer}/{index}/id", f'`{pointer}/{index}/id` must match format "domain".'
            )
        result.append({"id": domain, "active": entry.get("active", True)})  # A7
    ids = [e["id"] for e in result]
    if len(set(ids)) != len(ids):
        return [], FakeResponse(200, {"errors": [{"code": "duplicate"}]})
    return result, None


def _catalog_entry(catalog: str, reference: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(reference)
    if catalog.startswith("parentalControl/"):
        result["active"] = entry.get("active", True)
        result["recreation"] = entry.get("recreation", False)
    return result


def _record_type(content: str) -> Optional[str]:
    try:
        return "A" if ipaddress.ip_address(content).version == 4 else "AAAA"
    except ValueError:
        return "CNAME" if DOMAIN.match(content) else None


def _error(status: int, code: str, pointer: str, detail: Optional[str] = None) -> FakeResponse:
    error: dict[str, Any] = {"code": code}
    if pointer:
        error["source"] = {"pointer": pointer}
    if detail:
        error["detail"] = detail
    return FakeResponse(status, {"errors": [error]})


def _deep_merge(target: dict[str, Any], value: dict[str, Any]) -> None:
    for key, child in value.items():
        if isinstance(child, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], child)
        else:
            target[key] = child
