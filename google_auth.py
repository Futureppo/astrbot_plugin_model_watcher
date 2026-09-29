"""Service account authentication using the watcher's HTTPX transport."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Generator

import httpx
from google.auth import crypt, jwt

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class ServiceAccountAuth(httpx.Auth):
    """Cache OAuth tokens per entry without storing them in plugin state."""

    requires_response_body = True

    def __init__(self, key_json: str) -> None:
        """Parse a pasted service account key and prepare its local signer.

        Args:
            key_json: Complete JSON content from a service account key file.

        Raises:
            ValueError: The credential type, required fields, or key is invalid.
        """
        try:
            info = json.loads(key_json)
            if not isinstance(info, dict) or info.get("type") != "service_account":
                raise ValueError("Expected service account JSON")
            for field in ("client_email", "private_key", "project_id"):
                if not isinstance(info.get(field), str) or not info[field].strip():
                    raise ValueError("Missing service account field")
            if info.get("token_uri", TOKEN_URL) != TOKEN_URL:
                raise ValueError("Unsupported token endpoint")
            self._signer = crypt.RSASigner.from_service_account_info(info)
            self._email = info["client_email"]
            self._project = info["project_id"]
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Invalid service account JSON credential") from exc
        self._token = ""
        self._expires_at = 0.0

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Refresh tokens through the same proxy and retry one rejected token.

        Args:
            request: Catalog GET request to authenticate.

        Yields:
            Token exchange POST and authenticated catalog GET requests.

        Raises:
            ValueError: The token response has invalid fields.
            httpx.HTTPStatusError: The token exchange fails.
        """
        for attempt in range(2):
            if not self._token or time.monotonic() + 60 >= self._expires_at:
                now = int(time.time())
                assertion = jwt.encode(
                    self._signer,
                    {
                        "iss": self._email,
                        "scope": SCOPE,
                        "aud": TOKEN_URL,
                        "iat": now,
                        "exp": now + 3600,
                    },
                ).decode("ascii")
                token_response = yield httpx.Request(
                    "POST",
                    TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                    extensions=request.extensions.copy(),
                )
                token_response.raise_for_status()
                payload = token_response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Invalid OAuth token response")
                token = payload.get("access_token")
                expires = payload.get("expires_in")
                if (
                    not isinstance(token, str)
                    or not token.strip()
                    or isinstance(expires, bool)
                    or not isinstance(expires, (int, float))
                    or not math.isfinite(expires)
                    or expires <= 0
                    or str(payload.get("token_type", "Bearer")).lower() != "bearer"
                ):
                    raise ValueError("Invalid OAuth token response")
                self._token = token
                self._expires_at = time.monotonic() + expires
            request.headers["Authorization"] = f"Bearer {self._token}"
            request.headers["X-Goog-User-Project"] = self._project
            response = yield request
            if response.status_code != 401:
                return
            self._token = ""
            self._expires_at = 0.0
            if attempt == 1:
                return
