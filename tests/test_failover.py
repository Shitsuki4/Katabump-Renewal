import contextlib
import io
import os
import unittest
from unittest import mock

import renewal_runner as runner


ACCOUNTS = [{'email': 'one@example.com', 'password': 'test-password'},
            {'email': 'two@example.com', 'password': 'test-password'}]


def pool(count):
    return [{'tag': f'node-{index}'} for index in range(1, count + 1)]


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {'SUB_URL': 'https://example.com/sub',
                                                      'FALLBACK_PROXIES': 'example.com:8080:user:pass'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.prepare = mock.patch.object(runner.auto_proxy, 'prepare_source', return_value=pool(3)).start()
        self.start = mock.patch.object(runner, 'start_proxy', return_value=mock.sentinel.process).start()
        self.stop = mock.patch.object(runner, 'stop_process').start()
        self.pin = mock.patch.object(runner, 'pin_node').start()
        self.route = mock.patch.object(runner, 'route_is_reachable', return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.log = io.StringIO()
        self.output = contextlib.redirect_stdout(self.log)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_primary_success_never_prepares_or_contacts_standby(self):
        browser = mock.Mock(return_value=True)
        result = runner.run_renewals(ACCOUNTS, browser)
        self.assertEqual(result.failed, [])
        self.prepare.assert_called_once_with('subscription')
        self.pin.assert_called_once_with('node-1')
        self.assertEqual(browser.call_count, 2)
        self.stop.assert_called_once_with(mock.sentinel.process)

    def test_subscription_timeout_falls_back(self):
        self.prepare.side_effect = [TimeoutError('private-sub-url'), pool(10)]
        result = runner.run_renewals(ACCOUNTS[:1], mock.Mock(return_value=True))
        self.assertEqual(result.failed, [])
        self.assertEqual([call.args[0] for call in self.prepare.call_args_list], ['subscription', 'fallback'])
        self.assertNotIn('private-sub-url', self.log.getvalue())

    def test_proxy_startup_failure_also_falls_back(self):
        self.start.side_effect = [RuntimeError('cannot start'), mock.sentinel.backup]
        result = runner.run_renewals(ACCOUNTS[:1], mock.Mock(return_value=True))
        self.assertEqual(result.failed, [])
        self.stop.assert_called_once_with(mock.sentinel.backup)

    def test_only_failed_accounts_are_retried_on_standby(self):
        self.prepare.side_effect = [pool(1), pool(2)]
        browser = mock.Mock(side_effect=[True, False, True])
        result = runner.run_renewals(ACCOUNTS, browser, node_attempts=1)
        self.assertEqual(result.failed, [])
        self.assertEqual([call.args[1] for call in browser.call_args_list],
                         ['one@example.com', 'two@example.com', 'two@example.com'])

    def test_all_ten_backups_are_available_despite_primary_attempt_limit(self):
        self.prepare.side_effect = [pool(1), pool(10)]
        browser = mock.Mock(side_effect=[False] * 10 + [True])
        result = runner.run_renewals(ACCOUNTS[:1], browser, node_attempts=1)
        self.assertEqual(result.failed, [])
        self.assertEqual(browser.call_count, 11)
        self.assertEqual(self.pin.call_args_list[-1].args, ('node-10',))

    def test_invalid_selection_never_silently_reuses_current_node(self):
        self.prepare.side_effect = [pool(2), pool(1)]
        self.pin.side_effect = [RuntimeError('selector mismatch'), None, None]
        browser = mock.Mock(return_value=True)
        result = runner.run_renewals(ACCOUNTS[:1], browser)
        self.assertEqual(result.failed, [])
        self.assertEqual(browser.call_count, 1)
        self.assertEqual(self.pin.call_args_list[-1].args, ('node-2',))

    def test_unreachable_routes_skip_browser(self):
        self.prepare.side_effect = [pool(1), pool(1)]
        self.route.side_effect = [False, True]
        browser = mock.Mock(return_value=True)
        result = runner.run_renewals(ACCOUNTS[:1], browser)
        self.assertEqual(result.failed, [])
        self.assertEqual(browser.call_count, 1)
        self.assertIn('fallback', result.sources_used)

    def test_exhausted_pool_does_not_repeat_nodes_or_claim_success(self):
        self.prepare.side_effect = [pool(1), pool(2)]
        browser = mock.Mock(return_value=False)
        result = runner.run_renewals(ACCOUNTS[:1], browser, node_attempts=25)
        self.assertEqual(result.failed, [1])
        self.assertEqual(browser.call_count, 3)
        self.assertEqual(result.succeeded, 0)
        self.assertEqual(self.stop.call_count, 2)

    def test_proxy_url_tier_is_between_subscription_and_standby(self):
        os.environ['PROXY_URL'] = 'socks5://user:pass@example.com:1080'
        self.prepare.side_effect = [RuntimeError(), RuntimeError(), pool(1)]
        result = runner.run_renewals(ACCOUNTS[:1], mock.Mock(return_value=True))
        self.assertEqual(result.sources_used, ['subscription', 'proxy_url', 'fallback'])

    def test_no_subscription_means_direct_then_standby(self):
        del os.environ['SUB_URL']
        browser = mock.Mock(side_effect=[False, True])
        result = runner.run_renewals(ACCOUNTS[:1], browser, node_attempts=1)
        self.assertEqual(result.failed, [])
        self.assertNotIn('proxy', browser.call_args_list[0].args[0])
        self.assertEqual(browser.call_args_list[1].args[0]['proxy'], runner.PROXY_URL)
        self.prepare.assert_called_once_with('fallback')

    def test_time_budget_failure_still_stops_owned_process(self):
        clock = [0]
        def browser(*args):
            clock[0] = 4000
            return False
        with mock.patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]):
            result = runner.run_renewals(ACCOUNTS, browser)
        self.assertEqual(result.failed, [1, 2])
        self.stop.assert_called_once_with(mock.sentinel.process)
        self.prepare.assert_called_once_with('subscription')
