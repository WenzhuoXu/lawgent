"""Shared HTTP plumbing for the connector layer.

Resilience contract (used by every connector and by ``mcp/sync_client.py``):

- ``request_with_retry`` retries transient failures — transport errors,
  timeouts, 429 (honoring ``Retry-After``), and 5xx — with exponential
  backoff + jitter, then raises the last ``httpx.HTTPError`` so existing
  ``except httpx.HTTPError`` call sites keep producing the uniform
  ``{"error": ...}`` dict.
- Non-JSON 200s raise :class:`ConnectorHTTPError` (an ``httpx.HTTPError``
  subclass) instead of leaking ``json.JSONDecodeError`` to the tool loop.
- ``TTLCache`` + ``SEARCH_TTL`` / ``BODY_TTL`` give idempotent lookups a
  bounded response cache (15 min for searches, 24 h for immutable bodies).
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from collections import OrderedDict
from typing import Any, Optional, Protocol

import httpx


USER_AGENT = "legal-helper/1.0 (+contact: legal-helper@example.com)"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

RETRY_ATTEMPTS = 3
_BACKOFF_BASE = 0.5  # seconds; grows 0.5 → 1.0 → 2.0 (+ jitter)
_BACKOFF_CAP = 8.0
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

SEARCH_TTL = 15 * 60  # idempotent search lookups
BODY_TTL = 24 * 60 * 60  # immutable document bodies

# Indirection so tests can monkeypatch away real sleeping.
_sleep = time.sleep


class ConnectorHTTPError(httpx.HTTPError):
    """A 2xx response that violated the connector contract (e.g. non-JSON
    body on a JSON endpoint). Subclasses ``httpx.HTTPError`` so every
    existing ``except httpx.HTTPError`` site converts it into the uniform
    ``{"error": ...}`` payload."""


class TTLCache:
    """Small thread-safe TTL cache with LRU eviction. Values are stored as
    handed in; callers treat cached payloads as read-only."""

    _MISS = object()

    def __init__(self, maxsize: int = 256):
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return self._MISS
            expires, value = entry
            if time.monotonic() >= expires:
                del self._data[key]
                return self._MISS
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_RESPONSE_CACHE = TTLCache(maxsize=256)


def cache_key(*parts: Any) -> str:
    """Stable sha256 key over arbitrary parts (never ``hash()``, which is
    salted per process and collides across restarts)."""
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    if retry_after:
        try:
            return min(float(retry_after), _BACKOFF_CAP)
        except ValueError:
            pass  # HTTP-date form — fall through to backoff
    delay = min(_BACKOFF_BASE * (2**attempt), _BACKOFF_CAP)
    return delay + random.uniform(0, delay / 4)


def request_with_retry(
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: Optional[httpx.Timeout] = None,
    follow_redirects: bool = True,
    attempts: int = RETRY_ATTEMPTS,
) -> httpx.Response:
    """Issue one HTTP request with bounded retries for transient failures.

    Raises the final ``httpx.HTTPError`` (transport error or
    ``HTTPStatusError``) when every attempt fails; callers keep their
    existing ``except httpx.HTTPError`` → ``{"error": ...}`` handling.
    """
    last_exc: Optional[httpx.HTTPError] = None
    for attempt in range(attempts):
        try:
            with httpx.Client(
                timeout=timeout or TIMEOUT, follow_redirects=follow_redirects
            ) as client:
                resp = client.request(
                    method, url, params=params, json=json_body, data=data, headers=headers
                )
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code not in _RETRYABLE_STATUS:
                raise
            retry_after = e.response.headers.get("Retry-After")
        except httpx.TransportError as e:
            last_exc = e
            retry_after = None
        if attempt < attempts - 1:
            _sleep(_retry_delay(attempt, retry_after))
    assert last_exc is not None
    raise last_exc


def _json_or_raise(resp: httpx.Response, url: str) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as e:
        ctype = resp.headers.get("content-type", "unknown")
        raise ConnectorHTTPError(
            f"non-JSON response from {url} (content-type {ctype}): {e!s}"
        ) from e


def http_get(
    url: str,
    params: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
    *,
    cache_ttl: float = SEARCH_TTL,
) -> dict[str, Any]:
    base_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        base_headers.update(headers)
    key = cache_key("GET", url, params, sorted(base_headers.items()))
    if cache_ttl > 0:
        cached = _RESPONSE_CACHE.get(key)
        if cached is not TTLCache._MISS:
            return cached
    resp = request_with_retry("GET", url, params=params, headers=base_headers)
    payload = _json_or_raise(resp, url)
    if cache_ttl > 0:
        _RESPONSE_CACHE.set(key, payload, cache_ttl)
    return payload


def http_post_json(
    url: str,
    body: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
    *,
    cache_ttl: float = 0,
) -> dict[str, Any]:
    base_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        base_headers.update(headers)
    key = cache_key("POST", url, body, sorted(base_headers.items()))
    if cache_ttl > 0:
        cached = _RESPONSE_CACHE.get(key)
        if cached is not TTLCache._MISS:
            return cached
    resp = request_with_retry("POST", url, json_body=body, headers=base_headers)
    payload = _json_or_raise(resp, url)
    if cache_ttl > 0:
        _RESPONSE_CACHE.set(key, payload, cache_ttl)
    return payload


def http_get_text(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    *,
    cache_ttl: float = 0,
) -> str:
    base_headers = {"User-Agent": USER_AGENT}
    if headers:
        base_headers.update(headers)
    key = cache_key("GET-text", url, params, sorted(base_headers.items()))
    if cache_ttl > 0:
        cached = _RESPONSE_CACHE.get(key)
        if cached is not TTLCache._MISS:
            return cached
    resp = request_with_retry("GET", url, params=params, headers=base_headers)
    text = resp.text
    if cache_ttl > 0:
        _RESPONSE_CACHE.set(key, text, cache_ttl)
    return text


def truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ensure_keys_loaded() -> None:
    """Trigger config.load_settings so api_key file is mirrored into env."""
    try:
        from ..config import load_settings

        load_settings()
    except Exception:
        pass


class Connector(Protocol):
    """Protocol for an in-process connector exposed as a function tool."""

    jurisdiction: tuple[str, ...]
    rate_limit_per_sec: float
    cache_ttl_seconds: int

    def name(self) -> str:
        ...
