import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import auto_proxy
from fallback_proxy import FallbackProxyError, parse_fallback_proxies
import proxy_config


class FallbackParserTests(unittest.TestCase):
    def test_multiline_authenticated_proxies_preserve_order_and_deduplicate(self):
        nodes = parse_fallback_proxies('192.0.2.1:8080:user:pass\n192.0.2.2:8081:user:pass\n192.0.2.1:8080:user:pass')
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0][1], {'type': 'http', 'server': '192.0.2.1', 'server_port': 8080,
                                        'username': 'user', 'password': 'pass'})
        self.assertEqual(nodes[1][0], 'fallback-02')

    def test_url_encoded_socks_credentials(self):
        outbound = parse_fallback_proxies('socks5://a%40b:p%3Ass%40word@example.com:1081')[0][1]
        self.assertEqual(outbound['username'], 'a@b')
        self.assertEqual(outbound['password'], 'p:ss@word')
        self.assertEqual(outbound['type'], 'socks')
        self.assertEqual(outbound['version'], '5')

    def test_https_proxy_enables_certificate_validation(self):
        outbound = parse_fallback_proxies('https://user:pass@example.com:443')[0][1]
        self.assertEqual(outbound['tls'], {'enabled': True, 'server_name': 'example.com'})

    def test_json_and_blank_lines(self):
        nodes = parse_fallback_proxies(json.dumps(['', '# standby', 'example.com:8080:user:pass']))
        self.assertEqual(len(nodes), 1)

    def test_ipv6_plain_and_url(self):
        for raw in ('[2001:db8::1]:8080:user:pass', 'http://user:pass@[2001:db8::1]:8080'):
            self.assertEqual(parse_fallback_proxies(raw)[0][1]['server'], '2001:db8::1')

    def test_password_can_contain_colons(self):
        self.assertEqual(parse_fallback_proxies('example.com:8080:user:a:b:c')[0][1]['password'], 'a:b:c')

    def test_invalid_entries_do_not_echo_credentials(self):
        for entry in ('host:70000:private-user:private-password',
                      'http://private-user:private-password@host:bad',
                      'ftp://private-user:private-password@host:22',
                      'http://private-user:private-password@host/subscription'):
            with self.subTest(entry=entry), self.assertRaises(FallbackProxyError) as caught:
                parse_fallback_proxies(entry)
            self.assertNotIn('private-', str(caught.exception))

    def test_empty_optional_secret_is_allowed(self):
        self.assertEqual(parse_fallback_proxies(' \n'), [])

    def test_json_must_contain_strings(self):
        with self.assertRaises(FallbackProxyError):
            parse_fallback_proxies('[123]')


