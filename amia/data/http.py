"""HTTP client with polite rate limiting and retries."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, headers: dict[str, str] | None = None, min_interval: float = 0.0, timeout: float = 60.0):
        self.client = httpx.Client(headers=headers or {}, timeout=timeout, follow_redirects=True)
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, params: dict[str, Any] | None = None, retries: int = 4) -> httpx.Response:
        delay = 2.0
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == retries:
                    raise
                log.warning("GET %s failed (%s); retrying in %.0fs", url, e, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    resp.raise_for_status()
                retry_after = float(resp.headers.get("retry-after", delay))
                log.warning("GET %s -> %s; retrying in %.0fs", url, resp.status_code, retry_after)
                time.sleep(retry_after)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self.get(url, params).json()
