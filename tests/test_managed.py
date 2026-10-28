import json
import copy
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_runner.cli import RunnerError, Store, Worker, control_job
from codex_runner.managed import LocalServer


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state')
        self.job = dict(id='01a151c8-b419-73f3-8fd2-119ba622ac45', cwd=self.temp.name,
                        codex='unused', path=os.environ['PATH'], codex_home=None,
                        model=None, limit_id='codex', sandbox='danger-full-access',
                        managed=True, status='queued', next_run=0, turns=0, prompt='authorized task')
        self.store.add(self.job)
        self.calls = []
        self.thread_status = 'idle'
        self.latest_turn = {'id': 'prior-turn', 'status': 'interrupted', 'items': []}
        self.turn = {'id': 'turn-owned', 'status': 'completed',
                     'items': [{'type': 'agentMessage', 'phase': 'final_answer',
                                'text': json.dumps({'status': 'complete', 'summary': 'verified'})}]}
        self.server = type('FakeServer', (), {})()
        self.server.rpc = self.rpc
        self.server.close = lambda: None
        self.server.event = self.event
        self.worker = Worker(self.store)
        self.worker.capacity = lambda *a, **kw: None
        self.mock = patch('codex_runner.managed.LocalServer', return_value=self.server)
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.store.db.close()
        self.temp.cleanup()

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/resume':
            return {'thread': {'id': self.job['id'], 'cwd': self.job['cwd'],
                               'status': {'type': self.thread_status}}}
        if method == 'thread/read':
            return {'thread': {'id': self.job['id'], 'cwd': self.job['cwd'],
                               'status': {'type': self.thread_status}}}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-owned', 'status': 'inProgress'}}
        if method == 'thread/turns/list':
            if params.get('limit') == 1:
                return {'data': [self.latest_turn], 'nextCursor': None}
            return {'data': [self.turn], 'nextCursor': None}
        if method == 'turn/interrupt':
            self.turn.update(status='interrupted')
            return {}
        raise AssertionError(method)

    def event(self):
        self.latest_turn = copy.deepcopy(self.turn)
        return {'method': 'turn/completed', 'params': {'threadId': self.job['id'], 'turn': self.turn}}

    def run_job(self):
        self.worker.run(self.store.get(self.job['id']))
        return self.store.get(self.job['id'])

    def test_existing_server_starts_exact_thread_and_stops_when_complete(self):
        job = self.run_job()
        self.assertEqual(job['status'], 'complete')
        starts = [p for m, p in self.calls if m == 'turn/start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['threadId'], self.job['id'])
        self.assertEqual(starts[0]['input'][0]['text'], 'authorized task')
        self.assertEqual(starts[0]['sandboxPolicy'], {'type': 'dangerFullAccess'})
        self.assertEqual(starts[0]['approvalPolicy'], 'never')
        self.assertNotIn('model', starts[0])
        self.assertNotIn('effort', starts[0])
        self.assertIsNone(job.get('managed_turn_id'))
        self.assertEqual(job['turns'], 1)

    def test_model_and_effort_overrides_are_sent_when_set(self):
        self.store.patch(self.job['id'], model='gpt-test', effort='high')
        self.run_job()
        start = [p for m, p in self.calls if m == 'turn/start'][0]
        self.assertEqual((start['model'], start['effort']), ('gpt-test', 'high'))

    def test_workspace_write_network_is_opt_in(self):
        from codex_runner.managed import sandbox_policy
        job = dict(self.job, sandbox='workspace-write')
        self.assertFalse(sandbox_policy(job)['networkAccess'])
        policy = sandbox_policy(dict(job, network=True))
        self.assertEqual(policy, {'type': 'workspaceWrite', 'writableRoots': [self.job['cwd']], 'networkAccess': True})

    def test_git_write_adds_git_directory_as_writable_root(self):
        from codex_runner.managed import sandbox_policy
        policy = sandbox_policy(dict(self.job, sandbox='workspace-write', git_write=True))
        self.assertEqual(policy['writableRoots'], [self.job['cwd'], str(Path(self.job['cwd']) / '.git')])
        self.assertFalse(policy['networkAccess'])

    def test_windows_restricted_launcher_waits_for_desktop_daemon_without_sending_input(self):
        with patch('codex_runner.managed.LocalServer', side_effect=FileNotFoundError()), \
                patch('codex_runner.managed.WINDOWS', True), \
                patch('codex_runner.managed.subprocess.run') as start:
            start.return_value.returncode = 1
            start.return_value.stderr = b'this Windows launcher prevents background processes from outliving it'
            job = self.run_job()
        self.assertEqual(job['status'], 'waiting')
        self.assertTrue(job['watch_external'])
        self.assertIn('normal terminal', job['detail'])
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))
        self.thread_status = 'active'
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_completed_task_is_detected_without_sending_a_message(self):
        self.latest_turn = copy.deepcopy(self.turn)
        job = self.run_job()
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['turns'], 0)
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_plain_successful_turn_stays_idle_without_sending_a_message(self):
        self.latest_turn = {'id': 'native', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Tests still need checking.'}]}
        self.assertEqual(self.run_job()['status'], 'idle')
        self.assertEqual(self.run_job()['status'], 'idle')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_idle_observer_follows_new_work_and_recovers_only_after_quota_failure(self):
        self.latest_turn = {'id': 'native-done', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Answered.'}]}
        self.assertEqual(self.run_job()['status'], 'idle')
        self.thread_status = 'active'
        self.latest_turn = {'id': 'new-native-task', 'status': 'inProgress', 'items': []}
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.thread_status = 'idle'
        self.latest_turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        self.worker.capacity = lambda *a, **kw: 9999999999
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))
        self.worker.capacity = lambda *a, **kw: None
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def test_idle_observer_does_not_retry_escape_or_nonquota_errors(self):
        self.store.patch(self.job['id'], status='idle', watch_external=True)
        for status in ('interrupted', 'failed'):
            self.latest_turn = {'id': 'native-' + status, 'status': status,
                                'items': [], 'error': {'codexErrorInfo': 'other'}}
            self.assertEqual(self.run_job()['status'], 'idle')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))
        control_job(self.store, self.job['id'], 'resume')
        self.assertEqual(self.run_job()['status'], 'complete')

    def test_manual_resume_of_a_finished_native_response_arms_idle_by_default(self):
        self.latest_turn = {'id': 'native-done', 'status': 'completed', 'items': [
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Answered.'}]}
        control_job(self.store, self.job['id'], 'resume')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'idle')
        self.assertTrue(self.store.get(self.job['id'])['watch_external'])
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))
        control_job(self.store, self.job['id'], 'resume', continue_completed=True)
        self.assertEqual(self.run_job()['status'], 'complete')

    def test_structured_input_blocker_is_not_reclassified_as_idle(self):
        self.latest_turn = copy.deepcopy(self.turn)
        self.latest_turn['items'][0]['text'] = json.dumps({'status': 'blocked', 'summary': 'Need credentials from user'})
        self.assertEqual(self.run_job()['status'], 'blocked')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_manual_resume_checks_completion_and_explicit_override_is_turn_scoped(self):
        self.latest_turn = copy.deepcopy(self.turn)
        self.store.patch(self.job['id'], status='paused')
        with self.assertRaisesRegex(RunnerError, 'No message sent'):
            control_job(self.store, self.job['id'], 'resume')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'complete')
        control_job(self.store, self.job['id'], 'resume', continue_completed=True)
        self.assertEqual(self.store.get(self.job['id'])['approved_completed_turn'], 'turn-owned')
        self.latest_turn['id'] = 'newly-finished-native-turn'
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_manual_resume_cannot_requeue_worker_that_started_during_check(self):
        normal = self.rpc
        def rpc(method, params):
            if method == 'thread/read':
                self.store.patch(self.job['id'], status='running', managed_turn_id='owned-by-worker')
            return normal(method, params)
        self.server.rpc = rpc
        self.store.patch(self.job['id'], status='paused')
        with self.assertRaisesRegex(RunnerError, 'running state was preserved'):
            control_job(self.store, self.job['id'], 'resume')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'running')
        self.assertEqual(self.store.get(self.job['id'])['managed_turn_id'], 'owned-by-worker')

    def test_completed_continue_result_can_proceed(self):
        self.latest_turn = copy.deepcopy(self.turn)
        self.latest_turn['items'][0]['text'] = json.dumps({'status': 'continue', 'summary': 'Tests remain'})
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def test_idle_runtime_with_in_progress_turn_does_not_send_input(self):
        self.latest_turn['status'] = 'inProgress'
        self.assertEqual(self.run_job()['status'], 'blocked')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_turn_starts_between_metadata_reads_is_rechecked(self):
        self.latest_turn['status'] = 'inProgress'
        normal = self.rpc
        reads = []
        def rpc(method, params):
            if method == 'thread/read':
                reads.append(1)
                if len(reads) > 1:
                    self.thread_status = 'active'
            return normal(method, params)
        self.server.rpc = rpc
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_unknown_or_system_error_runtime_fails_closed(self):
        self.thread_status = 'systemError'
        self.assertEqual(self.run_job()['status'], 'error')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_quota_system_error_waits_then_recovers_once_without_runtime_restart(self):
        self.store.patch(self.job['id'], watch_external=True)
        self.thread_status = 'systemError'
        self.latest_turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        self.worker.capacity = lambda *a, **kw: 9999999999
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))
        self.worker.capacity = lambda *a, **kw: None
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def test_system_error_requires_latest_quota_failure_not_an_old_or_unrelated_error(self):
        self.thread_status = 'systemError'
        for status, error in [('completed', {'codexErrorInfo': 'usageLimitExceeded'}),
                              ('failed', {'codexErrorInfo': 'other'})]:
            self.store.patch(self.job['id'], status='queued', watch_external=True)
            self.latest_turn.update(status=status, error=error)
            self.assertEqual(self.run_job()['status'], 'error')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))

    def test_terminal_starts_during_system_error_quota_recovery_is_only_observed(self):
        self.thread_status = 'systemError'
        self.latest_turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        def capacity(*a, **kw):
            self.thread_status = 'active'
            self.latest_turn.update(status='inProgress', error=None)
        self.worker.capacity = capacity
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))

    def test_manual_resume_of_quota_system_error_does_not_send_a_message(self):
        self.thread_status = 'systemError'
        self.latest_turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        self.store.patch(self.job['id'], status='error')
        control_job(self.store, self.job['id'], 'resume')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'queued')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))

    def test_terminal_starts_work_during_quota_check_no_input_is_sent(self):
        def capacity(*a, **kw):
            self.thread_status = 'active'
        self.worker.capacity = capacity
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m in ('turn/start', 'turn/interrupt') for m, p in self.calls))

    def test_terminal_completes_task_during_quota_check_no_input_is_sent(self):
        def capacity(*a, **kw):
            self.latest_turn = copy.deepcopy(self.turn)
        self.worker.capacity = capacity
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_background_shell_process_is_not_automatically_busy(self):
        self.latest_turn['items'] = [{'type': 'commandExecution', 'command': 'tail -f app.log',
                                      'status': 'inProgress', 'processId': 'monitor'}]
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def linked_agent(self, state='active', parent=None):
        self.latest_turn = copy.deepcopy(self.turn)
        self.latest_turn['items'].append({'type': 'collabAgentToolCall', 'senderThreadId': self.job['id'],
            'receiverThreadIds': ['child'], 'agentsStates': {'child': {'status': 'running'}}})
        normal = self.rpc
        def rpc(method, params):
            if method == 'thread/read' and params['threadId'] == 'child':
                return {'thread': {'id': 'child', 'parentThreadId': parent or self.job['id'], 'status': {'type': state}}}
            if method == 'thread/turns/list' and params['threadId'] == 'child':
                return {'data': [{'id': 'child-turn', 'status': 'completed', 'items': []}]}
            return normal(method, params)
        self.server.rpc = rpc

    def test_linked_active_agent_keeps_parent_waiting_even_when_parent_is_idle(self):
        self.linked_agent()
        job = self.run_job()
        self.assertEqual(job['status'], 'waiting')
        self.assertIn('Linked agent', job['detail'])
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_stale_agent_snapshot_does_not_keep_finished_child_waiting(self):
        self.linked_agent(state='idle')
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_finished_owned_parent_waits_for_child_without_resubmitting(self):
        self.linked_agent(state='active')
        self.turn = copy.deepcopy(self.latest_turn)
        self.latest_turn = {'id': 'prior-turn', 'status': 'interrupted', 'items': []}
        waiting = self.run_job()
        self.assertEqual(waiting['status'], 'waiting')
        self.assertIsNone(waiting['managed_turn_id'])
        self.assertEqual(waiting['turns'], 1)
        self.linked_agent(state='idle')
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def test_unrelated_referenced_agent_is_not_treated_as_child_work(self):
        self.linked_agent(parent='different-project-thread')
        self.assertEqual(self.run_job()['status'], 'complete')

    def test_an_existing_interactive_turn_is_not_interrupted_or_duplicated(self):
        self.thread_status = 'active'
        waiting = self.run_job()
        self.assertEqual(waiting['status'], 'waiting')
        self.assertGreater(waiting['next_run'], 0)
        self.assertFalse(any(m in ['turn/start', 'turn/interrupt'] for m, p in self.calls))
        self.thread_status = 'idle'
        self.assertEqual(self.run_job()['status'], 'idle')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_failed_managed_turn_recovers_from_workspace_credit_state(self):
        from codex_runner.cli import retry_at
        self.turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        self.worker.capacity = lambda *a, **kw: retry_at({'rateLimitReachedType': 'workspace_owner_credits_depleted'}) if kw.get('refresh') else None
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertIsNone(self.store.get(self.job['id']).get('managed_turn_id'))
        self.turn.update(status='completed')
        self.worker.capacity = lambda *a, **kw: None
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 2)

    def test_usage_limit_error_is_polled_when_snapshot_has_not_caught_up(self):
        self.turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        job = self.run_job()
        self.assertEqual(job['status'], 'waiting')
        self.assertIn('usageLimitExceeded', job['detail'])
        self.assertIsNone(job.get('managed_turn_id'))

    def test_refused_turn_after_quota_wait_does_not_use_a_resume(self):
        self.store.patch(self.job['id'], max_quota_resumes=1, quota_waiting=True)
        self.turn.update(status='failed', error={'codexErrorInfo': 'usageLimitExceeded'})
        job = self.run_job()
        self.assertEqual((job['status'], job['quota_resumes'], job['quota_waiting']), ('waiting', 0, True))
        self.turn.update(status='completed')
        job = self.run_job()
        self.assertEqual((job['status'], job['quota_resumes']), ('complete', 1))

    def test_server_overload_waits_with_backoff_then_recovers(self):
        self.turn.update(status='failed', error={'codexErrorInfo': 'serverOverloaded'})
        before = time.time()
        job = self.run_job()
        self.assertEqual(job['status'], 'waiting')
        self.assertIn('serverOverloaded', job['detail'])
        self.assertAlmostEqual(job['next_run'] - before, 300, delta=5)
        self.store.patch(self.job['id'], status='queued')
        self.assertAlmostEqual(self.run_job()['next_run'] - before, 600, delta=5)
        self.store.patch(self.job['id'], status='queued')
        self.turn.update(status='completed', error=None)
        job = self.run_job()
        self.assertEqual(job['status'], 'complete')
        self.assertEqual(job['overload_retries'], 0)

    def test_system_error_after_server_overload_resumes(self):
        self.thread_status = 'systemError'
        self.latest_turn.update(status='failed', error={'codexErrorInfo': 'serverOverloaded'})
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual(len([m for m, p in self.calls if m == 'turn/start']), 1)

    def test_ordinary_turn_failure_still_stops_for_review(self):
        self.turn.update(status='failed', error={'codexErrorInfo': 'other', 'message': 'broken config'})
        self.assertEqual(self.run_job()['status'], 'error')

    def test_quota_wait_does_not_start_a_turn(self):
        self.worker.capacity = lambda *a, **kw: 9999999999
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_failed_turn_waits_only_on_confirmed_quota_then_resumes_same_id(self):
        self.turn.update(status='failed', error={'message': 'quota'})
        self.worker.capacity = lambda *a, **kw: 9999999999 if kw.get('refresh') else None
        self.assertEqual(self.run_job()['status'], 'waiting')
        self.worker.capacity = lambda *a, **kw: None
        self.turn.update(status='completed')
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertEqual([p['threadId'] for m, p in self.calls if m == 'turn/start'], [self.job['id']] * 2)

    def test_restart_reattaches_owned_turn_without_duplicate_model_call(self):
        self.thread_status = 'active'
        self.store.patch(self.job['id'], managed_turn_id='turn-owned')
        self.assertEqual(self.run_job()['status'], 'complete')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_lost_start_response_is_never_replayed(self):
        normal = self.rpc
        def lost(method, params):
            if method == 'turn/start':
                raise RunnerError('connection lost')
            return normal(method, params)
        self.server.rpc = lost
        self.assertEqual(self.run_job()['status'], 'error')
        self.assertTrue(self.store.get(self.job['id'])['managed_start_pending'])
        self.server.rpc = normal
        self.store.patch(self.job['id'], status='queued')
        self.calls.clear()
        self.assertEqual(self.run_job()['status'], 'error')
        self.assertFalse(any(m == 'turn/start' for m, p in self.calls))

    def test_pause_cancels_only_the_owned_turn_and_waits_for_confirmation(self):
        normal = self.rpc
        def pause_after_start(method, params):
            r = normal(method, params)
            if method == 'turn/start':
                self.store.patch(self.job['id'], pause_requested=True)
            return r
        self.server.rpc = pause_after_start
        self.assertEqual(self.run_job()['status'], 'paused')
        interrupts = [p for m, p in self.calls if m == 'turn/interrupt']
        self.assertEqual(interrupts, [{'threadId': self.job['id'], 'turnId': 'turn-owned'}])
        self.assertIsNone(self.store.get(self.job['id']).get('managed_turn_id'))

    def test_pause_connection_failure_is_not_reported_as_success(self):
        normal = self.rpc
        def failed_pause(method, params):
            if method == 'turn/interrupt':
                raise RunnerError('server unavailable')
            r = normal(method, params)
            if method == 'turn/start':
                self.store.patch(self.job['id'], pause_requested=True)
            return r
        self.server.rpc = failed_pause
        job = self.run_job()
        self.assertEqual(job['status'], 'error')
        self.assertEqual(job['managed_turn_id'], 'turn-owned')
        self.assertIn('not confirmed', job['detail'])

    def test_daemon_restart_does_not_cancel_work_or_lose_ownership(self):
        normal = self.rpc
        def stopping(method, params):
            r = normal(method, params)
            if method == 'turn/start':
                self.worker.stopping.set()
            return r
        self.server.rpc = stopping
        job = self.run_job()
        self.assertEqual(job['status'], 'queued')
        self.assertEqual(job['managed_turn_id'], 'turn-owned')
        self.assertFalse(any(m == 'turn/interrupt' for m, p in self.calls))


class TransportTests(unittest.TestCase):
    def test_incomplete_frame_survives_timeout_without_losing_header(self):
        client, peer = socket.socketpair()
        connection = object.__new__(LocalServer)
        connection.sock = client
        connection.buffer = b''
        try:
            peer.sendall(b'\x81\x0b{"id":')
            with self.assertRaises(TimeoutError):
                connection.receive(.01)
            peer.sendall(b'1234}')
            self.assertEqual(connection.receive(.1), {'id': 1234})
        finally:
            client.close(); peer.close()


if __name__ == '__main__':
    unittest.main()
