import asyncio
import json

import httpx

from xai_enroller.coordinator import EnrollmentCoordinator
from xai_enroller.executors import PlaywrightExecutor
from xai_enroller.models import SourceRecord
from xai_enroller.protocol import XAIProfile, XAIProtocol
from xai_enroller.sinks import CPAAuthFileSink


class SingleSource:
    async def records(self):
        yield SourceRecord("source-1", "sso-secret")


class FakeButton:
    async def count(self):
        return 1

    @property
    def first(self):
        return self

    async def click(self):
        return None


class FakePage:
    async def goto(self, url, wait_until):
        self.url = url

    def locator(self, selector):
        return self

    async def inner_text(self):
        return "Authorize access"

    def get_by_role(self, role, name):
        return FakeButton() if name == "Authorize" else type(
            "NoButton",
            (),
            {"count": staticmethod(lambda: asyncio.sleep(0, result=0))},
        )()

    async def close(self):
        return None


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.cookies = []

    async def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def new_page(self):
        return FakePage()

    async def close(self):
        self.browser.live_contexts -= 1


class FakeBrowser:
    def __init__(self):
        self.launches = 0
        self.live_contexts = 0
        self.max_live_contexts = 0

    async def new_context(self):
        self.live_contexts += 1
        self.max_live_contexts = max(self.max_live_contexts, self.live_contexts)
        return FakeContext(self)

    async def close(self):
        return None


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **options):
        self.browser.launches += 1
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)

    async def stop(self):
        return None


class FakePlaywrightFactory:
    def __init__(self):
        self.browser = FakeBrowser()
        self.playwright = FakePlaywright(self.browser)

    def __call__(self):
        factory = self

        class Runner:
            async def start(self):
                return factory.playwright

        return Runner()


def test_adapter_boundary_hands_xai_credential_to_cpa_without_ledger_secrets(tmp_path):
    def xai_handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                },
            )
        if request.url.path == "/oauth2/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD",
                    "verification_uri": "https://accounts.x.ai/oauth2/device",
                    "verification_uri_complete": (
                        "https://accounts.x.ai/oauth2/device?user_code=ABCD"
                    ),
                    "expires_in": 60,
                    "interval": 0,
                },
            )
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "id_token": "id-secret",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "sub": "subject-1",
                },
            )
        raise AssertionError(f"unexpected xAI request: {request.url}")

    cpa_requests = []

    def cpa_handler(request):
        cpa_requests.append(request)
        return httpx.Response(201)

    xai_client = httpx.AsyncClient(transport=httpx.MockTransport(xai_handler))
    cpa_client = httpx.AsyncClient(transport=httpx.MockTransport(cpa_handler))
    factory = FakePlaywrightFactory()
    coordinator = EnrollmentCoordinator(
        source=SingleSource(),
        protocol=XAIProtocol(xai_client, XAIProfile.default()),
        executor=PlaywrightExecutor(playwright_factory=factory),
        sink=CPAAuthFileSink(
            "https://cpa.example",
            "management-secret",
            cpa_client,
            name_secret=b"name-secret",
        ),
        ledger_path=tmp_path / "ledger.db",
        ledger_salt=b"ledger-salt",
    )

    try:
        results = asyncio.run(coordinator.run(target=1))
    finally:
        asyncio.run(xai_client.aclose())
        asyncio.run(cpa_client.aclose())

    assert results[0].status.value == "imported"
    assert factory.browser.launches == 1
    assert factory.browser.max_live_contexts == 1
    assert cpa_requests[0].url.path == "/v0/management/auth-files"
    assert cpa_requests[0].url.params["name"].startswith("xai-")
    assert json.loads(cpa_requests[0].content)["refresh_token"] == "refresh-secret"
    ledger_bytes = (tmp_path / "ledger.db").read_bytes()
    for secret in ("sso-secret", "device-secret", "access-secret", "refresh-secret", "id-secret"):
        assert secret.encode() not in ledger_bytes
