import base64
import json

import httpx
import pytest

from xai_enroller.models import AuthorizationStatus
from xai_enroller.protocol import XAIProtocol, XAIProfile


def _unsigned_jwt(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_default_profile_matches_observed_grok_cli_device_contract():
    profile = XAIProfile.default()

    assert profile.client_id == "b1a00492-073a-47ea-816f-4c329264a828"
    assert profile.scope == "openid profile email offline_access grok-cli:access api:access"
    assert profile.version == "grok-cli-device-v1"


def test_device_flow_uses_discovered_endpoints_and_profile_scope():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                },
            )
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "expires_in": 60,
                "interval": 1,
            },
        )

    profile = XAIProfile(client_id="client", scope="openid offline_access")
    protocol = XAIProtocol(httpx.AsyncClient(transport=httpx.MockTransport(handler)), profile)

    async def run():
        flow = await protocol.start_device_flow()
        return flow

    flow = __import__("asyncio").run(run())
    assert flow.verification_url == "https://accounts.x.ai/oauth2/device?user_code=ABCD"
    assert dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(requests[1].content.decode())) == {
        "client_id": "client",
        "scope": "openid offline_access",
    }


def test_device_flow_uses_configured_poll_interval_when_issuer_omits_it():
    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                },
            )
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "expires_in": 60,
            },
        )

    protocol = XAIProtocol(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        XAIProfile(client_id="client", scope="openid"),
        default_poll_interval=7,
    )

    flow = __import__("asyncio").run(protocol.start_device_flow())

    assert flow.interval == 7


def test_device_flow_preserves_complete_verification_url():
    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                },
            )
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://accounts.x.ai/oauth2/device",
                "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD&state=issuer",
                "expires_in": 60,
            },
        )

    protocol = XAIProtocol(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        XAIProfile(client_id="client", scope="openid"),
    )

    flow = __import__("asyncio").run(protocol.start_device_flow())

    assert flow.verification_url == (
        "https://accounts.x.ai/oauth2/device?user_code=ABCD&state=issuer"
    )


def test_xai_url_allowlist_rejects_missing_hostname():
    assert XAIProtocol._allowed_url("https:///oauth2/device") is False


@pytest.mark.parametrize(
    "discovery",
    [
        {
            "device_authorization_endpoint": "http://auth.x.ai/device",
            "token_endpoint": "https://auth.x.ai/token",
        },
        {
            "device_authorization_endpoint": "https://evil.example/device",
            "token_endpoint": "https://auth.x.ai/token",
        },
    ],
)
def test_discovery_rejects_non_xai_or_non_https(discovery):
    def handler(request):
        return httpx.Response(200, json=discovery)

    protocol = XAIProtocol(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        XAIProfile(client_id="client", scope="openid"),
    )
    with pytest.raises(ValueError, match="allowlist"):
        __import__("asyncio").run(protocol.discover())


def test_token_polling_maps_pending_slow_down_and_denial():
    responses = iter(
        [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(400, json={"error": "access_denied"}),
        ]
    )

    def handler(request):
        return next(responses)

    protocol = XAIProtocol(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        XAIProfile(client_id="client", scope="openid"),
        sleep=lambda _: __import__("asyncio").sleep(0),
    )
    with pytest.raises(RuntimeError, match="oauth_denied"):
        __import__("asyncio").run(
            protocol.poll_token(
                endpoint="https://auth.x.ai/oauth2/token",
                flow=type("Flow", (), {"device_code": "device", "interval": 0})(),
                timeout=1,
            )
        )


def test_token_response_requires_refresh_token_and_never_exposes_body():
    def handler(request):
        return httpx.Response(200, json={"access_token": "access", "token_type": "Bearer"})

    protocol = XAIProtocol(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        XAIProfile(client_id="client", scope="openid"),
    )
    with pytest.raises(RuntimeError) as error:
        __import__("asyncio").run(
            protocol.poll_token(
                endpoint="https://auth.x.ai/oauth2/token",
                flow=type("Flow", (), {"device_code": "device", "interval": 0})(),
                timeout=1,
            )
        )
    assert "access" not in str(error.value)


def test_credential_subject_prefers_id_token_subject():
    credential = XAIProtocol._credential(
        {
            "access_token": _unsigned_jwt({"principal_id": "access-subject"}),
            "refresh_token": "refresh",
            "id_token": _unsigned_jwt(
                {"sub": "stable-subject", "email": "ignored@example.test"}
            ),
            "sub": "response-subject",
        },
        "https://auth.x.ai/oauth2/token",
    )

    assert credential.subject == "stable-subject"


def test_credential_subject_falls_back_to_access_token_principal_id():
    credential = XAIProtocol._credential(
        {
            "access_token": _unsigned_jwt({"principal_id": "stable-principal"}),
            "refresh_token": "refresh",
            "id_token": "malformed",
            "sub": "response-subject",
        },
        "https://auth.x.ai/oauth2/token",
    )

    assert credential.subject == "stable-principal"


def test_credential_subject_ignores_email_and_falls_back_to_response_subject():
    credential = XAIProtocol._credential(
        {
            "access_token": "not-a-jwt",
            "refresh_token": "refresh",
            "id_token": _unsigned_jwt({"email": "ignored@example.test"}),
            "sub": "response-subject",
        },
        "https://auth.x.ai/oauth2/token",
    )

    assert credential.subject == "response-subject"
