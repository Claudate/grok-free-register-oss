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
    next_playwright_proxy_settings,
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


class HTTPProtocolExecutor:
    """Device-verify over pure HTTP (no browser).

    accounts.x.ai HTML is CF-gated (often 403 without browser TLS/clearance), but
    auth.x.ai form endpoints accept the SSO cookie:

      POST /oauth2/device/verify  → 303 consent
      POST /oauth2/device/approve → 303 done  (hidden action must be ``allow``)

    Polling the token endpoint is still done by XAIProtocol after AUTHORIZED.
    """

    VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
    APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
    DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )

    def __init__(self, client: httpx.AsyncClient, concurrency: int = 2):
        self.client = client
        self.concurrency = max(1, int(concurrency))
        self._semaphore = asyncio.Semaphore(self.concurrency)

    def _browser_headers(self, *, referer: str) -> dict[str, str]:
        ua = ""
        with suppress(Exception):
            ua = cached_user_agent() or ""
        return {
            "User-Agent": ua or self.DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://accounts.x.ai",
            "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded",
        }

    def _cookie_header(self, source: SourceRecord) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        if source.cookies:
            for cookie in source.cookies:
                name = str(cookie.get("name") or "").strip()
                if not name or name in seen:
                    continue
                parts.append(f"{name}={cookie.get('value') or ''}")
                seen.add(name)
        if "sso" not in seen and source.sso_token:
            parts.insert(0, f"sso={source.sso_token}")
        elif source.sso_token and "sso" in seen:
            # Prefer the live source token over a possibly stale session cookie.
            rebuilt = [f"sso={source.sso_token}"]
            for item in parts:
                if not item.startswith("sso="):
                    rebuilt.append(item)
            parts = rebuilt
        return "; ".join(parts)

    async def confirm(self, source: SourceRecord, flow: DeviceFlow):
        parsed = urlparse(flow.verification_url)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "x.ai" or parsed.hostname.endswith(".x.ai")
        ):
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION, "invalid_url"
            )
        if not source.sso_token:
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION, "login_required"
            )

        cookie = self._cookie_header(source)
        referer = flow.verification_url
        headers = self._browser_headers(referer=referer)
        headers["Cookie"] = cookie

        async with self._semaphore:
            try:
                verify = await self.client.post(
                    self.VERIFY_URL,
                    data={"user_code": flow.user_code},
                    headers=headers,
                    follow_redirects=False,
                )
            except Exception:
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "browser_error"
                )

            location = verify.headers.get("location") or ""
            loc = urlparse(location)
            error = parse_qs(loc.query).get("error", [None])[0]
            if error == "rate_limited":
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "rate_limited"
                )
            if error:
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "device_verify_failed"
                )
            if verify.status_code not in {301, 302, 303, 307, 308}:
                body = ""
                with suppress(Exception):
                    body = (await verify.aread()).decode("utf-8", errors="replace")[:800]
                text = body.lower()
                if verify.status_code == 403 and any(
                    marker in text for marker in ("challenge", "cf-chl", "captcha")
                ):
                    return AuthorizationResult(
                        AuthorizationStatus.NEEDS_BROWSER, "challenge"
                    )
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "device_verify_failed"
                )
            if "/oauth2/device/done" in (loc.path or ""):
                return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")

            consent_referer = (
                location
                if location.startswith("http")
                else f"https://accounts.x.ai{location}"
                if location.startswith("/")
                else f"https://accounts.x.ai/oauth2/device/consent?user_code={flow.user_code}"
            )
            approve_headers = self._browser_headers(referer=consent_referer)
            approve_headers["Cookie"] = cookie
            try:
                approve = await self.client.post(
                    self.APPROVE_URL,
                    data={
                        "user_code": flow.user_code,
                        "action": "allow",
                        "principal_type": "User",
                        "principal_id": "",
                    },
                    headers=approve_headers,
                    follow_redirects=False,
                )
            except Exception:
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "browser_error"
                )

            approve_loc = approve.headers.get("location") or ""
            approve_parsed = urlparse(approve_loc)
            approve_error = parse_qs(approve_parsed.query).get("error", [None])[0]
            if approve_error == "rate_limited":
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "rate_limited"
                )
            if approve_error:
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "device_verify_failed"
                )

            body = ""
            with suppress(Exception):
                body = (await approve.aread()).decode("utf-8", errors="replace")[:800]
            text = body.lower()
            if "invalid action" in text:
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "consent_invalid_action"
                )
            if approve.status_code // 100 == 2 and (
                "device authorized" in text or "设备已授权" in text
            ):
                return AuthorizationResult(AuthorizationStatus.AUTHORIZED, "confirmed")
            if approve.status_code in {301, 302, 303, 307, 308}:
                path = approve_parsed.path or ""
                if "/oauth2/device/done" in path or "device/done" in path:
                    return AuthorizationResult(
                        AuthorizationStatus.AUTHORIZED, "confirmed"
                    )
                # Some edges bounce through accounts again; treat any non-error
                # redirect after action=allow as success — token poll is the truth.
                if not approve_error:
                    return AuthorizationResult(
                        AuthorizationStatus.AUTHORIZED, "confirmed"
                    )
            if approve.status_code == 403 and any(
                marker in text for marker in ("challenge", "cf-chl", "captcha")
            ):
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_BROWSER, "challenge"
                )
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION, "unknown_page"
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

    @staticmethod
    def _browser_connected(browser) -> bool:
        if browser is None:
            return False
        is_connected = getattr(browser, "is_connected", None)
        if not callable(is_connected):
            return True
        try:
            return bool(is_connected())
        except Exception:
            return False

    @staticmethod
    def _is_transport_error(error) -> bool:
        """Node driver EPIPE / closed target / killed browser — recover by relaunch."""
        if error is None:
            return False
        name = type(error).__name__
        if name in {
            "TargetClosedError",
            "Error",
            "Exception",
            "BrowserError",
            "TimeoutError",
        }:
            # Narrow by message: bare Exception is too wide, so require markers.
            pass
        message = str(error).lower()
        markers = (
            "epipe",
            "broken pipe",
            "target closed",
            "browser has been closed",
            "browser closed",
            "connection closed",
            "pipe closed",
            "protocol error",
            "websocket",
            "playwright connection",
            "chromium",
            "browser.new_context",
            "browser.new_page",
        )
        if any(marker in message for marker in markers):
            return True
        # Playwright TargetClosedError and driver disconnects.
        return name in {"TargetClosedError", "BrowserClosedError"}

    async def start(self):
        async with self._lifecycle_lock:
            if self._browser_connected(self._browser):
                return
            if self._browser is not None or self._playwright is not None:
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
        # Never let Node EPIPE / closed-pipe errors escape cleanup.
        try:
            if browser is not None:
                await self._close_transport_resource(browser.close())
        except Exception:
            pass
        finally:
            try:
                if playwright is not None:
                    await self._close_transport_resource(playwright.stop())
            except Exception:
                pass

    @staticmethod
    def _consume_task_result(task):
        with suppress(BaseException):
            task.result()

    @classmethod
    async def _close_transport_resource(cls, awaitable):
        try:
            task = asyncio.create_task(awaitable)
        except Exception:
            return False
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

    async def _recycle_browser(self, browser=None):
        async with self._lifecycle_lock:
            if browser is None or self._browser is browser or not self._browser_connected(
                self._browser
            ):
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

    @classmethod
    async def _submit_device_consent(cls, page, action="allow"):
        """Approve/deny the device-consent form.

        accounts.x.ai renders a POST form to auth.x.ai/oauth2/device/approve with a
        hidden ``action`` input that starts empty. Client JS is supposed to stamp
        ``allow``/``deny`` on click; under higher RTT (or before hydration) a bare
        click can POST ``action=`` and the server replies plain ``Invalid action``.
        Force the hidden field first, then click the visible control.
        """
        action = "allow" if str(action).lower() not in {"deny", "拒绝"} else "deny"
        try:
            await page.evaluate(
                """(value) => {
                    const forms = Array.from(document.querySelectorAll('form'));
                    const form = forms.find((item) => {
                        const target = (item.getAttribute('action') || item.action || '');
                        return target.includes('device/approve') || target.includes('oauth2/device');
                    }) || forms[0];
                    if (!form) return false;
                    let input = form.querySelector('input[name="action"]');
                    if (!input) {
                        input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = 'action';
                        form.appendChild(input);
                    }
                    input.value = value;
                    return true;
                }""",
                action,
            )
        except Exception:
            pass
        labels = (
            ("允许", "Allow", "Authorize", "Approve")
            if action == "allow"
            else ("拒绝", "Deny", "Cancel")
        )
        if await cls._click_visible_exact(page, labels):
            return True
        try:
            submitted = await page.evaluate(
                """() => {
                    const forms = Array.from(document.querySelectorAll('form'));
                    const form = forms.find((item) => {
                        const target = (item.getAttribute('action') || item.action || '');
                        return target.includes('device/approve') || target.includes('oauth2/device');
                    }) || forms[0];
                    if (!form) return false;
                    if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                    } else {
                        form.submit();
                    }
                    return true;
                }"""
            )
            return bool(submitted)
        except Exception:
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
        # Relaunch if previous account killed the Chromium / Node driver pipe.
        for launch_try in range(2):
            try:
                await self.start()
                break
            except Exception as error:
                if launch_try == 0 and self._is_transport_error(error):
                    await self._recycle_browser()
                    continue
                return AuthorizationResult(
                    AuthorizationStatus.NEEDS_INTERACTION, "browser_error"
                )
        async with self._semaphore:
            browser = self._browser
            if not self._browser_connected(browser):
                await self._recycle_browser(browser)
                try:
                    await self.start()
                except Exception:
                    return AuthorizationResult(
                        AuthorizationStatus.NEEDS_INTERACTION, "browser_error"
                    )
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
                try:
                    return attempt.result()
                except Exception as error:
                    # Dead browser / EPIPE from Node driver: recycle and soft-fail.
                    if self._is_transport_error(error):
                        await self._recycle_browser(browser)
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION, "browser_error"
                        )
                    raise

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
                # Per-attempt proxy rotation when PROXY_FILE / multi-proxy pool is set
                pw_proxy, _assigned = next_playwright_proxy_settings()
                if pw_proxy:
                    context_kwargs["proxy"] = pw_proxy
                context = await browser.new_context(**context_kwargs)
                # CF cookies from FlareSolverr when CLEARANCE_ENABLED=1.
                try:
                    from grok_register.clearance import clearance_enabled, prewarm_clearance

                    if clearance_enabled():
                        await asyncio.to_thread(prewarm_clearance)
                except Exception:
                    pass
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

                    if "invalid action" in text_lower:
                        # Empty/missing consent action; retry once with forced field.
                        if not consent_submitted:
                            if await self._submit_device_consent(page, "allow"):
                                consent_submitted = True
                                unknown_since = None
                                await page.wait_for_timeout(self.CONSENT_WAIT_MS)
                                continue
                        return AuthorizationResult(
                            AuthorizationStatus.NEEDS_INTERACTION,
                            "consent_invalid_action",
                        )

                    if "/oauth2/device/consent" in parsed.path or any(
                        marker in text_lower
                        for marker in ("authorize grok build", "授权 grok build")
                    ):
                        if not consent_submitted:
                            allowed = await self._submit_device_consent(page, "allow")
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
                            "login with email",
                            "log into your account",
                            "log in to your account",
                            "login with google",
                            "login with apple",
                            "使用邮箱登录",
                            "登录你的账户",
                            "登录您的账户",
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
            if self._is_transport_error(error) or (
                not is_timeout and "closed" in str(error).lower()
            ):
                # Signal outer confirm() to recycle the shared browser.
                raise
            return AuthorizationResult(
                AuthorizationStatus.NEEDS_INTERACTION,
                "confirmation_timeout" if is_timeout else "browser_error",
            )
        finally:
            with suppress(Exception):
                await self._close_attempt_resources(page, context)
