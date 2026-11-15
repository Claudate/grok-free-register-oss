"""Regression: HTTP pool must not reuse leftover SSO across emails.

Root cause (PROTOCOL_HTTP merge): XAIHttpClientPool.release put the Session
back with sso still in the jar; server_action_register treated jar cookies as
success (sso_source=jar). auth-sessions.jsonl then grew with many emails
sharing a handful of real session_ids.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grok_register import http_protocol as hp  # noqa: E402
from grok_register import register as reg  # noqa: E402


def _fake_jwt(session_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"session_id": session_id}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


class _Jar:
    """Minimal cookie jar that supports set / get / del / iteration."""

    def __init__(self):
        self._items: dict[str, str] = {}

    def set(self, name, value, domain=None, path=None):
        if value == "" or value is None:
            self._items.pop(name, None)
        else:
            self._items[name] = str(value)

    def get(self, name, default=None):
        return self._items.get(name, default)

    def __delitem__(self, name):
        self._items.pop(name, None)

    def __contains__(self, name):
        return name in self._items

    def __iter__(self):
        return iter(self._items)

    def items(self):
        return self._items.items()

    def keys(self):
        return self._items.keys()

    def clear(self, *args):
        if not args:
            self._items.clear()
            return
        # jar.clear(domain, path, name)
        if len(args) >= 3:
            self._items.pop(args[2], None)


class FakeSession:
    def __init__(self):
        self.cookies = _Jar()
        self.headers = {"cookie": ""}

    def close(self):
        pass


class TestPurgeAuthCookies(unittest.TestCase):
    def test_purge_removes_sso_keeps_clearance(self):
        sess = FakeSession()
        sess.cookies.set("sso", "old-jwt-value")
        sess.cookies.set("sso-rw", "old-rw")
        sess.cookies.set("cf_clearance", "cf-token")
        hp._purge_auth_cookies(sess)
        self.assertIsNone(sess.cookies.get("sso"))
        self.assertIsNone(sess.cookies.get("sso-rw"))
        self.assertEqual(sess.cookies.get("cf_clearance"), "cf-token")

    def test_clear_auth_cookies_strips_header(self):
        client = object.__new__(hp.XAIHttpClient)
        client._lock = __import__("threading").RLock()
        client.session = FakeSession()
        client.session.cookies.set("sso", "polluted")
        client.session.cookies.set("cf_clearance", "keep-me")
        client.session.headers["cookie"] = "sso=polluted; cf_clearance=keep-me"
        client.clear_auth_cookies()
        self.assertIsNone(client.session.cookies.get("sso"))
        self.assertEqual(client.session.cookies.get("cf_clearance"), "keep-me")
        hdr = client.session.headers.get("cookie") or ""
        self.assertNotIn("sso=", hdr)
        self.assertIn("cf_clearance=keep-me", hdr)


class TestRegisterRejectsJarSource(unittest.TestCase):
    def test_sso_session_id_decode(self):
        jwt = _fake_jwt("abc-123")
        self.assertEqual(reg._sso_session_id(jwt), "abc-123")

    def test_persist_dedupes_session_id(self):
        jwt = _fake_jwt("unique-sid-1")
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            keys = td_path / "keys"
            keys.mkdir()
            # chdir so relative keys/* paths resolve into temp
            old = Path.cwd()
            try:
                import os

                os.chdir(td)
                reg._PERSISTED_SESSION_IDS.clear()
                cookies = [
                    {
                        "name": "sso",
                        "value": jwt,
                        "domain": ".x.ai",
                        "path": "/",
                    }
                ]
                reg._persist_registration("a@x.com", "pw1", jwt, cookies)
                reg._persist_registration("b@x.com", "pw2", jwt, cookies)
                lines = (keys / "auth-sessions.jsonl").read_text().strip().splitlines()
                self.assertEqual(len(lines), 1)
                acc = (keys / "accounts.txt").read_text().strip().splitlines()
                self.assertEqual(len(acc), 1)
                self.assertTrue(acc[0].startswith("a@x.com:"))
            finally:
                os.chdir(old)
                reg._PERSISTED_SESSION_IDS.clear()


class TestSignupResultIsolatesPriorSso(unittest.TestCase):
    """Pre-polluted pool jar must not become a new registration success."""

    def test_prior_jar_sso_is_cleared_not_reused(self):
        client = object.__new__(hp.XAIHttpClient)
        client._lock = __import__("threading").RLock()
        client.session = FakeSession()
        # Pre-pollute as if a prior account left sso in the jar.
        old = _fake_jwt("old-session")
        client.session.cookies.set("sso", old)

        class Resp:
            status_code = 200
            text = "1:HL[\"$undefined\",null]"
            headers = {"content-type": "text/x-component"}
            content = b""

        def fake_post(*_a, **_k):
            return Resp()

        client.session.post = fake_post
        client.refresh_clearance = lambda force=True: {}
        client.follow_set_cookie = lambda url: None
        client.create_session = lambda **_k: hp.SignupResult(ok=False, error="skip")
        client._cookie_headers = lambda: {}
        # clear_auth_cookies is real — should wipe polluted sso before POST
        client.clear_auth_cookies = types.MethodType(
            hp.XAIHttpClient.clear_auth_cookies, client
        )

        result = hp.XAIHttpClient.server_action_register(
            client,
            email="new@example.com",
            password="Secret1!",
            code="123456",
            turnstile_token="tok",
            action_id="deadbeef",
            state_tree="[]",
            create_session_fallback=False,
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.sso)
        self.assertNotEqual(result.sso_source, "jar")
        # Prior account sso must not remain after entry clear
        self.assertIsNone(client.session.cookies.get("sso"))
        self.assertNotEqual(result.sso, old)

    def test_same_request_session_sso_accepted(self):
        """After entry clear, Set-Cookie into jar is a real same-request success."""
        client = object.__new__(hp.XAIHttpClient)
        client._lock = __import__("threading").RLock()
        client.session = FakeSession()
        fresh = _fake_jwt("fresh-session")

        class Resp:
            status_code = 200
            text = "1:HL[\"$undefined\",null]"
            headers = {"content-type": "text/x-component"}
            content = b""

        def fake_post(*_a, **_k):
            # Simulate response side-effect: Set-Cookie lands in jar only
            client.session.cookies.set("sso", fresh)
            return Resp()

        client.session.post = fake_post
        client.refresh_clearance = lambda force=True: {}
        client.follow_set_cookie = lambda url: None
        client.create_session = lambda **_k: hp.SignupResult(ok=False, error="skip")
        client._cookie_headers = lambda: {}
        client.clear_auth_cookies = types.MethodType(
            hp.XAIHttpClient.clear_auth_cookies, client
        )

        result = hp.XAIHttpClient.server_action_register(
            client,
            email="new@example.com",
            password="Secret1!",
            code="123456",
            turnstile_token="tok",
            action_id="deadbeef",
            state_tree="[]",
            create_session_fallback=False,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.sso, fresh)
        self.assertEqual(result.sso_source, "session")


class TestPoolReleaseClearsSso(unittest.TestCase):
    def test_release_purges_sso(self):
        pool = object.__new__(hp.XAIHttpClientPool)
        pool._closed = False
        pool._q = __import__("queue").Queue()

        client = object.__new__(hp.XAIHttpClient)
        client._lock = __import__("threading").RLock()
        client.session = FakeSession()
        client.session.cookies.set("sso", "must-go")
        client.session.cookies.set("cf_clearance", "stay")
        client.clear_auth_cookies = types.MethodType(
            hp.XAIHttpClient.clear_auth_cookies, client
        )

        hp.XAIHttpClientPool.release(pool, client)
        self.assertIsNone(client.session.cookies.get("sso"))
        self.assertEqual(client.session.cookies.get("cf_clearance"), "stay")
        # slot returned to pool
        self.assertIs(pool._q.get_nowait(), client)


if __name__ == "__main__":
    unittest.main()
