import asyncio
import json
import sys
import tempfile
import types
import unittest

from grok_register.core.observer import Metrics


playwright_pkg = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
playwright_async_api.async_playwright = lambda: None
sys.modules.setdefault("playwright", playwright_pkg)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

requests_mod = types.ModuleType("requests")
requests_mod.get = lambda *_args, **_kwargs: None
requests_mod.post = lambda *_args, **_kwargs: None
sys.modules.setdefault("requests", requests_mod)

from grok_register import register


def test_registration_persists_exact_session_before_legacy_outputs(monkeypatch):
    writes = []
    monkeypatch.setattr(
        register,
        "_append_registration_line",
        lambda path, line, mode=None, durable=False: writes.append(
            (path, line, mode, durable)
        ),
    )

    register._persist_registration(
        "user@example.test",
        "password",
        "sso-token",
        [{"name": "sso", "value": "opaque", "domain": "accounts.x.ai"}],
    )

    assert [item[0] for item in writes] == [
        "keys/auth-sessions.jsonl",
        "keys/accounts.txt",
        "keys/grok.txt",
    ]
    assert writes[0][2] == 0o600
    assert [item[3] for item in writes] == [True, False, False]


class FakePage:
    def __init__(self, context=None):
        self.context = context
        self.closed = False
        self.url = "about:blank"
        self.goto_calls = []
        self.waits = []
        self.evaluations = []
        self.route_calls = []
        self.mouse = types.SimpleNamespace(
            moves=[],
            clicks=[],
            downs=0,
            ups=0,
            move=self._mouse_move,
            click=self._mouse_click,
            down=self._mouse_down,
            up=self._mouse_up,
        )
        self.turnstile_token = ""
        self.turnstile_box = {"x": 160, "y": 45}
        self.turnstile_page_trace = {
            "created_at": 10.0,
            "script_inserted_at": 11.0,
            "script_loaded_at": 12.0,
            "render_called_at": 13.0,
            "render_returned_at": 14.0,
            "token_written_at": None,
            "token_len": 0,
            "error": None,
        }
        self.turnstile_dom_snapshot = {
            "widget": {"present": True, "x": 10, "y": 10, "w": 300, "h": 70, "visible": True},
            "click_center": {"x": 160, "y": 45},
            "element_at_center": {"tag": "IFRAME", "id": "", "class": "", "is_iframe": True},
            "all_iframe_count": 1,
            "turnstile_iframe_count": 1,
            "iframe_summaries": [
                {"host": "challenges.cloudflare.com", "path": "/turnstile/v0", "x": 10, "y": 10, "w": 300, "h": 70, "visible": True}
            ],
            "turnstile_loaded": True,
            "response_input": {"present": True, "token_len": 0},
        }

    async def set_viewport_size(self, size):
        self.viewport = size
        pass

    async def goto(self, url, timeout=None, wait_until=None):
        self.url = url
        self.goto_calls.append({
            "url": url,
            "timeout": timeout,
            "wait_until": wait_until,
        })
        pass

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)
        pass

    async def evaluate(self, script):
        self.evaluations.append(script)
        if "__csp_solver_snapshot" in script:
            return self.turnstile_dom_snapshot
        if "__cspTurnstileTrace" in script and "return window.__cspTurnstileTrace" in script:
            return self.turnstile_page_trace
        if "cf-turnstile-response" in script:
            return self.turnstile_token
        if "getBoundingClientRect" in script and ".cf-turnstile" in script:
            return self.turnstile_box
        return None

    async def close(self):
        self.closed = True
        pass

    async def route(self, pattern, handler):
        self.route_calls.append({"pattern": pattern, "handler": handler})

    async def _mouse_move(self, x, y, steps=None):
        self.mouse.moves.append({"x": x, "y": y, "steps": steps})

    async def _mouse_click(self, x, y):
        self.mouse.clicks.append({"x": x, "y": y})

    async def _mouse_down(self):
        self.mouse.downs += 1

    async def _mouse_up(self):
        self.mouse.ups += 1


class FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False
        self.clear_cookies_calls = 0
        self.request = types.SimpleNamespace(get=self._request_get)
        self.request_get_calls = []
        self.cookies_value = []
        self.cancel_on_clear = False

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def clear_cookies(self):
        self.clear_cookies_calls += 1
        if self.cancel_on_clear:
            raise asyncio.CancelledError()
        self.cookies_value = []
        pass

    async def cookies(self):
        return list(self.cookies_value)

    async def _request_get(self, url, timeout=None):
        self.request_get_calls.append({"url": url, "timeout": timeout})
        return types.SimpleNamespace(status=403)

    async def close(self):
        self.closed = True
        for page in self.pages:
            page.closed = True


class FakeBrowser:
    def __init__(self):
        self.pages = []
        self.contexts = []
        self.context = types.SimpleNamespace(request=object())

    async def new_page(self):
        page = FakePage(self.context)
        self.pages.append(page)
        return page

    async def new_context(self):
        context = FakeContext()
        self.contexts.append(context)
        return context


class FakePair:
    def __init__(self):
        self.t = types.SimpleNamespace(value="tok")
        self.q = types.SimpleNamespace(
            value={"email": "e@example.test", "password": "pw", "code": "123456"}
        )


class FakeInventory:
    def __init__(self):
        self.active = 0
        self.claims = 0
        self.t_depth = 0
        self.q_depth = 0

    def claim_pair(self):
        inventory = self

        class Claim:
            async def __aenter__(self):
                inventory.active += 1
                inventory.claims += 1
                return FakePair()

            async def __aexit__(self, exc_type, exc, tb):
                inventory.active -= 1
                return False

        return Claim()


class RegisterRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._old_stop = register.STOP
        self._old_timeout = getattr(register, "C_CONSUME_TIMEOUT", None)
        self._old_verify = register.grpc_verify_code
        self._old_register = register.server_action_register
        self._old_create_code = register.grpc_create_code
        self._old_poll_code = register.poll_code
        self._old_poll_code_async = register._poll_code_async
        self._old_log = register.log
        self._old_target = register.TARGET
        self._old_c_hot_page_pool = getattr(register, "C_HOT_PAGE_POOL", None)
        self._old_c_hot_page_pool_size = getattr(register, "C_HOT_PAGE_POOL_SIZE", None)
        self._old_c_set_cookie_via_request = getattr(register, "C_SET_COOKIE_VIA_REQUEST", None)
        self._old_log_mode = getattr(register, "REGISTER_LOG_MODE", None)
        self._old_rate_limit_circuit = register.REGISTRATION_RATE_LIMIT_CIRCUIT
        self._old_protocol_http = getattr(register, "PROTOCOL_HTTP", False)
        # 默认走浏览器契约；HTTP 路径单测再显式打开
        register.PROTOCOL_HTTP = False

    async def asyncTearDown(self):
        if hasattr(register, "_close_c_hot_page_pool"):
            await register._close_c_hot_page_pool()
        if hasattr(register, "_close_http_client"):
            await register._close_http_client()
        register.STOP = self._old_stop
        if self._old_timeout is not None:
            register.C_CONSUME_TIMEOUT = self._old_timeout
        register.grpc_verify_code = self._old_verify
        register.server_action_register = self._old_register
        register.grpc_create_code = self._old_create_code
        register.poll_code = self._old_poll_code
        register._poll_code_async = self._old_poll_code_async
        register.log = self._old_log
        register.TARGET = self._old_target
        register.PROTOCOL_HTTP = self._old_protocol_http
        if self._old_c_hot_page_pool is None and hasattr(register, "C_HOT_PAGE_POOL"):
            delattr(register, "C_HOT_PAGE_POOL")
        elif self._old_c_hot_page_pool is not None:
            register.C_HOT_PAGE_POOL = self._old_c_hot_page_pool
        if self._old_c_hot_page_pool_size is None and hasattr(register, "C_HOT_PAGE_POOL_SIZE"):
            delattr(register, "C_HOT_PAGE_POOL_SIZE")
        elif self._old_c_hot_page_pool_size is not None:
            register.C_HOT_PAGE_POOL_SIZE = self._old_c_hot_page_pool_size
        if self._old_c_set_cookie_via_request is None and hasattr(register, "C_SET_COOKIE_VIA_REQUEST"):
            delattr(register, "C_SET_COOKIE_VIA_REQUEST")
        elif self._old_c_set_cookie_via_request is not None:
            register.C_SET_COOKIE_VIA_REQUEST = self._old_c_set_cookie_via_request
        if self._old_log_mode is None and hasattr(register, "REGISTER_LOG_MODE"):
            delattr(register, "REGISTER_LOG_MODE")
        elif self._old_log_mode is not None:
            register.REGISTER_LOG_MODE = self._old_log_mode
        register.REGISTRATION_RATE_LIMIT_CIRCUIT = self._old_rate_limit_circuit

    async def test_c_worker_timeout_releases_physical_and_pair_and_counts_failure(self):
        async def slow_verify(*_args, **_kwargs):
            await asyncio.sleep(10)
            return True

        register.STOP = asyncio.Event()
        register.C_CONSUME_TIMEOUT = 0.05
        register.grpc_verify_code = slow_verify
        register.server_action_register = lambda *_args, **_kwargs: None
        register.log = lambda _msg: None

        inventory = FakeInventory()
        physical_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register.c_worker(0, FakeBrowser(), inventory, physical_sem, metrics)
        )
        await asyncio.sleep(0.12)
        register.STOP.set()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(inventory.active, 0)
        self.assertEqual(metrics.pair_consumed_fail, 1)

    async def test_c_consume_uses_single_use_page_by_default(self):
        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(*_args, **_kwargs):
            return None

        register.C_HOT_PAGE_POOL = False
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        ok = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertFalse(ok)
        self.assertEqual(len(browser.pages), 1)
        self.assertEqual(len(browser.contexts), 0)
        self.assertTrue(browser.pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_reuses_page_and_clears_cookies_between_consumes(self):
        seen_pages = []

        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(page, *_args, **_kwargs):
            seen_pages.append(page)
            return None

        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        first = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())
        second = await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(len(browser.contexts), 1)
        self.assertEqual(len(browser.contexts[0].pages), 1)
        self.assertEqual(seen_pages, [browser.contexts[0].pages[0], browser.contexts[0].pages[0]])
        self.assertEqual(browser.contexts[0].clear_cookies_calls, 2)
        self.assertFalse(browser.contexts[0].closed)
        self.assertFalse(browser.contexts[0].pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_discards_page_after_exception(self):
        async def failing_verify(*_args, **_kwargs):
            raise RuntimeError("verify failed")

        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2
        register.grpc_verify_code = failing_verify
        register.log = lambda _msg: None

        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)

        with self.assertRaises(RuntimeError):
            await register._consume_pair(browser, physical_sem, FakePair(), Metrics())

        self.assertEqual(len(browser.contexts), 1)
        self.assertTrue(browser.contexts[0].closed)
        self.assertTrue(browser.contexts[0].pages[0].closed)
        self.assertEqual(physical_sem._value, 1)

    async def test_c_hot_page_closes_context_when_cancelled_during_cleanup(self):
        context = FakeContext()
        page = await context.new_page()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        context.cancel_on_clear = True
        register.C_HOT_PAGE_POOL = True

        with self.assertRaises(asyncio.CancelledError):
            await register._release_c_page(context, page, healthy=True)

        self.assertTrue(context.closed)
        self.assertTrue(page.closed)

    async def test_c_hot_page_discards_page_after_fallback_navigation(self):
        context = FakeContext()
        page = await context.new_page()
        page.url = "https://example.test/set-cookie?q=abc"
        register.C_HOT_PAGE_POOL = True
        register.C_HOT_PAGE_POOL_SIZE = 2

        await register._release_c_page(context, page, healthy=True)

        self.assertTrue(context.closed)
        self.assertTrue(page.closed)
        self.assertEqual(register._c_hot_page_pool, [])

    async def test_server_action_can_set_cookie_via_request_without_navigating_page(self):
        page = FakePage()
        context = FakeContext()
        context.cookies_value = [{"name": "sso", "value": "x" * 152}]
        page.context = context
        page.goto_calls = []

        async def fake_evaluate(_script):
            return '0:"https:\\/\\/auth.grokipedia.com\\/set-cookie?q=abc"1:'

        page.evaluate = fake_evaluate
        register.STATE_TREE = "state"
        register.ACTION_ID = "action"
        register.C_SET_COOKIE_VIA_REQUEST = True

        sso = await register.server_action_register(
            page, "e@example.test", "pw", "123456", "token"
        )

        self.assertEqual(sso, "x" * 152)
        self.assertEqual(page.goto_calls, [])
        self.assertEqual(
            context.request_get_calls,
            [{"url": "https://auth.grokipedia.com/set-cookie?q=abc", "timeout": 15000}],
        )

    async def test_server_action_raises_a_distinct_error_for_a_rate_limited_signup_page(self):
        page = FakePage()

        async def rate_limited_response(_script):
            return "Too many requests. Please try again later."

        page.evaluate = rate_limited_response
        register.STATE_TREE = "state"
        register.ACTION_ID = "action"

        with self.assertRaises(register.RegistrationRateLimited):
            await register.server_action_register(
                page, "e@example.test", "pw", "123456", "token"
            )

    async def test_rate_limit_circuit_opens_for_the_configured_cooldown(self):
        now = [100.0]
        circuit = register.RegistrationRateLimitCircuit(
            cooldown_seconds=60,
            clock=lambda: now[0],
        )

        circuit.trip()

        self.assertTrue(circuit.is_open())
        self.assertEqual(circuit.remaining_seconds(), 60)
        now[0] = 160.0
        self.assertFalse(circuit.is_open())

    async def test_monitor_uses_metrics_snapshot(self):
        register.STOP = asyncio.Event()
        register.TARGET = 1
        register.REGISTER_LOG_MODE = "debug"
        messages = []
        register.log = messages.append

        metrics = Metrics()
        metrics.success_count = 1
        metrics.pair_claimed = 2
        metrics.pair_consumed_ok = 1
        metrics.pair_consumed_fail = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        await register.monitor(FakeInventory(), sems, metrics, interval=0)

        self.assertTrue(
            any("pair:2 ok:1 fail:1" in message for message in messages),
            messages,
        )

    async def test_monitor_hides_internal_snapshot_in_user_mode(self):
        register.STOP = asyncio.Event()
        register.TARGET = 1
        register.REGISTER_LOG_MODE = "user"
        messages = []
        register.log = messages.append

        metrics = Metrics()
        metrics.success_count = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        await register.monitor(FakeInventory(), sems, metrics, interval=0)

        self.assertFalse(any(message.startswith("[*] T:") for message in messages), messages)

    async def test_user_event_format_reports_only_registration_outcomes(self):
        self.assertEqual(
            register.format_user_registration_event("started", task_id=7),
            "[→] 开始注册 #7",
        )
        self.assertEqual(
            register.format_user_registration_event(
                "success", task_id=7, count=5, rate_per_minute=12.34
            ),
            "[✓] 注册成功 #7 | 运行平均 12.3/分 | 累计 5",
        )
        self.assertEqual(
            register.format_user_registration_event("failed", task_id=7),
            "[✗] 注册失败 #7 | 已跳过，继续下一任务",
        )
        self.assertEqual(
            register.format_user_registration_event("rate_limited", wait_seconds=60),
            "[⏸] 触发限流 | 60秒后恢复探测",
        )
        self.assertEqual(
            register.format_user_registration_event("recovered", wait_seconds=61),
            "[▶] 限流解除 | 实际等待 61秒",
        )

    async def test_debug_flag_overrides_environment_and_invalid_mode_is_rejected(self):
        self.assertEqual(
            register.resolve_register_log_mode(["--debug"], {"REGISTER_LOG_MODE": "user"}),
            "debug",
        )
        self.assertEqual(
            register.resolve_register_log_mode([], {"REGISTER_LOG_MODE": "debug"}),
            "debug",
        )
        with self.assertRaises(ValueError):
            register.resolve_register_log_mode([], {"REGISTER_LOG_MODE": "verbose"})

    async def test_registration_task_numbers_are_separate_from_pair_claims(self):
        metrics = Metrics()
        metrics.pair_claimed = 12

        self.assertEqual(metrics.next_registration_task(), 1)
        self.assertEqual(metrics.next_registration_task(), 2)

    async def test_five_minute_rate_uses_process_uptime_then_sliding_window(self):
        now = [0.0]
        metrics = Metrics(clock=lambda: now[0])
        self.assertIsNone(metrics.five_minute_success_rate())

        now[0] = 10.0
        metrics.record_success()
        now[0] = 20.0
        metrics.record_success()
        self.assertEqual(metrics.five_minute_success_rate(), 6.0)

        now[0] = 311.0
        self.assertAlmostEqual(metrics.five_minute_success_rate(), 0.2)
        now[0] = 321.0
        self.assertEqual(metrics.five_minute_success_rate(), 0.0)

    async def test_runtime_average_rate_includes_the_entire_cooldown_period(self):
        now = [0.0]
        metrics = Metrics(clock=lambda: now[0])
        self.assertIsNone(metrics.runtime_average_success_rate())

        now[0] = 10.0
        metrics.record_success()
        now[0] = 20.0
        metrics.record_success()
        self.assertEqual(metrics.runtime_average_success_rate(), 6.0)

        now[0] = 320.0
        self.assertEqual(metrics.runtime_average_success_rate(), 0.375)

    async def test_terminal_output_failure_does_not_escape_log(self):
        old_output = register._terminal_output
        try:
            register._terminal_output = lambda _message: (_ for _ in ()).throw(
                OSError("closed pipe")
            )
            register.log("safe")
        finally:
            register._terminal_output = old_output

    async def test_rate_limit_circuit_measures_one_recovery_window(self):
        now = [100.0]
        circuit = register.RegistrationRateLimitCircuit(
            cooldown_seconds=60,
            clock=lambda: now[0],
        )
        circuit.trip()
        now[0] = 161.5

        self.assertEqual(circuit.consume_recovery_seconds(), 61.5)
        self.assertIsNone(circuit.consume_recovery_seconds())

    async def test_rate_limit_probe_is_released_when_consume_times_out(self):
        async def slow_verify(*_args, **_kwargs):
            await asyncio.Event().wait()

        register.REGISTRATION_RATE_LIMIT_CIRCUIT = register.RegistrationRateLimitCircuit(0)
        register.REGISTRATION_RATE_LIMIT_CIRCUIT.trip()
        register.grpc_verify_code = slow_verify

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                register._consume_pair(
                    FakeBrowser(), asyncio.Semaphore(1), FakePair(), Metrics(), task_id=1
                ),
                timeout=0.01,
            )

        self.assertFalse(register.REGISTRATION_RATE_LIMIT_CIRCUIT._probe_active)

    async def test_auto_capacity_is_bounded_by_cpu_and_memory(self):
        roomy = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )
        tight = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=0,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )

        self.assertEqual(roomy[0], 8)
        self.assertEqual(roomy[1], 10)
        self.assertEqual(roomy[2], register.Q_PENDING_CAP + 2)
        self.assertEqual(roomy[3], 10)
        self.assertEqual(tight[0], 1)

    async def test_default_auto_capacity_is_conservative(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
        )

        self.assertEqual(physical, 4)
        self.assertEqual(s_workers, 6)
        self.assertEqual(c_workers, 6)

    async def test_explicit_physical_cap_overrides_auto_capacity(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=3,
            physical_per_cpu=4,
            physical_mem_mb=512,
            min_free_mem_mb=500,
        )

        self.assertEqual(physical, 3)
        self.assertEqual(s_workers, 5)
        self.assertEqual(c_workers, 5)

    async def test_capacity_profile_supplies_physical_cap_when_not_explicit(self):
        physical, s_workers, _p_workers, c_workers = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=0,
            profile_physical_cap=7,
        )
        tight, *_ = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=1100,
            physical_cap=0,
            profile_physical_cap=7,
        )
        explicit, *_ = register.derive_capacity(
            cpu_count=2,
            max_mem_mb=5600,
            physical_cap=5,
            profile_physical_cap=7,
        )

        self.assertEqual(physical, 7)
        self.assertEqual(s_workers, 9)
        self.assertEqual(c_workers, 9)
        self.assertEqual(tight, 1)
        self.assertEqual(explicit, 5)

    async def test_load_capacity_profile_reads_valid_physical_cap(self):
        with tempfile.NamedTemporaryFile("w+", delete=True) as f:
            json.dump({"physical_cap": 7}, f)
            f.flush()

            profile = register.load_capacity_profile(f.name)

        self.assertEqual(profile["physical_cap"], 7)
        self.assertEqual(register.load_capacity_profile("/does/not/exist"), {})
        self.assertEqual(register.load_capacity_profile(""), {})

    async def test_admission_t_high_defaults_to_physical_cap_bounded_by_slot(self):
        watermarks = register.derive_admission_watermarks(
            physical_cap=6,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
        )
        bounded = register.derive_admission_watermarks(
            physical_cap=10,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
        )

        self.assertEqual(watermarks["t_high"], 6)
        self.assertEqual(watermarks["t_low"], 3)
        self.assertEqual(bounded["t_high"], 8)

    async def test_admission_t_high_override_remains_explicit(self):
        watermarks = register.derive_admission_watermarks(
            physical_cap=6,
            t_slot_cap=8,
            q_pending_cap=12,
            t_target=4,
            q_target=4,
            t_high_override=4,
            t_low_override=2,
        )

        self.assertEqual(watermarks["t_high"], 4)
        self.assertEqual(watermarks["t_low"], 2)

    async def test_c_hot_page_pool_size_is_derived_at_startup(self):
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=6, c_workers=8, configured_size=0
            ),
            6,
        )
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=8, c_workers=3, configured_size=0
            ),
            3,
        )
        self.assertEqual(
            register.derive_c_hot_page_pool_size(
                physical_cap=8, c_workers=10, configured_size=4
            ),
            4,
        )

    async def test_send_q_request_batch_reuses_one_page_for_multiple_emails(self):
        emails = []

        async def fake_create_code(_page, email):
            emails.append(email)
            return True

        register.grpc_create_code = fake_create_code
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        requests = [
            {"handle": "h1", "email": "a@example.test", "password": "pw1"},
            {"handle": "h2", "email": "b@example.test", "password": "pw2"},
            {"handle": "h3", "email": "c@example.test", "password": "pw3"},
        ]

        results = await register._send_q_request_batch(
            browser, physical_sem, p_send_sem, requests
        )

        self.assertEqual(emails, [item["email"] for item in requests])
        self.assertEqual([item["sent"] for item in results], [True, True, True])
        self.assertEqual(len(browser.pages), 1)
        self.assertTrue(browser.pages[0].closed)
        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(p_send_sem._value, 1)

    async def test_send_q_request_batch_records_physical_and_stage_metrics(self):
        async def fake_create_code(_page, _email):
            return True

        register.grpc_create_code = fake_create_code
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        await register._send_q_request_batch(
            browser,
            physical_sem,
            p_send_sem,
            [{"handle": "h1", "email": "a@example.test", "password": "pw"}],
            metrics,
        )

        self.assertEqual(metrics.p_physical_count, 1)
        self.assertEqual(metrics.p_page_prepare_count, 1)
        self.assertEqual(metrics.p_send_count, 1)
        self.assertGreaterEqual(metrics.p_physical_wait_seconds, 0)
        self.assertGreaterEqual(metrics.p_physical_hold_seconds, 0)

    async def test_consume_pair_records_physical_and_stage_metrics(self):
        async def ok_verify(*_args, **_kwargs):
            return True

        async def no_sso_register(*_args, **_kwargs):
            return None

        register.C_HOT_PAGE_POOL = False
        register.grpc_verify_code = ok_verify
        register.server_action_register = no_sso_register
        register.log = lambda _msg: None
        metrics = Metrics()

        await register._consume_pair(
            FakeBrowser(), asyncio.Semaphore(1), FakePair(), metrics
        )

        self.assertEqual(metrics.c_physical_count, 1)
        self.assertEqual(metrics.c_page_acquire_count, 1)
        self.assertEqual(metrics.c_verify_count, 1)
        self.assertEqual(metrics.c_register_count, 1)
        self.assertEqual(metrics.c_hot_page_hits, 0)
        self.assertEqual(metrics.c_hot_page_misses, 0)

    async def test_poll_and_admit_q_releases_one_pending_per_terminal_request(self):
        register.P_REQUEST_TIMEOUT = 1
        register.poll_code = lambda _handle: None
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        await register._poll_and_admit_q(
            {"handle": "h1", "email": "a@example.test", "password": "pw"},
            FakeInventory(),
            q_pending_sem,
            q_slot_sem,
            metrics,
        )

        self.assertEqual(q_pending_sem._value, 1)
        self.assertEqual(q_slot_sem._value, 1)
        self.assertEqual(metrics.q_discarded, 1)

    async def test_poll_cancel_before_terminal_does_not_release_pending(self):
        async def blocked_poll(_loop, _handle):
            await asyncio.sleep(10)
            return "123456"

        register._poll_code_async = blocked_poll
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register._poll_and_admit_q(
                {"handle": "h1", "email": "a@example.test", "password": "pw"},
                FakeInventory(),
                q_pending_sem,
                q_slot_sem,
                metrics,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(q_pending_sem._value, 0)
        self.assertEqual(q_slot_sem._value, 1)

    async def test_poll_cancel_after_q_return_releases_pending(self):
        async def returned_poll(_loop, _handle):
            return "123456"

        class BlockingInventory(FakeInventory):
            async def put_q(self, _env):
                await asyncio.sleep(10)

        register._poll_code_async = returned_poll
        q_pending_sem = asyncio.Semaphore(0)
        q_slot_sem = asyncio.Semaphore(1)
        metrics = Metrics()

        task = asyncio.create_task(
            register._poll_and_admit_q(
                {"handle": "h1", "email": "a@example.test", "password": "pw"},
                BlockingInventory(),
                q_pending_sem,
                q_slot_sem,
                metrics,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(q_pending_sem._value, 1)
        self.assertEqual(q_slot_sem._value, 1)
        self.assertEqual(metrics.q_returned, 1)

    async def test_metrics_snapshot_includes_solver_timing(self):
        metrics = Metrics()
        metrics.t_solve_count = 2
        metrics.t_solve_seconds = 5.0
        metrics.t_solve_failed = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        row = metrics.snapshot(FakeInventory(), sems)

        self.assertIn("t_solve_avg:2.5", row)
        self.assertIn("t_solve_fail:1", row)

    async def test_metrics_snapshot_includes_role_physical_and_stage_timing(self):
        metrics = Metrics()
        metrics.s_physical_count = 2
        metrics.s_physical_wait_seconds = 1.0
        metrics.s_physical_hold_seconds = 6.0
        metrics.p_physical_count = 1
        metrics.p_physical_wait_seconds = 0.2
        metrics.p_physical_hold_seconds = 1.4
        metrics.c_physical_count = 3
        metrics.c_physical_wait_seconds = 0.9
        metrics.c_physical_hold_seconds = 7.5
        metrics.p_email_create_count = 2
        metrics.p_email_create_seconds = 1.0
        metrics.p_page_prepare_count = 1
        metrics.p_page_prepare_seconds = 0.8
        metrics.p_send_count = 1
        metrics.p_send_seconds = 0.4
        metrics.c_page_acquire_count = 2
        metrics.c_page_acquire_seconds = 0.6
        metrics.c_verify_count = 2
        metrics.c_verify_seconds = 0.8
        metrics.c_register_count = 2
        metrics.c_register_seconds = 3.0
        metrics.c_hot_page_hits = 4
        metrics.c_hot_page_misses = 1
        sems = {
            "physical": asyncio.Semaphore(1),
            "t_slot": asyncio.Semaphore(1),
            "q_slot": asyncio.Semaphore(1),
            "q_pending": asyncio.Semaphore(1),
        }

        row = metrics.snapshot(FakeInventory(), sems)

        self.assertIn("s_phys:0.50/3.00", row)
        self.assertIn("p_phys:0.20/1.40", row)
        self.assertIn("c_phys:0.30/2.50", row)
        self.assertIn("p_stage:0.50/0.80/0.40", row)
        self.assertIn("c_stage:0.30/0.40/1.50", row)
        self.assertIn("c_hot:4/1", row)

    async def test_record_solver_trace_accumulates_stage_metrics(self):
        metrics = Metrics()

        register._record_solver_trace(
            metrics,
            {
                "goto_s": 1.0,
                "inject_s": 0.2,
                "initial_s": 0.5,
                "click_s": 0.1,
                "wait_s": 20.0,
                "reused": True,
                "visible_frame": False,
            },
            21.8,
            "token",
        )
        register._record_solver_trace(metrics, {}, 10.0, None)

        self.assertEqual(metrics.t_solve_count, 2)
        self.assertEqual(metrics.t_solve_failed, 1)
        self.assertAlmostEqual(metrics.solver_goto_seconds, 1.0)
        self.assertAlmostEqual(metrics.solver_wait_seconds, 20.0)
        self.assertEqual(metrics.solver_reused_count, 1)
        self.assertEqual(metrics.solver_visible_frame_count, 0)

    async def test_solver_timeline_records_click_and_token_events_when_enabled(self):
        page = FakePage()
        original_click = register._mouse_click_turnstile_center_trace
        original_read = register._read_turnstile_token
        clicked_once = False
        timeline = register._new_solver_timeline(enabled=True)

        async def fake_click(_page, **_kwargs):
            nonlocal clicked_once
            clicked_once = True
            return True, {"box_eval_ms": 0.0}

        async def fake_read(_page):
            return "token-value-long" if clicked_once else ""

        try:
            register._mouse_click_turnstile_center_trace = fake_click
            register._read_turnstile_token = fake_read

            clicked = await register._repeat_mouse_click_turnstile(page, timeline=timeline)
        finally:
            register._mouse_click_turnstile_center_trace = original_click
            register._read_turnstile_token = original_read

        events = [event["event"] for event in timeline["events"]]
        self.assertTrue(clicked)
        self.assertIn("click_before", events)
        self.assertIn("click_after", events)
        self.assertTrue(any(event.get("dom", {}).get("widget", {}).get("present") for event in timeline["events"]))
        self.assertTrue(any(event.get("click_call_ms", 0) >= 0 for event in timeline["events"]))
        self.assertTrue(any(event.get("token_len", 0) > 10 for event in timeline["events"]))

    async def test_turnstile_dom_snapshot_reports_click_target_without_text_or_full_urls(self):
        page = FakePage()

        snapshot = await register._turnstile_dom_snapshot(page)

        self.assertEqual(snapshot["all_iframe_count"], 1)
        self.assertEqual(snapshot["turnstile_iframe_count"], 1)
        self.assertEqual(snapshot["widget"]["w"], 300)
        self.assertEqual(snapshot["element_at_center"]["tag"], "IFRAME")
        self.assertEqual(snapshot["iframe_summaries"][0]["host"], "challenges.cloudflare.com")
        self.assertNotIn("src", snapshot["iframe_summaries"][0])
        self.assertNotIn("text", snapshot["element_at_center"])

    async def test_mouse_click_turnstile_center_can_return_timing_trace(self):
        page = FakePage()

        clicked, trace = await register._mouse_click_turnstile_center_trace(page)

        self.assertTrue(clicked)
        self.assertEqual(trace["click_x"], 160.0)
        self.assertEqual(trace["click_y"], 45.0)
        for key in ("box_eval_ms", "mouse_move1_ms", "mouse_move2_ms", "mouse_down_ms", "mouse_up_ms"):
            self.assertIn(key, trace)
            self.assertGreaterEqual(trace[key], 0)

    async def test_inject_turnstile_widget_leaves_default_script_uninstrumented(self):
        page = FakePage()

        await register._inject_turnstile_widget(page)

        script = page.evaluations[-1]
        self.assertNotIn("__cspTurnstileTrace", script)

    async def test_inject_turnstile_widget_records_page_timeline_when_enabled(self):
        page = FakePage()

        await register._inject_turnstile_widget(page, timeline=True)

        script = page.evaluations[-1]
        self.assertIn("__cspTurnstileTrace", script)
        self.assertIn("script_inserted_at", script)
        self.assertIn("render_called_at", script)
        self.assertIn("token_written_at", script)

    async def test_start_turnstile_challenge_records_page_trace_when_timeline_enabled(self):
        browser = FakeBrowser()
        messages = []
        old_trace = register.SOLVER_TIMELINE_TRACE
        old_sample = register.SOLVER_TIMELINE_SAMPLE
        old_emitted = register._solver_timeline_emitted
        old_log = register.log
        try:
            register.SOLVER_TIMELINE_TRACE = True
            register.SOLVER_TIMELINE_SAMPLE = 1
            register._solver_timeline_emitted = 0
            register.log = messages.append

            item = await register._start_turnstile_challenge(browser, fast_click=True)
            await register._put_solver_page(item, False)
        finally:
            register.SOLVER_TIMELINE_TRACE = old_trace
            register.SOLVER_TIMELINE_SAMPLE = old_sample
            register._solver_timeline_emitted = old_emitted
            register.log = old_log

        events = item["timeline"]["events"]
        self.assertTrue(any(event["event"] == "page_trace_after_inject" for event in events))
        self.assertTrue(any(event["event"] == "page_trace_after_click" for event in events))

    async def test_solve_one_turnstile_uses_fast_click_by_default(self):
        calls = []

        async def fake_start(_browser, *, fast_click=False):
            calls.append(fast_click)
            return {"page": object()}

        async def fake_wait(_item):
            return "token-value"

        old_start = register._start_turnstile_challenge
        old_wait = register._wait_turnstile_challenge
        try:
            register._start_turnstile_challenge = fake_start
            register._wait_turnstile_challenge = fake_wait

            token = await register.solve_one_turnstile(object())
        finally:
            register._start_turnstile_challenge = old_start
            register._wait_turnstile_challenge = old_wait

        self.assertEqual(token, "token-value")
        self.assertEqual(calls, [True])

    async def test_wait_turnstile_logs_timeline_when_present(self):
        messages = []
        register.REGISTER_LOG_MODE = "debug"

        async def fake_poll(_page, **_kwargs):
            return "token-value-long"

        async def fake_put(_item, _ok):
            return None

        old_poll = register._poll_turnstile_token
        old_put = register._put_solver_page
        old_log = register.log
        try:
            register._poll_turnstile_token = fake_poll
            register._put_solver_page = fake_put
            register.log = messages.append
            item = {
                "page": object(),
                "trace": {},
                "timeline": {
                    "start": register.time.time(),
                    "events": [{"t": 0.1, "event": "x"}],
                },
            }

            token = await register._wait_turnstile_challenge(item)
        finally:
            register._poll_turnstile_token = old_poll
            register._put_solver_page = old_put
            register.log = old_log

        self.assertEqual(token, "token-value-long")
        self.assertTrue(any(message.startswith("[solver_timeline] ") for message in messages))

    async def test_wait_turnstile_timeline_logs_solve_id_and_poll_summary(self):
        messages = []
        register.REGISTER_LOG_MODE = "debug"
        page = FakePage()
        page.turnstile_token = "token-value-long"
        timeline = register._new_solver_timeline(enabled=True)

        async def fake_put(_item, _ok):
            return None

        old_attempts = register.SOLVER_POLL_ATTEMPTS
        old_interval = register.SOLVER_POLL_INTERVAL_MS
        old_put = register._put_solver_page
        old_sleep = register.asyncio.sleep
        old_log = register.log
        try:
            register.SOLVER_POLL_ATTEMPTS = 1
            register.SOLVER_POLL_INTERVAL_MS = 50

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep
            register._put_solver_page = fake_put
            register.log = messages.append

            token = await register._wait_turnstile_challenge({
                "page": page,
                "trace": {},
                "timeline": timeline,
            })
        finally:
            register.SOLVER_POLL_ATTEMPTS = old_attempts
            register.SOLVER_POLL_INTERVAL_MS = old_interval
            register.asyncio.sleep = old_sleep
            register._put_solver_page = old_put
            register.log = old_log

        payload = next(message.removeprefix("[solver_timeline] ") for message in messages)
        events = register.json.loads(payload)
        poll_done = next(event for event in events if event["event"] == "poll_done")

        self.assertEqual(token, "token-value-long")
        self.assertIn("solve_id", poll_done)
        self.assertEqual(poll_done["poll_attempts"], 1)
        self.assertEqual(poll_done["first_token_attempt"], 1)
        self.assertGreaterEqual(poll_done["poll_read_ms_max"], 0)

    async def test_mouse_click_turnstile_retries_uses_center_clicks(self):
        page = FakePage()

        old_retries = getattr(register, "SOLVER_MOUSE_CLICK_RETRIES", None)
        old_interval = getattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS", None)
        old_sleep = register.asyncio.sleep
        try:
            register.SOLVER_MOUSE_CLICK_RETRIES = 3
            register.SOLVER_MOUSE_CLICK_INTERVAL_MS = 600

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep

            clicked = await register._repeat_mouse_click_turnstile(page)
        finally:
            register.asyncio.sleep = old_sleep
            if old_retries is None:
                delattr(register, "SOLVER_MOUSE_CLICK_RETRIES")
            else:
                register.SOLVER_MOUSE_CLICK_RETRIES = old_retries
            if old_interval is None:
                delattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS")
            else:
                register.SOLVER_MOUSE_CLICK_INTERVAL_MS = old_interval

        self.assertTrue(clicked)
        self.assertEqual(page.mouse.downs, 3)
        self.assertEqual(page.mouse.ups, 3)
        self.assertEqual(page.mouse.moves[-1], {"x": 160, "y": 45, "steps": 8})

    async def test_mouse_click_turnstile_stops_when_token_appears(self):
        page = FakePage()
        evaluate_count = 0
        original_evaluate = page.evaluate

        async def evaluate(script):
            nonlocal evaluate_count
            if "cf-turnstile-response" in script:
                evaluate_count += 1
                return "token-value-long" if evaluate_count > 1 else ""
            return await original_evaluate(script)

        page.evaluate = evaluate

        old_retries = getattr(register, "SOLVER_MOUSE_CLICK_RETRIES", None)
        old_interval = getattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS", None)
        old_sleep = register.asyncio.sleep
        try:
            register.SOLVER_MOUSE_CLICK_RETRIES = 3
            register.SOLVER_MOUSE_CLICK_INTERVAL_MS = 600

            async def no_sleep(_seconds):
                return None

            register.asyncio.sleep = no_sleep

            clicked = await register._repeat_mouse_click_turnstile(page)
        finally:
            register.asyncio.sleep = old_sleep
            if old_retries is None:
                delattr(register, "SOLVER_MOUSE_CLICK_RETRIES")
            else:
                register.SOLVER_MOUSE_CLICK_RETRIES = old_retries
            if old_interval is None:
                delattr(register, "SOLVER_MOUSE_CLICK_INTERVAL_MS")
            else:
                register.SOLVER_MOUSE_CLICK_INTERVAL_MS = old_interval

        self.assertTrue(clicked)
        self.assertEqual(page.mouse.downs, 1)
        self.assertEqual(evaluate_count, 2)

    async def test_prepare_signup_page_uses_configured_navigation_profile(self):
        page = FakePage()

        old_wait_until = getattr(register, "PAGE_GOTO_WAIT_UNTIL", None)
        old_post_wait = getattr(register, "PAGE_POST_WAIT_MS", None)
        try:
            register.PAGE_GOTO_WAIT_UNTIL = "domcontentloaded"
            register.PAGE_POST_WAIT_MS = 500

            await register._prepare_signup_page(page, redirect=True)
        finally:
            if old_wait_until is None:
                delattr(register, "PAGE_GOTO_WAIT_UNTIL")
            else:
                register.PAGE_GOTO_WAIT_UNTIL = old_wait_until
            if old_post_wait is None:
                delattr(register, "PAGE_POST_WAIT_MS")
            else:
                register.PAGE_POST_WAIT_MS = old_post_wait

        self.assertEqual(page.goto_calls[-1]["wait_until"], "domcontentloaded")
        self.assertEqual(page.waits[-1], 500)

    async def test_prepare_signup_page_can_block_static_assets(self):
        page = FakePage()

        old_block_assets = getattr(register, "PAGE_BLOCK_STATIC_ASSETS", None)
        try:
            register.PAGE_BLOCK_STATIC_ASSETS = True

            await register._prepare_signup_page(page, redirect=True)
        finally:
            if old_block_assets is None:
                delattr(register, "PAGE_BLOCK_STATIC_ASSETS")
            else:
                register.PAGE_BLOCK_STATIC_ASSETS = old_block_assets

        self.assertEqual(len(page.route_calls), 1)
        self.assertEqual(page.route_calls[0]["pattern"], "**/*")

    async def test_default_solver_and_page_latency_profile_matches_accepted_optimization(self):
        self.assertEqual(register.SOLVER_INITIAL_WAIT_MS, 500)
        self.assertTrue(register.SOLVER_FAST_CLICK)
        self.assertEqual(register.SOLVER_MOUSE_CLICK_RETRIES, 3)
        self.assertEqual(register.SOLVER_MOUSE_CLICK_INTERVAL_MS, 600)
        self.assertEqual(register.PAGE_GOTO_WAIT_UNTIL, "domcontentloaded")
        self.assertEqual(register.PAGE_POST_WAIT_MS, 500)

    async def test_send_q_request_batch_http_skips_browser_and_physical(self):
        """PROTOCOL_HTTP: Create 不占 page / Physical_Sem。"""
        emails = []

        async def fake_create(page, email):
            self.assertIsNone(page)
            emails.append(email)
            return True

        async def fake_get_client():
            return object()

        register.PROTOCOL_HTTP = True
        register.grpc_create_code = fake_create
        register._get_http_client = fake_get_client
        browser = FakeBrowser()
        physical_sem = asyncio.Semaphore(1)
        p_send_sem = asyncio.Semaphore(1)
        requests = [
            {"handle": "h1", "email": "a@example.test", "password": "pw1"},
            {"handle": "h2", "email": "b@example.test", "password": "pw2"},
        ]
        results = await register._send_q_request_batch(
            browser, physical_sem, p_send_sem, requests
        )
        self.assertEqual(emails, ["a@example.test", "b@example.test"])
        self.assertEqual([r["sent"] for r in results], [True, True])
        self.assertEqual(len(browser.pages), 0)
        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(p_send_sem._value, 1)

    async def test_consume_pair_http_skips_physical_and_page(self):
        """PROTOCOL_HTTP: Verify/ServerAction 不占 Physical_Sem / hot page。"""

        async def ok_verify(page, email, code):
            self.assertIsNone(page)
            return True

        async def ok_register(page, email, password, code, token, include_session=False):
            self.assertIsNone(page)
            if include_session:
                return "sso-http-token", [{"name": "sso", "value": "sso-http-token"}]
            return "sso-http-token"

        register.PROTOCOL_HTTP = True
        register.grpc_verify_code = ok_verify
        register.server_action_register = ok_register
        register.log = lambda _msg: None
        register._persist_registration = lambda *a, **k: None
        metrics = Metrics()
        physical_sem = asyncio.Semaphore(1)
        browser = FakeBrowser()

        ok = await register._consume_pair(browser, physical_sem, FakePair(), metrics)
        self.assertTrue(ok)
        self.assertEqual(physical_sem._value, 1)
        self.assertEqual(metrics.c_physical_count, 0)
        self.assertEqual(metrics.c_verify_count, 1)
        self.assertEqual(metrics.c_register_count, 1)
        self.assertEqual(len(browser.pages), 0)


if __name__ == "__main__":
    unittest.main()
