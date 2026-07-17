"""Unit tests for external Turnstile providers + early-poll path."""
import unittest
from unittest import mock

from grok_register import turnstile_provider as tp


class TurnstileProviderUnit(unittest.TestCase):
    def test_resolve_provider_defaults_browser(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            # clear only the key if present
            env = {"TURNSTILE_PROVIDER": ""}
            with mock.patch.dict("os.environ", env, clear=False):
                self.assertEqual(tp.resolve_provider_name(), "browser")
                self.assertTrue(tp.uses_browser())

    def test_resolve_capsolver_aliases(self):
        with mock.patch.dict("os.environ", {"TURNSTILE_PROVIDER": "cap"}, clear=False):
            self.assertEqual(tp.resolve_provider_name(), "capsolver")
            self.assertFalse(tp.uses_browser("capsolver"))

    def test_resolve_lite_aliases(self):
        for alias in ("lite", "farm", "turnstile-solver", "local-solver"):
            with mock.patch.dict("os.environ", {"TURNSTILE_PROVIDER": alias}, clear=False):
                self.assertEqual(tp.resolve_provider_name(), "lite")
                self.assertFalse(tp.uses_browser("lite"))

    def _post_payload(self, call):
        payload = call.kwargs.get("json")
        if payload is None and len(call.args) > 1 and isinstance(call.args[1], dict):
            payload = call.args[1]
        if payload is None and len(call) > 1 and isinstance(call[1], dict):
            payload = call[1].get("json")
        if payload is None:
            payload = call.kwargs["json"]
        return payload

    def test_mint_capsolver_happy_path(self):
        create = mock.Mock()
        create.raise_for_status = mock.Mock()
        create.json.return_value = {"errorId": 0, "taskId": "tid-1"}
        result = mock.Mock()
        result.raise_for_status = mock.Mock()
        result.json.return_value = {
            "errorId": 0,
            "status": "ready",
            "solution": {"token": "x" * 40},
        }

        with mock.patch.dict(
            "os.environ",
            {
                "TURNSTILE_PROVIDER": "capsolver",
                "CAPSOLVER_API_KEY": "k",
                "CAPSOLVER_POLL_INTERVAL_SEC": "0",
            },
            clear=False,
        ):
            with mock.patch.object(tp.req, "post", side_effect=[create, result]) as post:
                with mock.patch.object(tp.time, "sleep", return_value=None):
                    token, trace = tp.mint_turnstile_token("0x4AAAAAAAtestkey")
        self.assertEqual(token, "x" * 40)
        self.assertEqual(trace["provider"], "capsolver")
        self.assertEqual(post.call_count, 2)
        payload = self._post_payload(post.call_args_list[0])
        self.assertEqual(payload["task"]["websiteKey"], "0x4AAAAAAAtestkey")
        self.assertEqual(payload["task"]["type"], "AntiTurnstileTaskProxyLess")
        self.assertEqual(payload["clientKey"], "k")

    def test_mint_lite_no_client_key(self):
        """Local lite farm: no commercial key; createTask + poll processing→ready."""
        create = mock.Mock()
        create.raise_for_status = mock.Mock()
        create.json.return_value = {"errorId": 0, "taskId": "lite-tid"}
        processing = mock.Mock()
        processing.raise_for_status = mock.Mock()
        processing.json.return_value = {
            "errorId": 0,
            "status": "processing",
            "taskId": "lite-tid",
        }
        ready = mock.Mock()
        ready.raise_for_status = mock.Mock()
        ready.json.return_value = {
            "errorId": 0,
            "status": "ready",
            "taskId": "lite-tid",
            "solution": {"token": "L" * 48},
        }

        with mock.patch.dict(
            "os.environ",
            {
                "TURNSTILE_PROVIDER": "lite",
                "LITE_SOLVER_URL": "http://127.0.0.1:5072",
                "LITE_SOLVER_POLL_INTERVAL_SEC": "0",
                "LITE_SOLVER_CLIENT_KEY": "",
            },
            clear=False,
        ):
            with mock.patch.object(
                tp.req, "post", side_effect=[create, processing, ready]
            ) as post:
                with mock.patch.object(tp.time, "sleep", return_value=None):
                    token, trace = tp.mint_turnstile_token("0x4AAAAAAAhr9JGVDZbrZOo0")
        self.assertEqual(token, "L" * 48)
        self.assertEqual(trace["provider"], "lite")
        self.assertIsNone(trace.get("error"))
        self.assertEqual(post.call_count, 3)
        create_url = post.call_args_list[0].args[0]
        self.assertTrue(create_url.endswith("/createTask"))
        self.assertIn("127.0.0.1:5072", create_url)
        payload = self._post_payload(post.call_args_list[0])
        self.assertNotIn("clientKey", payload)  # no commercial key
        self.assertEqual(payload["task"]["type"], "TurnstileTaskProxyless")
        self.assertEqual(payload["task"]["websiteKey"], "0x4AAAAAAAhr9JGVDZbrZOo0")
        poll_payload = self._post_payload(post.call_args_list[2])
        self.assertEqual(poll_payload["taskId"], "lite-tid")
        self.assertNotIn("clientKey", poll_payload)

    def test_mint_http_extracts_token(self):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        resp.json.return_value = {"data": {"token": "y" * 32}}
        with mock.patch.dict(
            "os.environ",
            {
                "TURNSTILE_PROVIDER": "http",
                "TURNSTILE_HTTP_URL": "http://127.0.0.1:9/solve",
            },
            clear=False,
        ):
            with mock.patch.object(tp.req, "post", return_value=resp):
                token, trace = tp.mint_turnstile_token("0x4AAAAAAAtestkey")
        self.assertEqual(token, "y" * 32)
        self.assertEqual(trace["provider"], "http")

    def test_mint_missing_key_returns_none(self):
        with mock.patch.dict(
            "os.environ",
            {"TURNSTILE_PROVIDER": "capsolver", "CAPSOLVER_API_KEY": ""},
            clear=False,
        ):
            token, trace = tp.mint_turnstile_token("0x4AAAAAAAtestkey")
        self.assertIsNone(token)
        self.assertIn("missing_CAPSOLVER_API_KEY", trace.get("error", ""))


class SolverEarlyPollUnit(unittest.IsolatedAsyncioTestCase):
    async def test_start_skips_click_when_early_token(self):
        import grok_register.register as register

        page = mock.AsyncMock()
        page.wait_for_timeout = mock.AsyncMock()
        item = {"page": page, "n": 1, "reused": True, "goto_s": 0.0}

        async def fake_get(_browser):
            return item

        async def fake_inject(_p, **_kwargs):
            return None

        async def fake_early(_p, **_kwargs):
            return "early-token-value-long-enough"

        click_called = []

        async def fake_click(_p, **_kwargs):
            click_called.append(1)
            return True

        old = (
            register._get_solver_page,
            register._inject_turnstile_widget,
            register._early_poll_turnstile_token,
            register._click_turnstile_if_possible,
            register._put_solver_page,
            register.SOLVER_TIMELINE_TRACE,
        )
        try:
            register.SOLVER_TIMELINE_TRACE = False
            register._get_solver_page = fake_get
            register._inject_turnstile_widget = fake_inject
            register._early_poll_turnstile_token = fake_early
            register._click_turnstile_if_possible = fake_click
            register._put_solver_page = mock.AsyncMock()
            out = await register._start_turnstile_challenge(object(), fast_click=True)
        finally:
            (
                register._get_solver_page,
                register._inject_turnstile_widget,
                register._early_poll_turnstile_token,
                register._click_turnstile_if_possible,
                register._put_solver_page,
                register.SOLVER_TIMELINE_TRACE,
            ) = old

        self.assertEqual(out["early_token"], "early-token-value-long-enough")
        self.assertTrue(out["trace"]["click_skipped"])
        self.assertEqual(click_called, [])

    async def test_external_solve_bypasses_browser_start(self):
        import grok_register.register as register

        old_uses = register.TURNSTILE_USES_BROWSER
        old_key = register.SITE_KEY
        old_mint = register.turnstile_provider_mod.mint_turnstile_token
        try:
            register.TURNSTILE_USES_BROWSER = False
            register.SITE_KEY = "0x4AAAAAAAtest"
            register.turnstile_provider_mod.mint_turnstile_token = lambda key: (
                "z" * 40,
                {"provider": "http", "goto_s": 0.1, "wait_s": 0.2,
                 "reused": False, "reuse_count": 0, "inject_s": 0.0,
                 "initial_s": 0.0, "click_s": 0.0, "visible_frame": False},
            )
            token, trace = await register.solve_one_turnstile_with_trace(object())
        finally:
            register.TURNSTILE_USES_BROWSER = old_uses
            register.SITE_KEY = old_key
            register.turnstile_provider_mod.mint_turnstile_token = old_mint
        self.assertEqual(token, "z" * 40)
        self.assertEqual(trace["provider"], "http")


if __name__ == "__main__":
    unittest.main()
