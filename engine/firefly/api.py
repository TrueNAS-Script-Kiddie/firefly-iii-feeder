"""
Minimal Firefly III REST API client (stdlib only).

Raises FireflyAuthError on 401/403 and FireflyUnavailableError when the
server cannot be reached or answers 5xx, so callers can stop the whole run
instead of failing row by row (the 5xx is in its status, for callers that check
whether only one request is the problem).
"""

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT_SECONDS = 60


class FireflyAuthError(Exception):
    pass


class FireflyUnavailableError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        # The 5xx Firefly answered; None when it was not reached or the answer was not the API
        self.status = status


class FireflyClient:
    def __init__(self, base_url: str, token: str) -> None:
        if not base_url or not token:
            raise FireflyAuthError("FIREFLY_URL or FIREFLY_TOKEN missing in config/app.env")
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: int = TIMEOUT_SECONDS
    ) -> tuple[int, dict[str, Any]]:
        """Return (status, json_body). 4xx other than auth are returned, not raised."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise FireflyUnavailableError(f"{method} {path}: {exc}") from exc

        if status in (401, 403):
            raise FireflyAuthError(f"{method} {path}: HTTP {status} (token refused or expired)")
        if status >= 500:
            raise FireflyUnavailableError(f"{method} {path}: HTTP {status}", status)
        try:
            return status, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            # Non-JSON 2xx/4xx usually means a proxy or login page, not the API
            raise FireflyUnavailableError(f"{method} {path}: HTTP {status}, non-JSON response") from None

    def get_all(self, path: str) -> list[dict[str, Any]]:
        """GET every page of a list endpoint."""
        items: list[dict[str, Any]] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            status, body = self.request("GET", f"{path}{separator}limit=500&page={page}")
            if status != 200:
                raise FireflyUnavailableError(f"GET {path}: HTTP {status}: {body}")
            items.extend(body.get("data", []))
            pagination = body.get("meta", {}).get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                return items
            page += 1
