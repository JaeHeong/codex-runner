import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from codex_runner.cli import QuotaCache, RunnerError, Scheduler, Store, control_job, read_limits

FAKE = Path(__file__).with_name('fake_codex.py')


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.fixture = self.base / 'fake.json'
        self.fixture.write_text('{}')
        self.env = patch.dict(os.environ, {'FAKE_CODEX_STATE': str(self.fixture)})
        self.env.start()
        self.store = Store(self.base / 'state')
        self.stopping = threading.Event()
        self.scheduler = Scheduler(self.store, self.stopping, max_parallel=2)
        self.jobs = []
        for i in range(3):
            job = dict(id=str(uuid.uuid4()), name='job-' + str(i), cwd=str(self.base), codex=str(FAKE),
                       path=os.environ['PATH'], codex_home=None, model=None, limit_id='codex',
                       sandbox='workspace-write', status='queued', next_run=0, turns=0, created=i)
            self.jobs.append(job)

    def tearDown(self):
        self.scheduler.close()
        self.store.db.close()
        self.env.stop()
        self.temp.cleanup()

    def add(self, count=2):
        for job in self.jobs[:count]:
            self.store.add(job)

    def until(self, predicate, timeout=20 if os.name == 'nt' else 8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.scheduler.tick()
            if predicate():
                return
            time.sleep(.03)
        self.fail('Scheduler condition not reached: ' + str(self.store.jobs()))

    def calls(self):
        path = self.fixture.with_suffix('.calls')
        return [json.loads(v) for v in path.read_text().splitlines()] if path.exists() else []

    def test_long_running_session_does_not_block_other_session_or_duplicate_itself(self):
        first, second = self.jobs[:2]
        self.fixture.write_text(json.dumps({'modes': {first['id']: 'sleep'}}))
        self.add()
        self.until(lambda: self.store.get(first['id']).get('pid') and self.store.get(second['id'])['status'] == 'complete' and len(self.calls()) == 2)
        self.assertEqual(self.store.get(first['id'])['status'], 'running')
        for _ in range(15):
            self.scheduler.tick()
        self.assertCountEqual([args[-2] for args in self.calls()], [first['id'], second['id']])

    def test_limit_and_independent_pause_allow_next_session_to_run(self):
        self.fixture.write_text(json.dumps({'mode': 'sleep'}))
        self.add(3)
        self.until(lambda: sum(bool(j.get('pid')) for j in self.store.jobs()) == 2)
        running = [j for j in self.store.jobs() if j.get('pid')]
        queued = next(j for j in self.store.jobs() if j['status'] == 'queued')
        self.assertIn('worker slot', queued['detail'])
        control_job(self.store, running[0]['id'], 'pause')
        self.until(lambda: self.store.get(running[0]['id'])['status'] == 'paused'
                   and self.store.get(queued['id']).get('pid') and len(self.calls()) == 3)
        self.assertEqual(self.store.get(running[1]['id'])['status'], 'running')
        self.assertLessEqual(sum(bool(j.get('pid')) for j in self.store.jobs()), 2)
        self.assertEqual(len(self.calls()), 3)

    def test_shared_quota_waits_without_model_calls_then_each_session_recovers(self):
        self.fixture.write_text(json.dumps({'used': 100, 'reset': time.time() + 100}))
        self.add()
        with patch('codex_runner.cli.read_limits', wraps=read_limits) as quota:
            self.until(lambda: all(j['status'] == 'waiting' for j in self.store.jobs()))
            self.assertEqual(quota.call_count, 1)
            self.assertEqual(self.calls(), [])
            self.until(lambda: not self.scheduler.active)
            self.fixture.write_text('{}')
            for key, value in list(self.scheduler.quotas.entries.items()):
                self.scheduler.quotas.entries[key] = (0, value[1])
            for job in self.jobs[:2]:
                self.store.patch(job['id'], next_run=0)
            self.until(lambda: all(j['status'] == 'complete' for j in self.store.jobs()))
            self.assertEqual(quota.call_count, 2)
        self.assertCountEqual([args[-2] for args in self.calls()], [j['id'] for j in self.jobs[:2]])

    def test_one_session_failure_does_not_stop_its_peer(self):
        self.fixture.write_text(json.dumps({'modes': {self.jobs[0]['id']: 'error'}}))
        self.add()
        self.until(lambda: self.store.get(self.jobs[0]['id'])['status'] == 'error'
                   and self.store.get(self.jobs[1]['id'])['status'] == 'complete')

    def test_shutdown_stops_cli_children_and_keeps_unfinished_sessions_queued(self):
        self.fixture.write_text(json.dumps({'mode': 'sleep'}))
        self.add()
        self.until(lambda: all(j.get('pid') for j in self.store.jobs()))
        self.scheduler.close()
        for job in self.store.jobs():
            self.assertEqual(job['status'], 'queued')
            self.assertIsNone(job['pid'])

    def test_only_one_concurrent_claim_succeeds(self):
        self.add(1)
        results, failures = [], []
        barrier = threading.Barrier(2)
        def claim():
            store = Store(self.store.base)
            try:
                barrier.wait(timeout=3)
                results.append(store.claim(self.jobs[0]['id']))
            except Exception as exc:
                failures.append(exc)
            finally:
                store.db.close()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(failures)
        self.assertEqual(sum(v is not None for v in results), 1)
        self.assertEqual(self.calls(), [])

    def test_managed_sessions_are_observed_independently_and_reattach_on_restart(self):
        self.add()
        owned, other = self.jobs[:2]
        for job in self.jobs[:2]:
            self.store.patch(job['id'], managed=True, codex_home=str(self.base / 'codex'))
        self.store.patch(owned['id'], managed_turn_id='existing-owned-turn')
        completed = threading.Event()
        calls = []
        jobs = {j['id']: j for j in self.jobs[:2]}
        class Server:
            def rpc(inner, method, params):
                calls.append((method, dict(params)))
                if method in ('thread/resume', 'thread/read'):
                    sid = params['threadId']
                    return {'thread': {'id': sid, 'cwd': jobs[sid]['cwd'],
                                       'status': {'type': 'idle' if sid == owned['id'] and completed.is_set() else 'active'}}}
                if method == 'thread/turns/list':
                    sid = params['threadId']
                    return {'data': [{'id': 'existing-owned-turn' if sid == owned['id'] else 'interactive-turn',
                        'status': 'completed' if sid == owned['id'] and completed.is_set() else 'inProgress',
                        'items': [{'type': 'agentMessage', 'phase': 'final_answer',
                                   'text': json.dumps({'status': 'complete', 'summary': 'Verified existing turn'})}]}]}
                raise AssertionError('Unexpected write RPC: ' + method)
            def event(inner):
                time.sleep(.02)
                raise TimeoutError()
            def close(inner):
                pass
        with patch('codex_runner.managed.LocalServer', side_effect=lambda home: Server()):
            self.until(lambda: self.store.get(owned['id'])['status'] == 'running'
                       and self.store.get(other['id'])['status'] == 'waiting')
            self.scheduler.close()
            self.assertEqual(self.store.get(owned['id'])['status'], 'queued')
            self.assertEqual(self.store.get(owned['id'])['managed_turn_id'], 'existing-owned-turn')
            completed.set()
            self.scheduler = Scheduler(self.store, threading.Event(), max_parallel=2)
            self.until(lambda: self.store.get(owned['id'])['status'] == 'complete')
        self.assertFalse(any(method in ('turn/start', 'turn/interrupt') for method, _ in calls))

    def test_nonpositive_parallelism_is_rejected(self):
        with self.assertRaisesRegex(RunnerError, 'at least 1'):
            Scheduler(self.store, self.stopping, max_parallel=0)

    def test_legacy_response_guard_migration_preserves_actual_blockers_and_pauses(self):
        self.add(3)
        legacy = 'Last Codex turn completed; task completion is unknown. Review before sending another message.'
        for job in self.jobs:
            self.store.patch(job['id'], managed=True)
        self.store.patch(self.jobs[0]['id'], status='blocked', detail=legacy, preflight_turn_id='finished-native-turn')
        self.store.patch(self.jobs[1]['id'], status='blocked', detail='Need credentials from user')
        self.store.patch(self.jobs[2]['id'], status='paused', detail=legacy)
        self.store.migrate_idle()
        idle = self.store.get(self.jobs[0]['id'])
        self.assertEqual(idle['status'], 'idle')
        self.assertEqual(idle['idle_turn_id'], 'finished-native-turn')
        self.assertGreater(idle['next_run'], time.time())
        self.assertEqual(self.store.get(self.jobs[1]['id'])['status'], 'blocked')
        self.assertEqual(self.store.get(self.jobs[2]['id'])['status'], 'paused')
        self.assertFalse(self.calls())

    def test_legacy_system_error_migration_only_rearms_unowned_managed_observation(self):
        self.add(3)
        reason = 'Cannot confirm Codex runtime state: systemError'
        for job in self.jobs:
            self.store.patch(job['id'], managed=True, status='error', detail=reason)
        self.store.patch(self.jobs[1]['id'], managed_start_pending=True)
        self.store.patch(self.jobs[2]['id'], managed_turn_id='owned-turn')
        self.store.migrate_runtime_errors()
        job = self.store.get(self.jobs[0]['id'])
        self.assertEqual(job['status'], 'waiting')
        self.assertTrue(job['watch_external'])
        self.assertIsNone(job.get('approved_completed_turn'))
        self.assertEqual(self.store.get(self.jobs[1]['id'])['status'], 'error')
        self.assertEqual(self.store.get(self.jobs[2]['id'])['status'], 'error')
        for status, managed, detail in [('paused', True, reason), ('error', False, reason),
                                         ('error', True, 'Authentication failed')]:
            self.store.patch(self.jobs[0]['id'], status=status, managed=managed, detail=detail)
            self.store.migrate_runtime_errors()
            self.assertEqual(self.store.get(self.jobs[0]['id'])['status'], status)
        self.assertFalse(self.calls())


class CacheTests(unittest.TestCase):
    def test_different_accounts_do_not_block_one_another(self):
        cache, entered, release, finished = QuotaCache(), threading.Event(), threading.Event(), threading.Event()
        first = {'codex': 'codex', 'codex_home': '/a', 'limit_id': 'codex'}
        second = dict(first, codex_home='/b')
        def read(job):
            if job['codex_home'] == '/a':
                entered.set()
                self.assertTrue(release.wait(timeout=3))
            return {'account': job['codex_home']}
        values = []
        def load(job):
            values.append(cache.read(job))
            if job == second:
                finished.set()
        with patch('codex_runner.cli.read_limits', side_effect=read):
            a, b = threading.Thread(target=load, args=(first,)), threading.Thread(target=load, args=(second,))
            a.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                b.start()
                self.assertTrue(finished.wait(timeout=2))
            finally:
                release.set()
                a.join(timeout=4)
                if b.ident:
                    b.join(timeout=4)
        self.assertCountEqual(values, [{'account': '/a'}, {'account': '/b'}])

    def test_shutdown_cancels_a_worker_waiting_for_another_quota_lookup(self):
        cache, stopping = QuotaCache(), threading.Event()
        job = {'codex': 'codex', 'codex_home': '/a', 'limit_id': 'codex'}
        lock = cache.locks.get(('codex', '/a', 'codex'))
        lock.acquire()
        values = []
        with patch('codex_runner.cli.read_limits') as quota:
            thread = threading.Thread(target=lambda: values.append(cache.read(job, stopping=stopping)))
            thread.start()
            stopping.set()
            thread.join(timeout=2)
            lock.release()
            self.assertFalse(thread.is_alive())
            self.assertEqual(values, [None])
            quota.assert_not_called()


if __name__ == '__main__':
    unittest.main()
