import contextlib
import io
import json
import os
import unittest
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
