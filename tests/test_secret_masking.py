import contextlib
import io
import os
import unittest
from unittest import mock

from secrets_runtime import mask_workflow_secrets


class SecretMaskTests(unittest.TestCase):
    def test_local_runs_do_not_print_mask_commands_or_passwords(self):
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            mask_workflow_secrets([{'email': 'test@example.com', 'password': 'private-password'}])
        self.assertEqual(output.getvalue(), '')

    def test_actions_masks_structured_account_and_proxy_fields(self):
        env = {'GITHUB_ACTIONS': 'true', 'FALLBACK_PROXIES': 'example.com:8080:proxy-user:proxy-pass'}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as output:
            mask_workflow_secrets([{'email': 'test@example.com', 'password': 'line1\nline2%'}])
        lines = output.getvalue().splitlines()
        self.assertIn('::add-mask::proxy-pass', lines)
        self.assertIn('::add-mask::test@example.com', lines)
        self.assertIn('::add-mask::line1%0Aline2%25', lines)
