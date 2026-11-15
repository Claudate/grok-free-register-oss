import os
import tempfile
import unittest

from grok_register.clearance import (
    ProxyPool,
    load_proxy_urls_from_env,
    proxy_host_label,
    proxy_url_to_playwright,
    reset_proxy_pool,
    get_proxy_pool,
)


class ProxyPoolTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "PROXY_FILE",
            "REGISTER_PROXY_LIST",
            "REGISTER_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "PROXY_STRATEGY",
            "PROXY_BAD_TTL_SEC",
        ):
            os.environ.pop(key, None)
        reset_proxy_pool()

    def test_load_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
            fh.write("# comment\n\n")
            fh.write("http://u1:p1@10.0.0.1:3129\n")
            fh.write("http://u2:p2@10.0.0.2:3129\n")
            path = fh.name
        os.environ["PROXY_FILE"] = path
        urls = load_proxy_urls_from_env()
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith("http://u1:"))

    def test_round_robin_and_mark_bad(self):
        pool = ProxyPool(
            [
                "http://a:x@1.1.1.1:1",
                "http://b:x@2.2.2.2:2",
                "http://c:x@3.3.3.3:3",
            ],
            strategy="round_robin",
        )
        first = pool.next()
        second = pool.next()
        self.assertNotEqual(first, second)
        pool.mark_bad(first)
        # subsequent picks should skip bad while others remain
        seen = {pool.next() for _ in range(6)}
        self.assertNotIn(first, seen)
        self.assertEqual(pool.bad_count(), 1)

    def test_playwright_split_auth(self):
        settings = proxy_url_to_playwright("http://user:pass@10.0.0.1:3129")
        self.assertEqual(
            settings,
            {
                "server": "http://10.0.0.1:3129",
                "username": "user",
                "password": "pass",
            },
        )

    def test_host_label_no_password(self):
        label = proxy_host_label("http://user:secret@10.0.0.1:3129")
        self.assertEqual(label, "10.0.0.1:3129")
        self.assertNotIn("secret", label)

    def test_get_proxy_pool_from_list(self):
        os.environ["REGISTER_PROXY_LIST"] = (
            "http://a:b@1.1.1.1:1,http://c:d@2.2.2.2:2"
        )
        reset_proxy_pool()
        pool = get_proxy_pool()
        self.assertEqual(pool.size, 2)


if __name__ == "__main__":
    unittest.main()
