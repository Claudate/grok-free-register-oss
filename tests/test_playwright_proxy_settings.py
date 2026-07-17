import os
import unittest

from grok_register.clearance import playwright_proxy_settings


class PlaywrightProxySettingsTests(unittest.TestCase):
    def tearDown(self):
        for key in ("REGISTER_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            os.environ.pop(key, None)

    def test_splits_basic_auth(self):
        os.environ["REGISTER_PROXY"] = "http://user:pass@10.0.0.1:3129"
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
        self.assertEqual(
            playwright_proxy_settings(),
            {"server": "http://10.0.0.1:3129"},
        )

    def test_empty(self):
        self.assertIsNone(playwright_proxy_settings())


if __name__ == "__main__":
    unittest.main()
