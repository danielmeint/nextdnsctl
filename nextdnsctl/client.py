"""HTTP client for the NextDNS API: pacing, retries and error mapping.

The numbers below come from measurements against the live API, recorded in
docs/v2-design.md (section "API facts", referenced here as A1..A17).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Iterator, Optional

import requests

from . import __version__

log = logging.getLogger(__name__)

API_BASE = "https://api.nextdns.io/"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3

# A3: request bodies above 100 KiB fail with a 500 and change nothing.
MAX_BODY_BYTES = 100 * 1024

# A11: writes are limited to 60 per fixed 60 s window per API key. Read limits depend on
# the endpoint (a full profile GET is throttled much sooner than a list) and clear within
# seconds, so on a 429 back off quickly at first; the schedule still spans a full write window.
WRITE_INTERVAL = 1.0
READ_INTERVAL = 0.2
RATE_LIMIT_BACKOFF = (2.0, 4.0, 8.0, 16.0, 30.0, 30.0)

Clock = Callable[[], float]
Sleep = Callable[[float], None]


class NextDNSError(Exception):
    """Base class for errors talking to NextDNS."""


class APIError(NextDNSError):
    """NextDNS answered with an error, either as an HTTP error or as a 200 with an `errors` body (A5, A6)."""

    def __init__(self, status: int, errors: list[dict[str, Any]], method: str, path: str):
        self.status = status
        self.errors = errors
        self.method = method
        self.path = path
        super().__init__(f"{method} {path}: {self.describe()} (HTTP {status})")

    @property
    def codes(self) -> list[str]:
        return [str(e.get("code")) for e in self.errors if isinstance(e, dict) and e.get("code")]

    @property
    def pointers(self) -> list[str]:
        """JSON pointers to the offending fields, where NextDNS provides them (A9)."""
        result = []
        for error in self.errors:
            source = error.get("source") if isinstance(error, dict) else None
            if isinstance(source, dict):
                pointer = source.get("pointer") or source.get("parameter")
                if pointer:
                    result.append(str(pointer))
        return result

    def has_code(self, code: str) -> bool:
        return code in self.codes

    def describe(self) -> str:
        if not self.errors:
            return "unknown error"
        parts = []
        for error in self.errors:
            if not isinstance(error, dict):
                parts.append(str(error))
                continue
            text = error.get("detail") or error.get("title") or error.get("code") or "error"
            source = error.get("source") or {}
            where = source.get("pointer") or source.get("parameter") if isinstance(source, dict) else None
            if where and where not in str(text):
                text = f"{text} at {where}"
            parts.append(str(text))
        return "; ".join(parts)


class RateLimitError(NextDNSError):
    """Still rate limited after waiting out a full window."""


class PayloadTooLargeError(NextDNSError):
    """The request body exceeds what NextDNS accepts (A3)."""


class NetworkError(NextDNSError):
    """The API could not be reached."""


def encode_body(data: Any) -> bytes:
    """Serialise a request body the way it is sent: compact JSON, so size checks are exact."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Pacer:
    """Spaces calls at least `interval` seconds apart. Thread-safe."""

    def __init__(self, interval: float, clock: Clock = time.monotonic, sleep: Sleep = time.sleep):
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next = self._clock() + self.interval

    def hold(self, seconds: float) -> None:
        """Push the next allowed call at least `seconds` into the future."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


class Client:
    """NextDNS API client. One instance per API key; safe to share between threads."""

    def __init__(
        self,
        api_key: Optional[str],
        *,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        session: Any = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
        write_interval: float = WRITE_INTERVAL,
        read_interval: float = READ_INTERVAL,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.retries = retries
        self.session = session if session is not None else requests.Session()
        self.session.headers.update({"User-Agent": f"nextdnsctl/{__version__}"})
        if api_key:
            self.session.headers["X-Api-Key"] = api_key
        self._sleep = sleep
        self.writes = Pacer(write_interval, clock, sleep)
        self.reads = Pacer(read_interval, clock, sleep)

    # ── transport ──────────────────────────────────────────────────────────

    def request(self, method: str, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        """Send a request and return the parsed JSON body (None for 204)."""
        path = path.lstrip("/")
        payload = encode_body(body) if body is not None else None
        if payload is not None and len(payload) > MAX_BODY_BYTES:
            raise PayloadTooLargeError(
                f"{method} {path}: body is {len(payload)} bytes, NextDNS accepts at most {MAX_BODY_BYTES}"
            )
        pacer = self.reads if method == "GET" else self.writes
        headers = {"Content-Type": "application/json"} if payload is not None else None
        attempt = 0
        rate_limited = 0

        while True:
            pacer.wait()
            try:
                response = self.session.request(
                    method, self.base_url + path, data=payload, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as e:
                if attempt < self.retries:
                    attempt += 1
                    delay = 2.0 ** (attempt - 1)
                    log.warning("Network error (%s); retrying in %gs", e, delay)
                    self._sleep(delay)
                    continue
                raise NetworkError(f"{method} {path}: {e}") from e

            status = response.status_code
            if status == 429:
                if rate_limited >= len(RATE_LIMIT_BACKOFF):
                    raise RateLimitError(
                        f"{method} {path}: still rate limited after waiting {sum(RATE_LIMIT_BACKOFF):.0f}s. "
                        "Is another program using the same API key?"
                    )
                delay = RATE_LIMIT_BACKOFF[rate_limited]
                rate_limited += 1
                log.log(
                    logging.WARNING if delay >= 8 else logging.INFO, "Rate limited by NextDNS; retrying in %gs", delay
                )
                pacer.hold(delay)
                continue
            if status >= 500 and attempt < self.retries:
                attempt += 1
                delay = 2.0 ** (attempt - 1)
                log.warning("NextDNS server error (HTTP %d); retrying in %gs", status, delay)
                self._sleep(delay)
                continue

            data = _parse_json(response)
            errors = data.get("errors") if isinstance(data, dict) else None
            if status >= 400 or errors:
                if not isinstance(errors, list):
                    errors = [{"code": "http", "detail": f"HTTP {status}"}]
                raise APIError(status, errors, method, path)
            return data

    def _data(self, method: str, path: str, body: Any = None, params: Optional[dict] = None) -> Any:
        response = self.request(method, path, body, params)
        return response.get("data") if isinstance(response, dict) else response

    # ── profiles ───────────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._data("GET", "profiles") or []

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return self._data("GET", f"profiles/{profile_id}")

    def create_profile(self, name: str) -> dict[str, Any]:
        return self._data("POST", "profiles", {"name": name})

    def delete_profile(self, profile_id: str) -> None:
        self.request("DELETE", f"profiles/{profile_id}")

    def patch_profile(self, profile_id: str, body: dict[str, Any]) -> None:
        """Atomic multi-section update (A1): objects merge, arrays replace (A2)."""
        self.request("PATCH", f"profiles/{profile_id}", body)

    # ── arrays (denylist, allowlist, rewrites, …) ──────────────────────────

    def get_items(self, profile_id: str, path: str) -> list[dict[str, Any]]:
        return self._data("GET", f"profiles/{profile_id}/{path}") or []

    def add_item(self, profile_id: str, path: str, item: dict[str, Any]) -> Any:
        return self._data("POST", f"profiles/{profile_id}/{path}", item)

    def patch_item(self, profile_id: str, path: str, key: str, body: dict[str, Any]) -> None:
        self.request("PATCH", f"profiles/{profile_id}/{path}/{key}", body)

    def delete_item(self, profile_id: str, path: str, key: str) -> None:
        self.request("DELETE", f"profiles/{profile_id}/{path}/{key}")

    # ── catalogs (A15) and logs (A14) ──────────────────────────────────────

    def catalog(self, path: str) -> list[dict[str, Any]]:
        """Public list of valid ids, e.g. 'privacy/blocklists'."""
        return self._data("GET", path) or []

    def iter_logs(self, profile_id: str, **params: Any) -> Iterator[dict[str, Any]]:
        """Yield log entries newest first, following the cursor."""
        query = {k: v for k, v in params.items() if v is not None}
        query.setdefault("limit", 100)
        while True:
            response = self.request("GET", f"profiles/{profile_id}/logs", params=query) or {}
            yield from response.get("data") or []
            cursor = ((response.get("meta") or {}).get("pagination") or {}).get("cursor")
            if not cursor:
                return
            query["cursor"] = cursor


def _parse_json(response: Any) -> Any:
    if response.status_code == 204 or not getattr(response, "content", b"x"):
        return None
    try:
        return response.json()
    except ValueError:
        return None
