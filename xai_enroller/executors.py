import asyncio
import glob
import importlib
import os
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

import httpx

from grok_register.clearance import (
    apply_clearance_to_context,
    cached_user_agent,
    playwright_proxy_settings,
)

from .models import AuthorizationResult, AuthorizationStatus, DeviceFlow, SourceRecord


class HTTPProbeExecutor:
    def __init__(self, client: httpx.AsyncClient, max_body=64 * 1024):
        self.client = client
        self.max_body = max_body

    async def confirm(self, source: SourceRecord, flow: DeviceFlow):
        parsed = urlparse(flow.verification_url)
        if parsed.scheme != "https" or parsed.hostname != "accounts.x.ai":
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "invalid_url")
        response = await self.client.get(
            flow.verification_url,
            headers={"Cookie": f"sso={source.sso_token}"},
            follow_redirects=False,
        )
        body = await response.aread()
        if len(body) > self.max_body:
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "response_too_large")
        text = body.decode("utf-8", errors="replace").lower()
        if response.status_code == 403 and any(
            marker in text for marker in ("challenge", "cf-chl", "captcha")
        ):
            return AuthorizationResult(AuthorizationStatus.NEEDS_BROWSER, "challenge")
        if response.status_code in {301, 302, 303, 307, 308}:
            return AuthorizationResult(AuthorizationStatus.NEEDS_INTERACTION, "redirect")
        return AuthorizationResult(
            AuthorizationStatus.NEEDS_INTERACTION, "http_probe_inconclusive"
        )


