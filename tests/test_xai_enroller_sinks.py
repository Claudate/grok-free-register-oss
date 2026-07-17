import asyncio
import json

import httpx

from xai_enroller.models import OAuthCredential
from xai_enroller.sinks import CPAAuthFileSink, SinkError


def credential():
    return OAuthCredential(
        access_token="access",
        refresh_token="refresh",
        id_token="id-token",
        token_type="Bearer",
        expires_in=3600,
        expires_at="2026-07-11T00:00:00Z",
        last_refresh="2026-07-11T00:00:00Z",
        subject="subject",
        token_endpoint="https://auth.x.ai/oauth2/token",
    )


def test_cpa_sink_builds_pinned_document_and_safe_name():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201)

    sink = CPAAuthFileSink(
        "https://cpa.example",
        "management-secret",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        name_secret=b"name-secret",
    )
    receipt = asyncio.run(sink.store(credential()))
    assert requests[0].url.path == "/v0/management/auth-files"
    assert requests[0].headers["authorization"] == "Bearer management-secret"
    assert requests[0].url.params["name"].startswith("xai-")
    assert requests[0].url.params["name"].endswith(".json")
    assert json.loads(requests[0].content) == {
        "type": "xai",
        "access_token": "access",
        "refresh_token": "refresh",
        "id_token": "id-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "expired": "2026-07-11T00:00:00Z",
        "last_refresh": "2026-07-11T00:00:00Z",
        "sub": "subject",
        "base_url": "https://api.x.ai/v1",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
        "auth_kind": "oauth",
    }
    assert "access" not in repr(receipt)


def test_cpa_sink_maps_non_2xx_without_retry_or_secret_leak():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(500, text="access")

    sink = CPAAuthFileSink(
        "https://cpa.example",
        "management-secret",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        name_secret=b"name-secret",
    )
    try:
        asyncio.run(sink.store(credential()))
    except SinkError as error:
        assert "access" not in str(error)
    else:
        raise AssertionError("expected SinkError")
    assert count == 1
