"""Grok contracts with fake clocks, files and billing replies; no network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from . import grok_monitor as grok


class GrokTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.now = 1790000000
        self.clock = lambda: self.now
        patcher = patch.object(grok, 'state_dir', return_value=self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.store = grok.GrokBotStore(self.clock)

    def post(self, state='working', bot='Research', **extra):
        self.store.accept({'v': 1, 'bot': bot, 'state': state, **extra})

    def write(self, filename, data):
        path = self.directory / filename
        path.write_text(json.dumps(data), encoding='utf-8')
        os.utime(path, (self.now, self.now))
        return path

    def test_validation_and_display(self):
        self.post(bot='Re\x00search', activity='é' * 32)
        bot = self.store.snapshot()['bots'][0]
        self.assertEqual(bot['name'], 'RESEARCH')
        self.assertEqual(len(bot['activity'].encode()), 16)
        for change in ({'v': True}, {'bot': ''}, {'bot': '\x00'},
                       {'bot': 'x' * 33}, {'state': 'unknown'},
                       {'state': []}, {'activity': 1}, {'secret': 'no'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.accept({'v': 1, 'bot': 'ok', 'state': 'idle', **change})

    def test_leases(self):
        for state, lease in grok.LEASES.items():
            with self.subTest(state=state):
                self.post(state, state)
                self.now += lease - 1
                self.assertIn(state.upper(), [b['name'] for b in self.store.snapshot()['bots']])
                self.now += 1
                self.assertNotIn(state.upper(), [b['name'] for b in self.store.snapshot()['bots']])

    def test_priority_eviction_counts(self):
        for i in range(17):
            self.post(bot=f'bot{i}')
            self.now += 1
        snap = self.store.snapshot()
        self.assertEqual(snap['botCount'], 16)
        self.assertEqual(snap['active'], 16)
        self.assertNotIn('BOT0', self.store._bots)
        for state in ('idle', 'done', 'error', 'waiting'):
            self.post(state, state)
            self.now += 1
        self.assertEqual([b['state'] for b in self.store.snapshot()['bots']],
                         ['waiting', 'error', 'working', 'working'])

    def test_done_transitions_and_local_day(self):
        self.post('done')
        self.post('done')
        self.assertEqual(self.store.snapshot()['doneToday'], 1)
        self.post('working')
        self.post('done')
        self.assertEqual(self.store.snapshot()['doneToday'], 2)
        self.now += 86400
        self.assertEqual(self.store.snapshot()['doneToday'], 0)
        self.post('done')
        self.assertEqual(self.store.snapshot()['doneToday'], 1)

    def test_bot_file_and_invalid_logged_once(self):
        path = self.write('grok-bots.json', {'v': 1, 'bots': [
            {'bot': 'File', 'state': 'done', 'updatedAt': self.now}]})
        self.assertEqual(self.store.snapshot()['doneToday'], 1)
        self.assertEqual(self.store.snapshot()['doneToday'], 1)
        self.now += 1
        self.write(path.name, {'v': 1, 'bots': [
            {'bot': 'File', 'state': 'done', 'updatedAt': self.now}]})
        self.assertEqual(self.store.snapshot()['doneToday'], 1)
        with self.assertLogs('tokenserver.grok') as logs:
            for content in ('secret invalid', '{other invalid'):
                path.write_text(content)
                self.store.snapshot()
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('secret', ''.join(logs.output))
        self.now += 1800
        self.assertEqual(self.store.snapshot()['bots'], [])

    def test_manual_and_auto_selection(self):
        monitor = grok.GrokMonitor(clock=self.clock)
        self.assertEqual(monitor.snapshot()['source'], 'none')
        self.write('grok.json', {'v': 1, 'usedPct': 42.9, 'resetAt': self.now + 90,
                                 'label': 'weekly'})
        data = monitor.snapshot()
        self.assertEqual((data['source'], data['usedPct'], data['resetSeconds']),
                         ('manual', 42, 90))
        self.assertEqual(data['label'], 'WEEKLY')
        self.now += 601
        self.assertTrue(monitor.snapshot()['stale'])
        self.assertEqual(monitor.snapshot()['ageSeconds'], 601)
        self.assertEqual(grok.GrokMonitor('bots', self.clock).snapshot()['source'], 'bots')
        self.assertEqual(grok.GrokMonitor('xai', self.clock).snapshot()['source'], 'none')
        for bad in (True, -1, 101, float('nan'), '42'):
            self.now += 1
            self.write('grok.json', {'v': 1, 'usedPct': bad})
            with self.assertLogs('tokenserver.grok') if not monitor.manual._file.warned else patch.object(grok.log, 'warning'):
                self.assertEqual(monitor.snapshot()['source'], 'none')
        # Auto order: xai, then live bot activity, then the manual file.
        self.now += 1
        self.write('grok.json', {'v': 1, 'usedPct': 10})
        self.assertEqual(monitor.snapshot()['source'], 'manual')
        monitor.store.accept({'v': 1, 'bot': 'Research', 'state': 'working'})
        data = monitor.snapshot()
        self.assertEqual((data['source'], data['active']), ('bots', 1))
        self.assertNotIn('usedPct', data)
        self.assertEqual(grok.GrokMonitor('manual', self.clock).snapshot()['source'],
                         'manual')
        self.now += 1
        self.write('grok.json', {'v': 1, 'usedPct': 'bad'})
        with self.assertLogs('tokenserver.grok'):
            self.assertEqual(grok.GrokMonitor('manual', self.clock).snapshot()['source'], 'none')

    def test_xai_fields_stale_secrets_and_selection(self):
        os.environ.update(XAI_MANAGEMENT_KEY='supersecret', XAI_TEAM_ID='team')
        replies = iter([
            {'coreInvoice': {'totalWithCorr': {'val': '123'}, 'amountAfterVat': '999',
                             'prepaidCredits': {'val': '-4500'}},
             'effectiveSpendingLimit': '20000'},
            RuntimeError('supersecret'),
            {'coreInvoice': {'amountAfterVat': '-5'}, 'effectiveSpendingLimit': '0'}])

        def fetch():
            result = next(replies)
            if isinstance(result, Exception):
                raise result
            return result

        monitor = grok.GrokMonitor(clock=self.clock, fetch=fetch)
        self.assertEqual(monitor.snapshot()['source'], 'xai')
        self.assertNotIn('spentCents', monitor.snapshot())
        monitor.xai.poll()
        data = monitor.snapshot()
        self.assertEqual([data[k] for k in ('spentCents', 'prepaidCents', 'limitCents')],
                         [123, 4500, 20000])
        self.now += 30
        with self.assertLogs('tokenserver.grok') as logs:
            monitor.xai.poll()
        data = monitor.snapshot()
        self.assertTrue(data['stale'])
        self.assertEqual(data['ageSeconds'], 30)
        self.assertEqual(data['spentCents'], 123)
        self.assertNotIn('supersecret', json.dumps(data) + ''.join(logs.output))
        monitor.xai.poll()
        self.assertFalse(monitor.snapshot()['stale'])
        self.assertEqual(monitor.snapshot()['spentCents'], 0)
        self.assertNotIn('limitCents', monitor.snapshot())
        self.now += 601
        self.assertTrue(monitor.snapshot()['stale'])

    def test_thread_first_poll_and_stop(self):
        import threading
        os.environ.update(XAI_MANAGEMENT_KEY='secret', XAI_TEAM_ID='team')
        called = threading.Event()

        def fetch():
            called.set()
            return {'coreInvoice': {'amountAfterVat': '7'}}

        source = grok.XaiApiUsageSource(fetch, self.clock)
        source.start()
        self.addCleanup(source.stop)
        self.assertTrue(called.wait(1))
        source.stop()
        self.assertIsNone(source._thread)

    def test_payload_bound_and_disabled(self):
        os.environ.update(XAI_MANAGEMENT_KEY='secret', XAI_TEAM_ID='team')
        monitor = grok.GrokMonitor(clock=self.clock, fetch=lambda: {
            'coreInvoice': {'amountAfterVat': str(10**50),
                            'prepaidCredits': {'val': str(-10**50)}},
            'effectiveSpendingLimit': str(10**50)})
        monitor.xai.poll()
        for i in range(16):
            monitor.store.accept({'v': 1, 'bot': str(i) + 'é' * 30,
                                  'state': 'waiting', 'activity': '\\"' * 16})
        data = grok.grok_snapshot(monitor)
        self.assertLess(len(json.dumps(data).encode()), 480)
        self.assertLessEqual(len(data['bots']), 4)
        self.assertEqual(data['active'], 16)
        self.assertEqual(grok.grok_snapshot(), {'v': 1, 'enabled': False})

    def test_manual_schema_and_recovery(self):
        source = grok.ManualQuotaSource(self.clock)
        self.assertIsNone(source.snapshot())
        for extra in ({'label': 'x' * 17}, {'resetAt': True}, {'resetAt': -1},
                      {'label': 123}, {'extra': 'secret'}, {'v': True}):
            self.now += 1
            self.write('grok.json', {'v': 1, 'usedPct': 5, **extra})
            with patch.object(grok.log, 'warning'):
                self.assertIsNone(source.snapshot())
        self.now += 1
        path = self.write('grok.json', {'v': 1, 'usedPct': 0})
        data = source.snapshot()
        self.assertEqual(data['usedPct'], 0)
        self.assertNotIn('resetSeconds', data)
        self.assertNotIn('label', data)
        path.unlink()
        self.assertIsNone(source.snapshot())

    def test_xai_unconfigured_never_fetches(self):
        fetch = Mock()
        os.environ['XAI_MANAGEMENT_KEY'] = 'secret'
        source = grok.XaiApiUsageSource(fetch, self.clock)
        source.start()
        source.poll()
        source.stop()
        self.assertIsNone(source.snapshot())
        fetch.assert_not_called()

    def test_cli_posts_expected_headers_and_reports_failure(self):
        from . import grok_status
        args = ['grok_status.py', '--bot', 'Research', '--state', 'working',
                '--activity', 'searching']
        with patch('sys.argv', args), patch.object(grok_status, 'urlopen') as opener:
            self.assertEqual(grok_status.main(), 0)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8737/api/grok/bots')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertFalse(request.has_header('Origin'))
        self.assertEqual(json.loads(request.data), {
            'v': 1, 'bot': 'Research', 'state': 'working', 'activity': 'searching'})
        with patch('sys.argv', args), patch.object(grok_status, 'urlopen',
                                                  side_effect=OSError()), \
                patch('sys.stderr') as stderr:
            self.assertEqual(grok_status.main(), 1)
            self.assertTrue(stderr.write.called)
