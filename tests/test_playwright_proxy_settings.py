import os
import unittest

from grok_register.clearance import playwright_proxy_settings, reset_proxy_pool


class PlaywrightProxySettingsTests(unittest.TestCase):
    def tearDown(self):
        for key in (
            "REGISTER_PROXY",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "PROXY_FILE",
            "REGISTER_PROXY_LIST",
        ):
            os.environ.pop(key, None)
        reset_proxy_pool()

    def test_splits_basic_auth(self):
        os.environ["REGISTER_PROXY"] = "http://user:pass@10.0.0.1:3129"
        reset_proxy_pool()
        self.assertEqual(
            playwright_proxy_settings(),
            {
                "server": "http://10.0.0.1:3129",
                "username": "user",
                "password": "pass",
            },
        )

    def test_no_auth(self):
        os.environ["REGISTER_PROXY"] = "http://10.0.0.1:3129"
        reset_proxy_pool()
        self.assertEqual(
            playwright_proxy_settings(),
            {"server": "http://10.0.0.1:3129"},
        )

    def test_empty(self):
        reset_proxy_pool()
        self.assertIsNone(playwright_proxy_settings())

    def test_multi_pool_launch_is_none(self):
        """Multi-proxy: launch not pinned; contexts rotate via next_*."""
        os.environ["REGISTER_PROXY_LIST"] = (
            "http://a:b@1.1.1.1:1,http://c:d@2.2.2.2:2"
        )
        reset_proxy_pool()
        self.assertIsNone(playwright_proxy_settings())


if __name__ == "__main__":
    unittest.main()
