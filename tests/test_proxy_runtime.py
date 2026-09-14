import subprocess
import unittest
from unittest import mock

import requests
import proxy_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def session(self):
        session = mock.MagicMock()
        patch = mock.patch.object(runtime.requests, 'Session')
        patch.start().return_value.__enter__.return_value = session
        self.addCleanup(patch.stop)
        return session

    def test_local_readiness_does_not_require_a_working_exit(self):
        session = self.session()
        session.get.return_value.json.return_value = {'now': 'node-1'}
        process = mock.Mock()
        process.poll.return_value = None
        self.assertEqual(runtime.wait_for_proxy(process), 'node-1')
        session.get.assert_called_once_with(runtime.CONTROL_URL, timeout=1)
        self.assertFalse(session.trust_env)

    def test_pin_requires_matching_selector_state(self):
        session = self.session()
        session.get.return_value.json.return_value = {'now': 'node-1'}
        with self.assertRaises(runtime.ProxyRuntimeError):
            runtime.pin_node('node-2')
        session.put.assert_called_once_with(runtime.CONTROL_URL, json={'name': 'node-2'}, timeout=5)

    def test_start_failure_cleans_up_owned_process(self):
        with mock.patch.object(runtime, '_require_free_ports'), \
                mock.patch.object(runtime.subprocess, 'run', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(runtime, 'spawn_singbox', return_value=mock.sentinel.process), \
                mock.patch.object(runtime, 'wait_for_proxy', side_effect=TimeoutError()), \
                mock.patch.object(runtime, 'stop_process') as stop:
            with self.assertRaises(TimeoutError):
                runtime.start_proxy()
        stop.assert_called_once_with(mock.sentinel.process)

    def test_stop_kills_only_owned_process_if_terminate_times_out(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('sing-box', 5), 0]
        runtime.stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_route_check_accepts_cloudflare_challenge_but_not_proxy_auth_failure(self):
        session = self.session()
        for status, expected in ((200, True), (403, True), (407, False), (429, False), (502, False)):
            session.get.return_value.status_code = status
            self.assertEqual(runtime.route_is_reachable(), expected)

    def test_route_check_handles_network_errors(self):
        session = self.session()
        session.get.side_effect = requests.ConnectionError('private-proxy-url')
        self.assertFalse(runtime.route_is_reachable())

    def test_ip_lookup_tries_second_service_and_rejects_html(self):
        session = self.session()
        session.get.side_effect = [mock.Mock(text='<html>blocked</html>'), mock.Mock(text='192.0.2.15\n')]
        self.assertEqual(runtime.get_exit_ip(runtime.PROXY_URL), '192.0.2.15')
        self.assertEqual(session.get.call_count, 2)
