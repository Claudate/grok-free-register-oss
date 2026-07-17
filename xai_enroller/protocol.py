import asyncio
import base64
import binascii
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import urlencode

import httpx

from .models import DeviceFlow, OAuthCredential


@dataclass(frozen=True)
class XAIProfile:
    client_id: str
    scope: str
    version: str = "xai-device-v1"

    @classmethod
    def default(cls):
        return cls(
            client_id="b1a00492-073a-47ea-816f-4c329264a828",
            scope="openid profile email offline_access grok-cli:access api:access",
            version="grok-cli-device-v1",
        )


class XAIProtocol:
    DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
    MAX_BODY = 64 * 1024

    def __init__(
        self,
        client: httpx.AsyncClient,
        profile: XAIProfile,
        sleep=None,
        default_poll_interval=5.0,
    ):
        self.client = client
        self.profile = profile
        self.sleep = sleep or asyncio.sleep
        self.default_poll_interval = float(default_poll_interval)
        self._discovered = None

    @staticmethod
    def _allowed_url(value):
        from urllib.parse import urlparse

        parsed = urlparse(value)
        hostname = parsed.hostname
        return bool(hostname) and parsed.scheme == "https" and (
            hostname == "x.ai" or hostname.endswith(".x.ai")
        )

    async def _json(self, response):
        body = await response.aread()
        if len(body) > self.MAX_BODY:
            raise RuntimeError("response body exceeds limit")
        try:
            return json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid JSON response") from exc

    async def discover(self):
        response = await self.client.get(self.DISCOVERY_URL, follow_redirects=False)
        if response.status_code // 100 != 2:
            raise RuntimeError("discovery rejected")
        document = await self._json(response)
        device_endpoint = document.get("device_authorization_endpoint")
        token_endpoint = document.get("token_endpoint")
        if not device_endpoint or not token_endpoint or not all(
            self._allowed_url(url) for url in (device_endpoint, token_endpoint)
        ):
            raise ValueError("discovery endpoint failed x.ai HTTPS allowlist")
        self._discovered = (device_endpoint, token_endpoint)
        return self._discovered

    async def start_device_flow(self):
        device_endpoint, token_endpoint = await self.discover()
        response = await self.client.post(
            device_endpoint,
            data={"client_id": self.profile.client_id, "scope": self.profile.scope},
            follow_redirects=False,
        )
        if response.status_code // 100 != 2:
            raise RuntimeError("device authorization rejected")
        document = await self._json(response)
        try:
            device_code = document["device_code"]
            user_code = document["user_code"]
            base_url = document.get("verification_uri") or document["verification_url"]
            expires_in = int(document["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid device authorization response") from exc
        if not self._allowed_url(base_url):
            raise ValueError("verification URL failed x.ai HTTPS allowlist")
        interval = float(document.get("interval", self.default_poll_interval))
        verification_url = document.get("verification_uri_complete")
        if verification_url is None:
            verification_url = (
                f"{base_url}{'&' if '?' in base_url else '?'}"
                f"{urlencode({'user_code': user_code})}"
            )
        if not self._allowed_url(verification_url):
            raise ValueError("verification URL failed x.ai HTTPS allowlist")
        return DeviceFlow(device_code, user_code, verification_url, expires_in, interval, token_endpoint)

    async def poll_token(self, *, endpoint, flow, timeout):
        deadline = time.monotonic() + timeout
        interval = max(0.0, float(flow.interval))
        while time.monotonic() < deadline:
            response = await self.client.post(
                endpoint,
                data={
                    "client_id": self.profile.client_id,
                    "device_code": flow.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                follow_redirects=False,
            )
            document = await self._json(response)
            if response.status_code // 100 == 2:
                return self._credential(document, endpoint)
            error = document.get("error")
            if error in {"authorization_pending", "slow_down"}:
                interval += 1 if error == "slow_down" else 0
                await self.sleep(interval)
                continue
            if error == "access_denied":
                raise RuntimeError("oauth_denied")
            if error == "expired_token":
                raise RuntimeError("oauth_expired")
            raise RuntimeError("oauth_rejected")
        raise RuntimeError("oauth_expired")

    @staticmethod
    def _jwt_subject(token):
        if not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 3 or not parts[1]:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(claims, dict):
            return None
        for claim in ("sub", "principal_id"):
            value = claims.get(claim)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _credential(document, endpoint):
        if not document.get("access_token") or not document.get("refresh_token"):
            raise RuntimeError("oauth_rejected")
        now = datetime.now(timezone.utc)
        expires_in = int(document.get("expires_in", 3600))
        expires_at = datetime.fromtimestamp(now.timestamp() + expires_in, timezone.utc).isoformat().replace("+00:00", "Z")
        subject = (
            XAIProtocol._jwt_subject(document.get("id_token"))
            or XAIProtocol._jwt_subject(document["access_token"])
            or document.get("sub")
        )
        return OAuthCredential(
            access_token=document["access_token"],
            refresh_token=document["refresh_token"],
            id_token=document.get("id_token"),
            token_type=document.get("token_type"),
            expires_in=expires_in,
            expires_at=expires_at,
            last_refresh=now.isoformat().replace("+00:00", "Z"),
            subject=subject,
            token_endpoint=endpoint,
        )
