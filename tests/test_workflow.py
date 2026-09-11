import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


import auto_proxy
import main


class WorkflowTests(unittest.TestCase):
    def test_js_fill_input_supports_password_fields(self):
        calls = []

        class Driver:
            def execute_script(self, script):
                calls.append(script)

        main.js_fill_input(Driver(), 'input[name="password"]', 'safe"password')
        self.assertIn('descriptor.set.call(el, "safe\\"password")', calls[0])

    def test_proxy_url_subscription_fallback_writes_pool(self):
        raw = json.dumps({
            "outbounds": [{
                "type": "shadowsocks",
                "tag": "single-node",
                "server": "example.com",
                "server_port": 8388,
                "method": "2022-blake3-aes-128-gcm",
                "password": "test-password",
            }]
        })
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                with mock.patch.dict(os.environ, {"PROXY_URL": "https://example.com/sub", "TEST_PROXY_URL_MODE": "1"}), \
                        mock.patch.object(auto_proxy, "fetch_subscription", return_value=[(
                            "singbox",
                            {
                                "type": "shadowsocks",
                                "name": "single-node",
                                "server": "example.com",
                                "server_port": 8388,
                                "method": "2022-blake3-aes-128-gcm",
                                "password": "test-password",
                            },
                        )]):
                    auto_proxy.main()
                config = json.loads(Path("config.json").read_text(encoding="utf-8"))
                pool = json.loads(Path("ranked_pool.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(config["route"]["final"], "proxy")
        self.assertEqual(config["experimental"]["clash_api"]["external_controller"], "127.0.0.1:9099")
        self.assertEqual([item["tag"] for item in pool], ["node-1"])


if __name__ == "__main__":
    unittest.main()