class PlaywrightExecutor:
    ALLOWED_CONTROLS = frozenset({"Authorize", "Allow", "Continue", "Confirm"})
    ATTEMPT_TIMEOUT_SECONDS = 55.0
    CLOSE_TIMEOUT_SECONDS = 5.0
    # Keep confirm loops tight: most successful device verifies finish under ~15s.
    LOOP_DEADLINE_SECONDS = 28.0
    COOKIE_WAIT_MS = 250
    CONSENT_WAIT_MS = 350
    CODE_WAIT_MS = 300
    CHALLENGE_WAIT_MS = 700
    POLL_WAIT_MS = 250
    # Drop heavy assets that never participate in device verification.
    BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})
    BLOCKED_URL_MARKERS = (
        "google-analytics",
        "googletagmanager",
        "doubleclick",
        "facebook.net",
        "hotjar",
        "segment.io",
    )

    def __init__(self, concurrency=1, playwright_factory=None, executable_path=None):
        self.concurrency = concurrency
        self.playwright_factory = playwright_factory
        self.executable_path = executable_path or self._find_executable_path()
        self._playwright = None
        self._browser = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._lifecycle_lock = asyncio.Lock()

    @staticmethod
    def _find_executable_path():
        configured = os.environ.get("XAI_ENROLLER_BROWSER_EXECUTABLE")
        if configured:
            return configured
        candidates = glob.glob(os.path.expanduser("~/.cloakbrowser/chromium-*/chrome"))
        candidates.extend(
            glob.glob(
                os.path.expanduser(
                    "~/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium"
                )
            )
        )
        return sorted(candidates)[-1] if candidates else None

    async def start(self):
        async with self._lifecycle_lock:
            if self._browser is not None:
                is_connected = getattr(self._browser, "is_connected", None)
                if not callable(is_connected) or is_connected():
                    return
                await self._close_unlocked()
            factory = self.playwright_factory
            if factory is None:
                module = importlib.import_module("playwright.async_api")
                factory = module.async_playwright
            playwright = await factory().start()
            options = {"headless": True}
            if self.executable_path:
                options["executable_path"] = self.executable_path
            # REGISTER_PROXY / HTTP_PROXY / HTTPS_PROXY → this process only
            proxy = playwright_proxy_settings()
            if proxy:
                options["proxy"] = proxy
            try:
                browser = await playwright.chromium.launch(**options)
            except BaseException:
                await self._close_transport_resource(playwright.stop())
                raise
            self._playwright = playwright
            self._browser = browser

    async def close(self):
        async with self._lifecycle_lock:
            await self._close_unlocked()

    async def _close_unlocked(self):
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                await self._close_transport_resource(browser.close())
        finally:
            if playwright is not None:
                await self._close_transport_resource(playwright.stop())

    @staticmethod
    def _consume_task_result(task):
        with suppress(BaseException):
            task.result()

    @classmethod
    async def _close_transport_resource(cls, awaitable):
        task = asyncio.create_task(awaitable)
        try:
            done, _pending = await asyncio.wait(
                {task}, timeout=cls.CLOSE_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(cls._consume_task_result)
            raise
        if task in done:
            cls._consume_task_result(task)
            return True
        task.cancel()
        task.add_done_callback(cls._consume_task_result)
        return False

    async def _recycle_browser(self, browser):
        async with self._lifecycle_lock:
            if self._browser is browser:
                await self._close_unlocked()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    @classmethod
    async def _close_attempt_resources(cls, page, context):
        async def close_session():
            closers = []
            if page is not None:
                closers.append(page.close())
            if context is not None:
                closers.append(context.close())
            if closers:
                await asyncio.gather(*closers, return_exceptions=True)

        cleanup = asyncio.create_task(close_session())
        cancelled = False
        deadline = asyncio.get_running_loop().time() + cls.CLOSE_TIMEOUT_SECONDS
        while not cleanup.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({cleanup}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
        if cleanup.done():
            cls._consume_task_result(cleanup)
        else:
            # asyncio.wait_for() is not a hard deadline: it cancels the child and
            # then waits for cancellation to finish, which a wedged Playwright
            # transport may never do.  Abandon the cleanup task after one real
            # wall-clock deadline and consume its eventual result asynchronously.
            cleanup.cancel()
            cleanup.add_done_callback(cls._consume_task_result)
        if cancelled:
            raise asyncio.CancelledError
        return cleanup.done()

    @staticmethod
    async def _click_visible_exact(page, names):
        for name in names:
            try:
                button = page.get_by_role("button", name=name, exact=True)
            except TypeError:
                button = page.get_by_role("button", name=name)
            for index in range(await button.count()):
                candidate = button.nth(index) if hasattr(button, "nth") else button.first
                is_visible = getattr(candidate, "is_visible", None)
                if is_visible is None or await is_visible():
                    await candidate.click()
                    return True
        return False

    @staticmethod
    async def _click_visible_turnstile(page):
        """Click a visible Turnstile widget using the registration solver motion."""
        for selector in (
            ".cf-turnstile",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
        ):
            locator = page.locator(selector).first
            if not await locator.count() or not await locator.is_visible():
                continue
            box = await locator.bounding_box()
            if not box:
                continue
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(max(0, x - 25), max(0, y - 8))
            await page.mouse.move(x, y, steps=8)
            await page.mouse.down()
            await asyncio.sleep(0.05)
            await page.mouse.up()
            return True
        return False

    @staticmethod
    def _expanded_cookies(source):
        allowed = {
            "name",
            "value",
            "url",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        }
        cookies = []
        for source_cookie in source.cookies:
            cookie = {
                key: value for key, value in source_cookie.items() if key in allowed
            }
            if cookie.get("domain"):
                cookie.pop("url", None)
            elif cookie.get("url"):
                cookie.pop("domain", None)
                cookie.pop("path", None)
            cookies.append(cookie)
        if not cookies:
            cookies = [
                {
                    "name": "sso",
                    "value": source.sso_token,
                    "domain": "accounts.x.ai",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        seen = {
            (cookie.get("name"), cookie.get("domain"), cookie.get("path", "/"))
            for cookie in cookies
        }
        for cookie in list(cookies) if source.cookies else ():
            name = cookie.get("name", "")
            if not name.startswith("sso"):
                continue
            key = (name, ".x.ai", cookie.get("path", "/"))
            if key in seen:
                continue
            clone = dict(cookie)
            clone.pop("url", None)
            clone["domain"] = ".x.ai"
            clone.setdefault("path", "/")
            cookies.append(clone)
            seen.add(key)
        return cookies

    async def confirm(self, source: SourceRecord, flow: DeviceFlow):
        await self.start()
        async with self._semaphore:
            browser = self._browser
            attempt = asyncio.create_task(
                self._confirm_once(browser, source, flow)
            )
            try:
                done, _pending = await asyncio.wait(
                    {attempt}, timeout=self.ATTEMPT_TIMEOUT_SECONDS
                )
            except asyncio.CancelledError:
                attempt.cancel()
                attempt.add_done_callback(self._consume_task_result)
                raise
            if attempt in done:
                return attempt.result()

            # A Playwright transport can swallow cancellation while one of its
            # RPCs is wedged.  Do not let asyncio.wait_for() extend the nominal
            # timeout indefinitely: detach the attempt and replace the complete
            # browser transport before admitting the next account.
            attempt.cancel()
            attempt.add_done_callback(self._consume_task_result)
            await self._recycle_browser(browser)
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION, "confirmation_timeout"
            )

    async def _install_request_filters(self, page):
        """Skip non-essential resources so device verification spends less wall time."""

        async def route_handler(route):
            request = route.request
            resource_type = getattr(request, "resource_type", "") or ""
            url = getattr(request, "url", "") or ""
            if resource_type in self.BLOCKED_RESOURCE_TYPES or any(
                marker in url for marker in self.BLOCKED_URL_MARKERS
            ):
                await route.abort()
                return
            await route.continue_()

        with suppress(Exception):
            await page.route("**/*", route_handler)

    async def _confirm_once(self, browser, source: SourceRecord, flow: DeviceFlow):
        context = None
        page = None
        code_submitted = False
        consent_submitted = False
        challenge_clicks = 0
        try:
                context_kwargs = {}
                ua = cached_user_agent()
                if ua:
                    context_kwargs["user_agent"] = ua
                context = await browser.new_context(**context_kwargs)
                # CF cookies from external FlareSolverr prewarm (host-level)
                await apply_clearance_to_context(context)
                page = await context.new_page()
                await self._install_request_filters(page)
                await context.add_cookies(self._expanded_cookies(source))
                await page.goto(flow.verification_url, wait_until="domcontentloaded")
                deadline = (
                    asyncio.get_running_loop().time() + self.LOOP_DEADLINE_SECONDS
                )
                unknown_since = None
                while asyncio.get_running_loop().time() < deadline:
                    parsed = urlparse(page.url)
                    if parsed.scheme != "https" or not parsed.hostname or not (
                        parsed.hostname == "x.ai"
                        or parsed.hostname.endswith(".x.ai")
                    ):
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "unsafe_page"
                        )
                    query_error = parse_qs(parsed.query).get("error", [None])[0]
                    text = await page.locator("body").inner_text()
                    text_lower = text.lower()
                    page_title = getattr(page, "title", None)
                    title_lower = (
                        (await page_title()).lower() if page_title is not None else ""
                    )

                    if query_error == "rate_limited":
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "rate_limited"
                        )
                    if query_error:
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "device_verify_failed"
                        )
                    if any(
                        marker in text_lower
                        for marker in ("rate limit", "too many requests", "请求过于频繁")
                    ):
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "rate_limited"
                        )
                    if (
                        "attention required" in title_lower
                        and "cloudflare" in title_lower
                    ) or any(
                        marker in text_lower
                        for marker in (
                            "sorry, you have been blocked",
                            "unable to access x.ai",
                            "cloudflare ray id",
                        )
                    ):
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION,
                            "challenge_required",
                        )
                    if "/oauth2/device/done" in parsed.path or any(
                        marker in text_lower
                        for marker in ("device authorized", "设备已授权")
                    ):
                        return AuthorizationResult(
                            AuthorizationStatus.AUTHORIZED, "confirmed"
                        )

                    cookie_clicked = await self._click_visible_exact(
                        page,
                        (
                            "全部拒绝",
                            "拒绝全部",
                            "Reject all",
                            "Reject All",
                        ),
                    )
                    if cookie_clicked:
                        unknown_since = None
                        await page.wait_for_timeout(self.COOKIE_WAIT_MS)
                        continue

                    if "/oauth2/device/consent" in parsed.path or any(
                        marker in text_lower
                        for marker in ("authorize grok build", "授权 grok build")
                    ):
                        if not consent_submitted:
                            allowed = await self._click_visible_exact(
                                page, ("允许", "Allow", "Authorize", "Approve")
                            )
                            if allowed:
                                consent_submitted = True
                                unknown_since = None
                                await page.wait_for_timeout(self.CONSENT_WAIT_MS)
                                continue
                        else:
                            await page.wait_for_timeout(self.POLL_WAIT_MS)
                            continue

                    code_input = page.locator('input[name="user_code"]')
                    try:
                        code_input_count = await code_input.count()
                    except AttributeError:
                        if await self._click_visible_exact(
                            page, self.ALLOWED_CONTROLS
                        ):
                            return AuthorizationResult(
                                AuthorizationStatus.AUTHORIZED,
                                "confirmed",
                            )
                        code_input_count = 0
                    if code_input_count and await code_input.first.is_visible():
                        if code_submitted:
                            await page.wait_for_timeout(self.POLL_WAIT_MS)
                            continue
                        unknown_since = None
                        current = await code_input.first.input_value()
                        # fill() is far cheaper than press_sequentially and the
                        # device-code form does not require keystroke events.
                        if flow.user_code.replace("-", "") not in current.replace(
                            "-", ""
                        ):
                            await code_input.first.fill(flow.user_code)
                        submit = page.locator(
                            'button[type="submit"], input[type="submit"]'
                        )
                        if not await submit.count():
                            return AuthorizationResult(
                                AuthorizationStatus.NEEDS_INTERACTION,
                                "submit_missing",
                            )
                        predicate = lambda response: (
                            urlparse(response.url).hostname == "auth.x.ai"
                            and urlparse(response.url).path == "/oauth2/device/verify"
                        )
                        async with page.expect_response(
                            predicate, timeout=12000
                        ) as response_info:
                            code_submitted = True
                            await submit.first.click()
                            response = await response_info.value
                        location = urlparse(response.headers.get("location", ""))
                        error = parse_qs(location.query).get("error", [None])[0]
                        if error == "rate_limited":
                            return AuthorizationResult(
                                AuthorizationStatus.NEEDS_INTERACTION,
                                "rate_limited",
                            )
                        if error:
                            return AuthorizationResult(
                                AuthorizationStatus.NEEDS_INTERACTION,
                                "device_verify_failed",
                            )
                        await page.wait_for_timeout(self.CODE_WAIT_MS)
                        continue

                    if any(
                        marker in text_lower
                        for marker in (
                            "continue with email",
                            "sign in with email",
                            "使用邮箱登录",
                        )
                    ) or await page.locator('input[type="password"]').count():
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION,
                            "login_required",
                        )

                    if any(
                        marker in text_lower
                        for marker in (
                            "captcha",
                            "verify you are human",
                            "security challenge",
                            "turnstile",
                            "人机验证",
                            "安全验证",
                        )
                    ):
                        if (
                            challenge_clicks < 3
                            and await self._click_visible_turnstile(page)
                        ):
                            challenge_clicks += 1
                            unknown_since = None
                            await page.wait_for_timeout(self.CHALLENGE_WAIT_MS)
                            continue
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION,
                            "challenge_required",
                        )

                    if any(
                        marker in text_lower
                        for marker in (
                            "multi-factor",
                            "two-factor",
                            "authentication code",
                            "verification code",
                            "双重验证",
                            "验证码",
                        )
                    ):
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION,
                            "mfa_required",
                        )

                    if await self._click_visible_exact(page, self.ALLOWED_CONTROLS):
                        return AuthorizationResult(
                            AuthorizationStatus.AUTHORIZED,
                            "confirmed",
                        )

                    now = asyncio.get_running_loop().time()
                    if unknown_since is None:
                        unknown_since = now
                    elif now - unknown_since >= 2.5:
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "unknown_page"
                        )

                    await page.wait_for_timeout(self.POLL_WAIT_MS)

                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "confirmation_timeout"
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            is_timeout = (
                error.__class__.__name__ == "TimeoutError"
                and error.__class__.__module__.startswith("playwright")
            )
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION,
                "confirmation_timeout" if is_timeout else "browser_error",
            )
        finally:
            await self._close_attempt_resources(page, context)
