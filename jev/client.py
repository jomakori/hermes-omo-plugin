from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_RETRY_STATUSES = {429, 529}


class JevClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_s

    def call(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
        url = f"{self._base_url}/v1/systemone"
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        t0 = time.monotonic()
        result = self._attempt(url, body, headers)
        if result is None:
            return None, round((time.monotonic() - t0) * 1000, 1)
        status_code, data = result
        if status_code in _RETRY_STATUSES:
            result2 = self._attempt(url, body, headers)
            if result2 is None or result2[0] in _RETRY_STATUSES:
                return None, round((time.monotonic() - t0) * 1000, 1)
            _, data = result2
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        return data, latency_ms

    def _attempt(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {}
        except Exception:
            return None


__all__ = ["JevClient"]
