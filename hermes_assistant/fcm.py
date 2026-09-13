from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

logger = logging.getLogger(__name__)
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
RETRY_DELAYS = (1.0, 2.0, 4.0)


class FcmTransport:
    """Firebase HTTP v1 sender; payloads intentionally contain no event content."""

    def __init__(self, credentials_path: str, project_id: str = "", client: Any = None):
        self.credentials_path, self.project_id, self._client = credentials_path, project_id, client
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    def _account(self) -> dict[str, Any]:
        try:
            with Path(self.credentials_path).open(encoding="utf-8") as handle:
                account = json.load(handle)
        except Exception as exc:
            raise RuntimeError("Firebase credentials are unavailable") from exc
        if not isinstance(account, dict) or not account.get("client_email") or not account.get("private_key"):
            raise RuntimeError("Firebase credentials are invalid")
        return account

    async def _access_token(self, client: Any) -> str:
        async with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            account = self._account()
            now = int(time.time())
            assertion = jwt.encode(
                {"iss": account["client_email"], "sub": account["client_email"], "scope": SCOPE,
                 "aud": TOKEN_URI, "iat": now, "exp": now + 3600},
                account["private_key"], algorithm="RS256",
            )
            response = await client.post(TOKEN_URI, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
            }, timeout=10.0)
            if response.status_code != 200:
                raise RuntimeError("Firebase OAuth exchange failed")
            payload = response.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Firebase OAuth response is invalid")
            self._token = (token, time.time() + int(payload.get("expires_in", 3600)))
            return token

    @staticmethod
    def _invalid_token(response: Any) -> bool:
        try:
            error = response.json().get("error", {})
            status = str(error.get("status", "")).upper()
            details = json.dumps(error.get("details", []), sort_keys=True).lower()
            return status == "UNREGISTERED" or (status == "INVALID_ARGUMENT" and "token" in details)
        except Exception:
            return False

    async def send(self, fcm_token: str, event_id: str) -> str:
        """Return success, transient, permanent, or auth. Never logs token/content."""
        if not fcm_token:
            return "permanent"
        owned = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            try:
                bearer = await self._access_token(client)
                account = self._account()
                project = self.project_id or str(account.get("project_id") or "")
                if not project:
                    return "auth"
            except Exception:
                self._token = None
                return "auth"
            payload = {"message": {"token": fcm_token, "data": {
                "event_id": event_id, "protocol_version": "1"}, "android": {"priority": "high"}}}
            endpoint = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
            for attempt in range(len(RETRY_DELAYS) + 1):
                try:
                    response = await client.post(endpoint, json=payload, headers={
                        "Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}, timeout=15.0)
                except (httpx.HTTPError, OSError, asyncio.TimeoutError):
                    response = None
                if response is not None and response.status_code == 200:
                    return "success"
                if response is not None and response.status_code in (401, 403):
                    self._token = None
                    return "auth"
                if response is not None and response.status_code in (400, 404, 410) and self._invalid_token(response):
                    return "permanent"
                if response is not None and response.status_code == 400:
                    return "auth"
                if attempt < len(RETRY_DELAYS):
                    await asyncio.sleep(RETRY_DELAYS[attempt])
            return "transient"
        finally:
            if owned:
                await client.aclose()
