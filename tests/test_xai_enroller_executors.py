import asyncio
from contextlib import suppress

import httpx
import pytest

from xai_enroller.executors import HTTPProbeExecutor, PlaywrightExecutor
from xai_enroller.models import AuthorizationStatus, DeviceFlow, SourceRecord


def test_http_probe_classifies_403_challenge_without_submission():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="<html>challenge-platform</html>")

    executor = HTTPProbeExecutor(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    )
    source = SourceRecord("source", "sso-token")
    flow = DeviceFlow("device", "ABCD", "https://accounts.x.ai/oauth2/device?user_code=ABCD", 60, 1)
    result = asyncio.run(executor.confirm(source, flow))
    assert result.status is AuthorizationStatus.NEEDS_BROWSER
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert "sso-token" not in repr(result)


def test_http_probe_does_not_follow_redirects_or_submit_forms():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    executor = HTTPProbeExecutor(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    )
    result = asyncio.run(
        executor.confirm(
            SourceRecord("source", "token"),
            DeviceFlow("device", "ABCD", "https://accounts.x.ai/oauth2/device", 60, 1),
        )
    )
    assert result.status is AuthorizationStatus.NEEDS_INTERACTION


class FakeButton:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count

    @property
    def first(self):
        return self

    async def click(self):
        return None


class FakePage:
    def __init__(self):
        self.closed = False

    async def goto(self, url, wait_until):
        self.url = url

    def locator(self, selector):
        return self

    async def inner_text(self):
        return "Authorize access"

    def get_by_role(self, role, name):
        return FakeButton(1 if name == "Authorize" else 0)

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.cookies = []
        self.page = FakePage()
        self.closed = False

    async def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.launches = 0
        self.contexts = []
        self.closed = False

    async def new_context(self):
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_options = None

    async def launch(self, **options):
        self.launch_options = options
        self.browser.launches += 1
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)
        self.stopped = False

    async def stop(self):
        self.stopped = True


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


class FailingPageContext(FakeContext):
    async def new_page(self):
        raise RuntimeError("page creation failed")


class FailingPageBrowser(FakeBrowser):
    async def new_context(self):
        context = FailingPageContext()
        self.contexts.append(context)
        return context


def test_playwright_executor_isolates_context_and_scopes_exact_cookie():
    factory = FakePlaywrightFactory()
    executor = PlaywrightExecutor(playwright_factory=factory)
    source = SourceRecord("source", "secret-token")
    flow = DeviceFlow("device", "ABCD", "https://accounts.x.ai/oauth2/device", 60, 1)
    result = asyncio.run(executor.confirm(source, flow))
    asyncio.run(executor.close())
    assert result.status is AuthorizationStatus.AUTHORIZED
    assert factory.browser.launches == 1
    context = factory.browser.contexts[0]
    assert context.cookies == [{
        "name": "sso",
        "value": "secret-token",
        "domain": "accounts.x.ai",
        "path": "/",
        "secure": True,
        "httpOnly": True,
    }]
    assert context.page.closed
    assert context.closed
    assert factory.browser.closed


def test_playwright_executor_closes_context_when_page_creation_fails():
    factory = FakePlaywrightFactory()
    factory.browser = FailingPageBrowser()
    factory.playwright = FakePlaywright(factory.browser)
    executor = PlaywrightExecutor(playwright_factory=factory)

    result = asyncio.run(
        executor.confirm(
            SourceRecord("source", "secret-token"),
            DeviceFlow("device", "ABCD", "https://accounts.x.ai/oauth2/device", 60, 1),
        )
    )
    asyncio.run(executor.close())

    assert result.status is AuthorizationStatus.NEEDS_INTERACTION
    assert factory.browser.contexts[0].closed


def test_playwright_executor_launches_configured_fingerprint_browser():
    factory = FakePlaywrightFactory()
    executor = PlaywrightExecutor(
        playwright_factory=factory,
        executable_path="/opt/cloakbrowser/chrome",
    )

    asyncio.run(executor.start())
    asyncio.run(executor.close())

    assert factory.playwright.chromium.launch_options == {
        "headless": True,
        "executable_path": "/opt/cloakbrowser/chrome",
    }


def test_playwright_executor_uses_fingerprint_browser_path_from_environment(monkeypatch):
    monkeypatch.setenv("XAI_ENROLLER_BROWSER_EXECUTABLE", "/opt/cloakbrowser/chrome")
    factory = FakePlaywrightFactory()
    executor = PlaywrightExecutor(playwright_factory=factory)

    asyncio.run(executor.start())
    asyncio.run(executor.close())

    assert factory.playwright.chromium.launch_options == {
        "headless": True,
        "executable_path": "/opt/cloakbrowser/chrome",
    }