class SubscriptionParserTests(unittest.TestCase):
    LINKS = ('socks5://user:pass@one.example.com:1080\n'
             'socks5://user:pass@two.example.com:1080')

    def test_plaintext_links_are_not_concatenated(self):
        self.assertEqual(len(auto_proxy._from_base64(self.LINKS)), 2)

    def test_base64_links_support_whitespace_and_missing_padding(self):
        raw = base64.urlsafe_b64encode(self.LINKS.encode()).decode().rstrip('=')
        self.assertEqual(len(auto_proxy._from_base64(raw[:20] + '\n' + raw[20:])), 2)

    def test_sip002_and_plain_shadowsocks_credentials(self):
        encoded = base64.urlsafe_b64encode(b'aes-128-gcm:password').decode().rstrip('=')
        for link in (f'ss://{encoded}@example.com:8388',
                     'ss://aes-128-gcm:password@example.com:8388'):
            node = auto_proxy.parse_share_link(link)
            self.assertEqual(node['cipher'], 'aes-128-gcm')
            self.assertEqual(node['password'], 'password')

    def test_trojan_always_has_tls(self):
        node = auto_proxy.parse_share_link('trojan://password@example.com:443')
        self.assertTrue(auto_proxy.to_outbound(node, 'test')['tls']['enabled'])

    def test_one_malformed_entry_does_not_break_normalization(self):
        nodes = auto_proxy.normalize_nodes([
            ('clash', {'type': 'vless', 'server': 'bad.example', 'port': 'invalid'}),
            ('clash', {'type': 'http', 'server': 'good.example', 'port': 8080}),
        ])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0][1]['server'], 'good.example')

    def test_timeout_retries_do_not_log_subscription_url(self):
        with mock.patch.object(auto_proxy.urllib.request, 'urlopen', side_effect=TimeoutError('private-subscription-token')), \
                mock.patch.object(auto_proxy.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as log:
            with self.assertRaises(auto_proxy.SubscriptionError):
                auto_proxy.fetch_subscription('https://example.com/private-subscription-token')
        self.assertEqual(log.getvalue().count('failed'), 3)
        self.assertNotIn('private-subscription-token', log.getvalue())

    def test_permanent_http_error_is_not_retried(self):
        error = auto_proxy.urllib.error.HTTPError('https://example.com/secret', 401, 'unauthorized', {}, None)
        with mock.patch.object(auto_proxy.urllib.request, 'urlopen', side_effect=error) as get, \
                mock.patch.object(auto_proxy.time, 'sleep') as sleep:
            with self.assertRaises(auto_proxy.SubscriptionError):
                auto_proxy.fetch_subscription('https://example.com/secret')
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()


class ProxyConfigurationTests(unittest.TestCase):
    def test_vless_vision_alias_is_normalized_without_mutation(self):
        node = {'type': 'vless', 'server': 'example.com', 'server_port': '443',
                'flow': 'xtls-rprx-vision-udp443', 'tls': {'server_name': 'example.com'}}
        output = proxy_config.sanitize_outbound(node)
        self.assertEqual(output['flow'], 'xtls-rprx-vision')
        self.assertTrue(output['tls']['enabled'])
        self.assertNotIn('enabled', node['tls'])
        self.assertEqual(node['flow'], 'xtls-rprx-vision-udp443')

    def test_unknown_flow_and_fingerprint_are_quarantined(self):
        base = {'type': 'vless', 'server': 'example.com', 'server_port': 443}
        self.assertIsNone(proxy_config.sanitize_outbound(dict(base, flow='unsupported-flow')))
        self.assertIsNone(proxy_config.sanitize_outbound(dict(base, tls={'utls': {'fingerprint': 'unsafe'}})))

    def test_invalid_ports_are_rejected(self):
        for port in (0, 65536, True, None, 'bad'):
            self.assertIsNone(proxy_config.sanitize_outbound({'type': 'http', 'server': 'example.com', 'server_port': port}))

    def test_httpupgrade_is_supported_but_xhttp_is_not(self):
        node = {'type': 'vless', 'server': 'example.com', 'server_port': 443,
                'transport': {'type': 'httpupgrade'}}
        self.assertIsNotNone(proxy_config.sanitize_outbound(node))
        node['transport']['type'] = 'xhttp'
        self.assertIsNone(proxy_config.sanitize_outbound(node))

    def test_selector_has_no_latency_auto_or_direct_bypass(self):
        config = proxy_config.build_config([('one', {'type': 'http', 'server': 'example.com', 'server_port': 8080})])
        self.assertEqual(config['outbounds'][-1]['default'], 'node-1')
        self.assertEqual(config['route']['final'], 'proxy')
        self.assertNotIn('urltest', [outbound['type'] for outbound in config['outbounds']])
        self.assertNotIn('direct', [outbound['type'] for outbound in config['outbounds']])

    def test_invalid_outbound_index_removes_only_bad_node(self):
        good = ('good', {'type': 'http', 'server': 'example.com', 'server_port': 8080})
        bad = ('bad', {'type': 'http', 'server': 'example.com', 'server_port': 8081})
        results = [mock.Mock(returncode=1, stderr='initialize outbound[1]: invalid private-password', stdout=''),
                   mock.Mock(returncode=0, stderr='', stdout=''), mock.Mock(returncode=0, stderr='', stdout='')]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(proxy_config.subprocess, 'run', side_effect=results), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            path = Path(directory) / 'config.json'
            usable = proxy_config.validate_nodes([good, bad], path=path)
            config = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(usable, [good])
        self.assertEqual(config['outbounds'][0]['server_port'], 8080)
        self.assertNotIn('private-password', log.getvalue())

    def test_unindexed_error_is_bisected_and_merged_config_is_written(self):
        nodes = [(str(i), {'type': 'http', 'server': 'example.com', 'server_port': 8080 + i}) for i in range(4)]
        def check(args, **kwargs):
            config = json.loads(Path(args[-1]).read_text(encoding='utf-8'))
            bad = any(ob.get('server_port') == 8081 for ob in config['outbounds'])
            return mock.Mock(returncode=int(bad), stderr='unindexed error' if bad else '', stdout='')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(proxy_config.subprocess, 'run', side_effect=check):
            path = Path(directory) / 'config.json'
            usable = proxy_config.validate_nodes(nodes, path=path)
            config = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual([n[0] for n in usable], ['0', '2', '3'])
        self.assertEqual([ob['tag'] for ob in config['outbounds']], ['node-1', 'node-2', 'node-3', 'proxy'])

    def test_all_rejected_nodes_raise_instead_of_writing_empty_selector(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(proxy_config.subprocess, 'run', return_value=mock.Mock(returncode=1, stderr='outbound[0]: invalid', stdout='')):
            with self.assertRaises(proxy_config.ProxyConfigurationError):
                proxy_config.validate_nodes([('bad', {'type': 'http', 'server': 'example.com', 'server_port': 8080})],
                                            path=Path(directory) / 'config.json')
