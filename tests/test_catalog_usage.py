import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_runner.catalog import last_context, load_sessions, projects, register_paused
from codex_runner.cli import PROMPT, Store
from codex_runner.usage import UsagePoller, limit_line, snapshot, usage_lines

SID = '01a151c8-b419-73f3-8fd2-119ba622ac45'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.job = {'id': SID, 'codex': '/fake/codex', 'codex_home': str(self.home), 'path': os.environ['PATH'], 'limit_id': 'codex'}

    def tearDown(self):
        self.temp.cleanup()

    def test_session_catalog_pages_read_only_metadata_and_groups_projects(self):
        calls = []
        class Server:
            def rpc(inner, method, params):
                calls.append((method, params))
                assert method == 'thread/list'
                if params.get('cursor'):
                    return {'data': [{'id': 'b', 'cwd': '/project', 'updatedAt': 20}], 'nextCursor': None}
                return {'data': [{'id': 'a', 'cwd': '/project', 'name': '작업 이름', 'updatedAt': 10}], 'nextCursor': 'next'}
            def close(inner):
                pass
        with patch('codex_runner.managed.LocalServer', return_value=Server()):
            sessions = load_sessions(self.job)
        self.assertEqual([s['id'] for s in sessions], ['b', 'a'])
        self.assertEqual(projects(sessions), [{'cwd': '/project', 'count': 2, 'updated': 20.0}])
        self.assertEqual([m for m, p in calls], ['thread/list', 'thread/list'])
        self.assertEqual(calls[0][1]['sortKey'], 'updated_at')

    def test_offline_metadata_fallback_keeps_codex_database_unchanged(self):
        p = self.home / 'state_5.sqlite'
        c = sqlite3.connect(p)
        c.execute('CREATE TABLE threads(id TEXT, cwd TEXT, title TEXT, updated_at INT, archived INT)')
        c.executemany('INSERT INTO threads VALUES (?,?,?,?,?)', [(SID, '/project', '최근 작업', 100, 0), ('old', '/old', 'archive', 200, 1)])
        c.commit(); c.close()
        before = p.read_bytes()
        with patch('codex_runner.managed.LocalServer', side_effect=FileNotFoundError()):
            sessions = load_sessions(self.job)
        self.assertEqual([v['id'] for v in sessions], [SID])
        self.assertEqual(p.read_bytes(), before)

    def test_registering_a_browsed_session_is_paused_and_never_calls_a_model(self):
        store = Store(self.home / 'runner')
        try:
            thread = {'id': SID, 'cwd': str(self.home), 'name': 'CI 검증', 'updatedAt': 123}
            with patch('codex_runner.catalog.probe_codex') as probe:
                sid = register_paused(store, thread, self.job, name='build')
            probe.assert_called_once()
            job = store.get(sid)
            self.assertEqual(job['status'], 'paused')
            self.assertEqual(job['session_title'], 'CI 검증')
            self.assertEqual(job['prompt'], PROMPT)
            self.assertEqual(job['sandbox'], 'workspace-write')
            self.assertTrue(job['managed'])
            register_paused(store, thread, self.job, name='other')
            self.assertEqual(store.get(sid)['name'], 'build')
        finally:
            store.db.close()

    def test_context_uses_last_request_instead_of_cumulative_totals(self):
        path = self.home / 'sessions/rollout.jsonl'
        path.parent.mkdir()
        events = [{'type': 'event_msg', 'timestamp': '2026-10-22T00:00:00Z', 'payload': {
            'type': 'token_count', 'info': {'total_token_usage': {'total_tokens': 99999999},
            'last_token_usage': {'total_tokens': 102990}, 'model_context_window': 475000}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}}]
        path.write_text('\n'.join(json.dumps(e) for e in events) + '\n')
        calls = []
        class Server:
            def rpc(inner, method, params):
                calls.append(method)
                return {'thread': {'id': SID, 'path': str(path), 'name': '실제 대화', 'updatedAt': 123}}
            def close(inner):
                pass
        with patch('codex_runner.managed.LocalServer', return_value=Server()):
            context = last_context(str(self.home), SID)
        self.assertEqual(context['used'], 102990)
        self.assertEqual(context['window'], 475000)
        self.assertEqual(context['title'], '실제 대화')
        self.assertEqual(calls, ['thread/read'])


class UsageTests(unittest.TestCase):
    def test_bars_show_remaining_allowance_and_unknown_is_not_zero(self):
        text = limit_line('5h limit', {'usedPercent': 33, 'resetsAt': 1792653418})
        self.assertIn('67% left', text)
        self.assertEqual(text.count('█') + text.count('░'), 20)
        self.assertEqual(limit_line('Weekly limit', None), 'Weekly limit: N/A')
        self.assertIn('0% left', limit_line('5h', {'usedPercent': 110}))

    def test_actual_context_and_snapshot_age_are_visible(self):
        lines = usage_lines({'fetched_at': time.time(), 'limits_at': 100,
            'limits_error': 'offline', 'limits': {'primary': {'usedPercent': 33}, 'secondary': {'usedPercent': 5}},
            'context': {'used': 249000, 'window': 475000}})
        self.assertIn('cached', lines[0])
        self.assertIn('249K used / 475K', lines[1])
        self.assertIn('67% left', lines[2])
        self.assertIn('95% left', lines[3])
        self.assertIn('~', lines[1])
        self.assertEqual(len(lines), 4)
        self.assertIn('Reset coupons: 1', usage_lines({'fetched_at': time.time(), 'limits': {}, 'coupons': 1})[-1])

    def test_context_failure_does_not_hide_working_account_limits(self):
        data = {'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 20}}}
        with patch('codex_runner.usage.read_limits', return_value=data) as rpc, patch('codex_runner.usage.last_context', side_effect=sqlite3.OperationalError('missing')):
            result = snapshot({'id': SID, 'codex_home': '/fake', 'limit_id': 'codex'})
        self.assertEqual(result['limits']['primary']['usedPercent'], 20)
        self.assertIn('context_error', result)
        rpc.assert_called_once()

    def test_background_refresh_is_nonblocking_cached_and_account_isolated(self):
        calls = []
        def load(job):
            calls.append(job['id'])
            return {'fetched_at': time.time(), 'limits': {'primary': {'usedPercent': len(calls)}}}
        with patch('codex_runner.usage.snapshot', side_effect=load):
            poller = UsagePoller(interval=30)
            job = {'id': 'one', 'codex_home': '/account-a'}
            other = {'id': 'two', 'codex_home': '/account-b'}
            try:
                poller.request(job)
                end = time.monotonic() + 2
                while poller.get(job) is None and time.monotonic() < end:
                    time.sleep(.01)
                self.assertIsNotNone(poller.get(job))
                self.assertIsNone(poller.get(other))
                poller.request(job)
                self.assertEqual(calls, ['one'])
                poller.request(job, force=True)
                end = time.monotonic() + 2
                while len(calls) < 2 and time.monotonic() < end:
                    time.sleep(.01)
                self.assertEqual(calls, ['one', 'one'])
            finally:
                poller.close()


if __name__ == '__main__':
    unittest.main()
