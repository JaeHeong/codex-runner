import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from codex_runner.cli import (RunnerError, SCHEMA, Store, Worker, build_command,
                             read_limits, retry_at, select_limit)

FAKE = Path(__file__).with_name('fake_codex.py')


class QuotaTests(unittest.TestCase):
    def test_wait_for_all_exhausted_windows(self):
        s = {'primary': {'usedPercent': 100, 'resetsAt': 200},
             'secondary': {'usedPercent': 100, 'resetsAt': 900}}
        self.assertEqual(retry_at(s, now=100), 945)

    def test_available_weekly_window_does_not_extend_wait(self):
        s = {'primary': {'usedPercent': 100, 'resetsAt': 200},
             'secondary': {'usedPercent': 30, 'resetsAt': 900}}
        self.assertEqual(retry_at(s, now=100), 245)

    def test_past_reset_is_rechecked_with_grace(self):
        self.assertEqual(retry_at({'primary': {'usedPercent': 100, 'resetsAt': 90}}, now=100), 145)

    def test_unknown_limits_do_not_run_blindly(self):
        for s in [{}, {'rateLimitReachedType': 'unknown_future_limit'}]:
            with self.assertRaises(RunnerError):
                retry_at(s, now=100)

    def test_workspace_limits_are_polled_without_assuming_a_reset_time(self):
        for state in ['workspace_owner_credits_depleted', 'workspace_member_credits_depleted',
                      'workspace_owner_usage_limit_reached', 'workspace_member_usage_limit_reached']:
            self.assertEqual(retry_at({'rateLimitReachedType': state}, now=100), 400)

    def test_spend_controls_and_missing_reset_times_are_polled(self):
        for snapshot in [{'spendControlReached': True},
                         {'rateLimitReachedType': 'rate_limit_reached'},
                         {'primary': {'usedPercent': 100}}]:
            self.assertEqual(retry_at(snapshot, now=100), 400)

    def test_workspace_state_uses_nearer_quota_reset_or_poll(self):
        s = {'rateLimitReachedType': 'workspace_owner_credits_depleted',
             'primary': {'usedPercent': 100, 'resetsAt': 200}}
        self.assertEqual(retry_at(s, now=100), 245)
        s['primary']['resetsAt'] = 1000
        self.assertEqual(retry_at(s, now=100), 400)

    def test_no_extra_credits_does_not_block_included_allowance(self):
        self.assertIsNone(retry_at({'credits': {'hasCredits': False},
                                   'primary': {'usedPercent': 6},
                                   'secondary': {'usedPercent': 75},
                                   'rateLimitReachedType': None}, now=100))

    def test_only_requested_metered_bucket_is_used(self):
        s = {'limitId': 'codex', 'primary': {'usedPercent': 20}}
        self.assertIs(select_limit({'rateLimits': s}, 'codex'), s)
        with self.assertRaises(RunnerError):
            select_limit({'rateLimitsByLimitId': {'other': s}}, 'codex')


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.fixture = self.base / 'fake.json'
        self.fixture.write_text('{}')
        self.env = patch.dict(os.environ, {'FAKE_CODEX_STATE': str(self.fixture)})
        self.env.start()
        self.store = Store(self.base / 'state')
        self.job = dict(id=str(uuid.uuid4()), cwd=str(self.base), codex=str(FAKE),
                        path=os.environ['PATH'], codex_home=None, model=None, limit_id='codex',
                        sandbox='workspace-write', status='queued', next_run=0, turns=0)
        self.store.add(self.job)

    def tearDown(self):
        self.store.db.close()
        self.env.stop()
        self.temp.cleanup()

    def configure(self, **data):
        self.fixture.write_text(json.dumps(data))

    def test_success_resumes_exact_id_and_stops(self):
        Worker(self.store).run(self.job)
        job = self.store.get(self.job['id'])
        self.assertEqual(job['status'], 'complete')
        args = json.loads(self.fixture.with_suffix('.calls').read_text().splitlines()[0])
        self.assertEqual(args[-2], self.job['id'])
        self.assertIn('workspace-write', args)
        self.assertNotIn('--last', args)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.assertNotIn('sandbox_workspace_write.network_access=true', args)

    def test_network_option_enables_workspace_write_network(self):
        cmd = build_command(dict(self.job, network=True), 'schema.json', 'out.json')
        self.assertIn('sandbox_workspace_write.network_access=true', cmd)
        self.assertEqual(cmd[cmd.index('sandbox_workspace_write.network_access=true') - 1], '-c')
        self.assertNotIn('sandbox_workspace_write.network_access=true',
                         build_command(dict(self.job, network=True, sandbox='read-only'), 'schema.json', 'out.json'))

    def test_git_write_option_adds_git_writable_root(self):
        cmd = build_command(dict(self.job, git_write=True), 'schema.json', 'out.json')
        roots = [v for v in cmd if v.startswith('sandbox_workspace_write.writable_roots=')]
        self.assertEqual(roots, ['sandbox_workspace_write.writable_roots=' + json.dumps([str(self.base / '.git')])])
        self.assertEqual(cmd[cmd.index(roots[0]) - 1], '-c')

    def test_model_and_effort_are_passed_only_when_set(self):
        cmd = build_command(dict(self.job, model='gpt-test', effort='high'), 'schema.json', 'out.json')
        self.assertEqual(cmd[cmd.index('--model') + 1], 'gpt-test')
        self.assertEqual(cmd[cmd.index('model_reasoning_effort="high"') - 1], '-c')
        self.assertFalse(any('model_reasoning_effort' in v for v in build_command(self.job, 'schema.json', 'out.json')))

    def test_model_command_lists_and_validates_against_codex_models(self):
        from codex_runner.cli import read_models
        self.assertEqual([m['model'] for m in read_models(self.job)], ['gpt-test'])
        command = [sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base), 'model', self.job['id']]
        listed = subprocess.run(command + ['--list'], capture_output=True, text=True, timeout=20)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('low, medium*, high', listed.stdout)
        self.assertNotIn('gpt-hidden', listed.stdout)
        for args in (['--model', 'gpt-missing'], ['--effort', 'ultra']):
            rejected = subprocess.run(command + args, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(rejected.returncode, 0)
        changed = subprocess.run(command + ['--model', 'gpt-test', '--effort', 'high'], capture_output=True, text=True, timeout=20)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        job = self.store.get(self.job['id'])
        self.assertEqual((job['model'], job['effort'], job['status']), ('gpt-test', 'high', 'queued'))

    def test_network_and_git_write_options_require_workspace_write(self):
        from codex_runner.cli import cli
        for flag in ('--network', '--git-write'):
            opened = []
            def store(path):
                opened.append(Store(path))
                return opened[-1]
            try:
                with patch('codex_runner.cli.Store', side_effect=store), self.assertRaisesRegex(RunnerError, 'workspace-write'):
                    cli(['--state-dir', str(self.base / 'other'), 'add', str(uuid.uuid4()), '--cwd', str(self.base),
                         '--sandbox', 'read-only', flag, '--codex', str(FAKE)])
            finally:
                # Windows cannot remove the temporary directory while SQLite holds it open.
                for item in opened:
                    item.db.close()
            self.assertEqual(len(opened), 1)

    def test_exhausted_quota_makes_no_model_call(self):
        self.configure(used=100, reset=time.time() + 200)
        Worker(self.store).run(self.job)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'waiting')
        self.assertFalse(self.fixture.with_suffix('.calls').exists())

    def test_mid_run_exhaustion_then_reset_resumes_same_session(self):
        self.configure(mode='quota_error')
        Worker(self.store).run(self.job)
        job = self.store.get(self.job['id'])
        self.assertEqual(job['status'], 'waiting')
        self.configure(used=0)
        Worker(self.store).run(job)
        self.assertEqual(self.store.get(job['id'])['status'], 'complete')
        calls = [json.loads(s) for s in self.fixture.with_suffix('.calls').read_text().splitlines()]
        self.assertEqual([c[-2] for c in calls], [job['id'], job['id']])

    def test_workspace_credit_error_is_polled_then_resumes_after_clear(self):
        self.configure(mode='credit_error')
        Worker(self.store).run(self.job)
        job = self.store.get(self.job['id'])
        self.assertEqual(job['status'], 'waiting')
        self.assertIn('workspace_owner_credits_depleted', job['detail'])
        self.assertGreater(job['next_run'], time.time() + 250)
        self.configure(limit_type='workspace_owner_credits_depleted')
        Worker(self.store).run(job)
        self.assertEqual(len(self.fixture.with_suffix('.calls').read_text().splitlines()), 1)
        self.configure(used=6)
        Worker(self.store).run(self.store.get(job['id']))
        self.assertEqual(self.store.get(job['id'])['status'], 'complete')
        calls = [json.loads(s) for s in self.fixture.with_suffix('.calls').read_text().splitlines()]
        self.assertEqual([c[-2] for c in calls], [job['id'], job['id']])

    def test_non_quota_error_stops(self):
        self.configure(mode='error')
        Worker(self.store).run(self.job)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'error')

    def test_model_overload_waits_with_backoff_then_resumes(self):
        self.configure(mode='overloaded')
        before = time.time()
        Worker(self.store).run(self.job)
        job = self.store.get(self.job['id'])
        self.assertEqual(job['status'], 'waiting')
        self.assertIn('serverOverloaded', job['detail'])
        self.assertAlmostEqual(job['next_run'] - before, 300, delta=5)
        Worker(self.store).run(job)
        self.assertAlmostEqual(self.store.get(job['id'])['next_run'] - before, 600, delta=5)
        self.configure()
        Worker(self.store).run(self.store.get(job['id']))
        job = self.store.get(job['id'])
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['overload_retries'], 0)
        calls = [json.loads(s) for s in self.fixture.with_suffix('.calls').read_text().splitlines()]
        self.assertEqual([c[-2] for c in calls], [job['id']] * 3)

    def test_api_key_mode_does_not_fallback_to_billed_execution(self):
        self.configure(auth_type='apiKey')
        Worker(self.store).run(self.job)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'error')
        self.assertFalse(self.fixture.with_suffix('.calls').exists())

    def test_different_thread_is_rejected(self):
        self.configure(wrong_id=str(uuid.uuid4()))
        Worker(self.store).run(self.job)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'error')

    def test_continue_requeues_and_repeated_no_progress_blocks(self):
        self.configure(result='continue')
        for _ in range(3):
            Worker(self.store).run(self.store.get(self.job['id']))
        self.assertEqual(self.store.get(self.job['id'])['status'], 'blocked')

    def test_pause_prevents_launch_during_quota_lookup(self):
        def capacity(*_):
            self.store.patch(self.job['id'], pause_requested=True)
            return None
        worker = Worker(self.store)
        worker.capacity = capacity
        worker.run(self.job)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'paused')
        self.assertFalse(self.fixture.with_suffix('.calls').exists())

    def test_compare_and_set_preserves_pause(self):
        self.store.patch(self.job['id'], status='paused')
        self.store.patch(self.job['id'], expected=['running'], status='complete')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'paused')

    def test_live_child_pause_and_singleton_daemon(self):
        self.configure(mode='sleep')
        command = [sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base)]
        proc = subprocess.Popen(command + ['daemon'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.store.get(self.job['id']).get('pid'):
                    break
                time.sleep(.05)
            else:
                self.fail('Child was not started')
            duplicate = subprocess.run(command + ['daemon', '--once'], capture_output=True, timeout=5)
            self.assertNotEqual(duplicate.returncode, 0)
            subprocess.run(command + ['pause', self.job['id']], check=True, capture_output=True)
            deadline = time.time() + 10
            while time.time() < deadline:
                job = self.store.get(self.job['id'])
                if job['status'] == 'paused' and job.get('pid') is None:
                    break
                time.sleep(.05)
            else:
                self.fail('Pause did not stop the child')
        finally:
            (self.store.base / 'stop.request').touch()
            proc.wait(timeout=20); proc.stderr.close()

    def test_crash_recovery_restores_registration(self):
        self.store.patch(self.job['id'], status='running', pid=None)
        command = [sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base), 'daemon', '--once']
        subprocess.run(command, check=True, capture_output=True, timeout=15)
        self.assertEqual(self.store.get(self.job['id'])['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