def test_playwright_executor_finds_macos_cloakbrowser_binary(monkeypatch):
    macos_binary = (
        "/Users/test/.cloakbrowser/chromium-145/Chromium.app/Contents/MacOS/Chromium"
    )
    monkeypatch.delenv("XAI_ENROLLER_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        "xai_enroller.executors.glob.glob",
        lambda pattern: [macos_binary] if "Chromium.app" in pattern else [],
    )

    assert PlaywrightExecutor._find_executable_path() == macos_binary


def test_playwright_attempt_cleanup_defers_repeated_cancellation():
    class Page:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def close(self):
            self.entered.set()
            await self.release.wait()
            self.closed = True

    class Context:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def scenario():
        page = Page()
        context = Context()
        task = asyncio.create_task(
            PlaywrightExecutor._close_attempt_resources(page, context)
        )
        await page.entered.wait()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        page.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert page.closed
        assert context.closed

    asyncio.run(scenario())


def test_playwright_attempt_cleanup_has_a_true_wall_clock_deadline(monkeypatch):
    class Page:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def close(self):
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    # Playwright transports can remain stuck while cancellation
                    # propagates.  A hard deadline must not await that forever.
                    continue

    async def scenario():
        monkeypatch.setattr(PlaywrightExecutor, "CLOSE_TIMEOUT_SECONDS", 0.02)
        page = Page()
        task = asyncio.create_task(
            PlaywrightExecutor._close_attempt_resources(page, None)
        )
        await page.entered.wait()
        done, _pending = await asyncio.wait({task}, timeout=0.10)
        completed_within_deadline = task in done

        page.release.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert completed_within_deadline

    asyncio.run(scenario())


def test_playwright_confirmation_has_hard_deadline_and_restarts_browser(monkeypatch):
    class StubbornGotoPage(FakePage):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def goto(self, url, wait_until):
            self.url = url
            self.entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    # Model a wedged Playwright transport that does not finish
                    # propagating task cancellation.
                    continue

    class StubbornGotoContext(FakeContext):
        def __init__(self):
            super().__init__()
            self.page = StubbornGotoPage()

    class StubbornGotoBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.close_entered = asyncio.Event()
            self.close_release = asyncio.Event()

        async def new_context(self):
            context = StubbornGotoContext()
            self.contexts.append(context)
            return context

        async def close(self):
            self.close_entered.set()
            while not self.close_release.is_set():
                try:
                    await self.close_release.wait()
                except asyncio.CancelledError:
                    continue
            self.closed = True

    class RotatingPlaywrightFactory:
        def __init__(self):
            self.browsers = [StubbornGotoBrowser(), FakeBrowser()]
            self.playwrights = [FakePlaywright(browser) for browser in self.browsers]
            self.starts = 0

        def __call__(self):
            factory = self

            class Runner:
                async def start(self):
                    playwright = factory.playwrights[factory.starts]
                    factory.starts += 1
                    return playwright

            return Runner()

    async def scenario():
        monkeypatch.setattr(PlaywrightExecutor, "ATTEMPT_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(PlaywrightExecutor, "CLOSE_TIMEOUT_SECONDS", 0.02)
        factory = RotatingPlaywrightFactory()
        executor = PlaywrightExecutor(playwright_factory=factory)
        source = SourceRecord("source", "secret-token")
        flow = DeviceFlow(
            "device", "ABCD", "https://accounts.x.ai/oauth2/device", 60, 1
        )

        first = asyncio.create_task(executor.confirm(source, flow))
        while not factory.browsers[0].contexts:
            await asyncio.sleep(0)
        stuck_page = factory.browsers[0].contexts[0].page
        await stuck_page.entered.wait()
        done, _pending = await asyncio.wait({first}, timeout=0.10)
        completed_within_deadline = first in done

        stuck_page.release.set()
        factory.browsers[0].close_release.set()
        first_result = await first
        await asyncio.sleep(0)
        second_result = await executor.confirm(source, flow)
        await executor.close()

        assert completed_within_deadline
        assert first_result.status is AuthorizationStatus.NEEDS_INTERACTION
        assert first_result.reason_code == "confirmation_timeout"
        assert second_result.status is AuthorizationStatus.AUTHORIZED
        assert factory.starts == 2
        assert factory.browsers[0].close_entered.is_set()
        assert factory.browsers[0].closed
        assert factory.playwrights[0].stopped

    asyncio.run(scenario())
