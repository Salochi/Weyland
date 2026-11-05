"""Minimal Plaid API client.

Plaid's API is JSON-over-POST, so we call it directly instead of pulling in the
generated SDK. Credentials are injected per request and never appear in errors.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings

PLAID_VERSION = "2020-09-14"


class PlaidError(Exception):
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self.error_type = payload.get("error_type", "UNKNOWN")
        self.error_code = payload.get("error_code", "UNKNOWN")
        self.display_message = payload.get("display_message")
        self.error_message = payload.get("error_message", "")
        self.request_id = payload.get("request_id")
        super().__init__(f"{self.error_type}/{self.error_code}: {self.error_message}")


class PlaidClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._http = httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Plaid-Version": PLAID_VERSION, "User-Agent": "weyland/0.1"},
            transport=transport,
        )

    @property
    def plaid_env(self) -> str:
        return self._settings.plaid_env

    def call(self, path: str, **body: Any) -> dict[str, Any]:
        payload = {"client_id": self._settings.client_id, "secret": self._settings.secret, **body}
        resp = self._http.post(path, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = {"error_type": "HTTP", "error_code": str(resp.status_code), "error_message": "non-JSON response"}
        if resp.status_code != 200:
            raise PlaidError(resp.status_code, data)
        return data

    def close(self) -> None:
        self._http.close()
