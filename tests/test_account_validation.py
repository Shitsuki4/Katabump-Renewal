import contextlib
import io
import json
import os
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock


import main


class AccountValidationTests(unittest.TestCase):
    def test_users_json_username_and_email_are_supported(self):
        with mock.patch.dict(os.environ, {
            "USERS_JSON": json.dumps([
                {"username": "one@example.com", "password": "one"},
                {"email": "two@example.com", "password": "two"},
            ]),
        }):
            accounts = main.load_accounts()
        self.assertEqual(
            accounts,
            [
                {"email": "one@example.com", "password": "one"},
                {"email": "two@example.com", "password": "two"},
            ],
        )

    def test_validate_config_rejects_non_integer_attempts(self):
        with mock.patch.dict(os.environ, {
            "USERS_JSON": json.dumps([{"username": "user@example.com", "password": "secret"}]),
            "NODE_ATTEMPTS": "bad",
        }), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.validate_config(), 1)
        self.assertIn("NODE_ATTEMPTS must be an integer", output.getvalue())

    def test_validate_config_rejects_missing_password(self):
        with mock.patch.dict(os.environ, {
            "USERS_JSON": json.dumps([{"username": "user@example.com", "password": ""}]),
        }), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.validate_config(), 1)
        self.assertIn("缺少密码", output.getvalue())

    def test_validate_config_accepts_users_json(self):
        with mock.patch.dict(os.environ, {
            "USERS_JSON": json.dumps([
                {"username": "user@example.com", "password": "secret"},
                {"username": "other@example.com", "password": "secret"},
            ]),
            "NODE_ATTEMPTS": "4",
        }), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.validate_config(), 0)
        self.assertIn("2 个账号", output.getvalue())


class ConfigurationExitCodeTests(unittest.TestCase):
    def test_invalid_config_has_nonzero_cli_exit(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"USERS_JSON", "KATABUMP_EMAIL", "KATABUMP_PASSWORD", "FALLBACK_PROXIES",
                                      "NODE_ATTEMPTS", "FALLBACK_ATTEMPTS", "RUN_BUDGET_SECONDS"}}
        environment["USERS_JSON"] = '[{"email":"test@example.com","password":""}]'
        process = subprocess.run([sys.executable, "-X", "utf8", "main.py", "--validate-config"],
                                 cwd=Path(__file__).resolve().parents[1], env=environment,
                                 capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 1)
        self.assertIn("缺少密码", process.stdout)

    def test_invalid_account_is_not_silently_dropped(self):
        with mock.patch.dict(os.environ, {"USERS_JSON": '[{"email":"ok@example.com","password":"ok"}, {"password":"oops"}]'}, clear=True):
            self.assertEqual(main.validate_config(), 1)

    def test_wrong_account_shapes_fail(self):
        for value in ('{}', '[null]', '["not-an-object"]', '[]', '[{"email":1,"password":"test"}]'):
            with mock.patch.dict(os.environ, {"USERS_JSON": value}, clear=True):
                self.assertEqual(main.validate_config(), 1)

    def test_blank_attempt_setting_uses_default(self):
        with mock.patch.dict(os.environ, {"NODE_ATTEMPTS": "  "}, clear=True):
            self.assertEqual(main.parse_node_attempts(), 3)

    def test_out_of_range_attempts_fail(self):
        for value in ('0', '-1', '100000'):
            with mock.patch.dict(os.environ, {"NODE_ATTEMPTS": value}, clear=True):
                with self.assertRaises(ValueError):
                    main.parse_node_attempts()


class RenewalResultTests(unittest.TestCase):
    def test_negative_renewed_text_is_not_success(self):
        for text in ('Server not renewed', 'Server has not been renewed',
                     'Server not successfully renewed', 'Error: server was not renewed',
                     'Unable to renew your server', "You can't renew an expired server"):
            self.assertEqual(main._renew_feedback_outcome(text), 'failure')

    def test_only_explicit_temporary_window_is_not_due(self):
        self.assertEqual(main._renew_feedback_outcome("You can't renew yet, you will be able to tomorrow"), 'not_due')
        self.assertEqual(main._renew_feedback_outcome("You can't renew because you are not eligible"), 'failure')

    def test_explicit_success(self):
        self.assertEqual(main._renew_feedback_outcome('Your server has been renewed successfully'), 'success')

    def test_expiry_must_advance_not_just_change(self):
        self.assertTrue(main._expiry_advanced('2026-09-14', '2026-09-16'))
        self.assertFalse(main._expiry_advanced('2026-09-14', '2026-09-13'))
        self.assertFalse(main._expiry_advanced('2026-09-14', '2026-09-14'))
        self.assertFalse(main._expiry_advanced('2026-09-14', 'not-a-date'))
