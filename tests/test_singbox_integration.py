"""Real sing-box checks and local-only HTTP CONNECT authentication test."""

import base64
import contextlib
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest import mock

import requests

from fallback_proxy import parse_fallback_proxies
import proxy_config
import proxy_runtime


BINARY = Path(proxy_runtime.singbox_binary())


class FakeAuthenticatedProxy(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        data = b''
        while b'\r\n\r\n' not in data:
            block = self.request.recv(4096)
            if not block:
                return
            data += block
        expected = b'Proxy-Authorization: Basic ' + base64.b64encode(b'test-user:test-password')
        if expected.lower() not in data.lower() or not data.startswith(b'CONNECT '):
            self.request.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
            return
        self.server.authenticated += 1
        self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        data = b''
        while b'\r\n\r\n' not in data:
            block = self.request.recv(4096)
            if not block:
                return
            data += block
        body = b'authenticated-standby-ok'
        self.request.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)


@unittest.skipUnless(BINARY.is_file(), 'Run python scripts/install_singbox.py for real integration checks')
class SingboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        patch = mock.patch.object(proxy_config, 'singbox_binary', return_value=str(BINARY))
        patch.start()
        self.addCleanup(patch.stop)

    def test_real_checker_accepts_normalized_vision_flow(self):
        outbound = proxy_config.sanitize_outbound({
            'type': 'vless', 'server': 'example.com', 'server_port': 443,
            'uuid': '00000000-0000-4000-8000-000000000001',
            'flow': 'xtls-rprx-vision-udp443', 'tls': {'server_name': 'example.com'},
        })
        self.assertEqual(len(proxy_config.validate_nodes([('vision', outbound)], path=self.path / 'vision.json')), 1)

    def test_real_checker_quarantines_unknown_field_without_losing_good_nodes(self):
        good = {'type': 'http', 'server': '127.0.0.1', 'server_port': 18001}
        bad = dict(good, unsupported_field='invalid')
        nodes = proxy_config.validate_nodes([('good', good), ('bad', bad)], path=self.path / 'isolation.json')
        self.assertEqual([name for name, _ in nodes], ['good'])

    def test_authenticated_http_fallback_through_actual_singbox(self):
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True
        with Server(('127.0.0.1', 0), FakeAuthenticatedProxy) as upstream:
            upstream.authenticated = 0
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                port = upstream.server_address[1]
                nodes = parse_fallback_proxies(f'127.0.0.1:{port}:test-user:test-password')
                with contextlib.chdir(self.path), \
                        mock.patch.object(proxy_runtime, 'singbox_binary', return_value=str(BINARY)):
                    proxy_config.validate_nodes(nodes, path='config.json', probe=False)
                    process = proxy_runtime.start_proxy()
                    try:
                        proxy_runtime.pin_node('node-1')
                        with requests.Session() as session:
                            session.trust_env = False
                            response = session.get('http://local-only.example.invalid/test',
                                                   proxies={'http': proxy_runtime.PROXY_URL}, timeout=10)
                        self.assertEqual(response.text, 'authenticated-standby-ok')
                        self.assertGreater(upstream.authenticated, 0)
                    finally:
                        proxy_runtime.stop_process(process)
                    self.assertIsNotNone(process.poll())
            finally:
                upstream.shutdown()
                thread.join(timeout=5)
