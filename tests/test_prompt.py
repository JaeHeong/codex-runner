import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from codex_runner.cli import PROMPT, RunnerError, Store, update_model, update_prompt


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state')
        self.sid = '01a151c8-b419-73f3-8fd2-119ba622ac45'
        self.job = dict(id=self.sid, prompt='old instructions', status='waiting',
                        next_run=9999999999, last_run=123, turns=4, pid=None, managed_turn_id=None)
        self.store.add(self.job)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base),
                               'prompt', self.sid, *args], capture_output=True, text=True, timeout=5)

    def test_reading_and_resetting_prompt_does_not_restart_waiting_work(self):
        self.assertEqual(self.command().stdout.strip(), 'old instructions')
        result = self.command('--default')
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.store.get(self.sid)
        self.assertEqual(after['prompt'], '이전에 하던 작업 이어서 해줘')
        self.assertEqual(after['prompt'], PROMPT)
        for key in ['status', 'next_run', 'last_run', 'turns', 'pid', 'managed_turn_id']:
            self.assertEqual(after[key], self.job[key])

    def test_exact_custom_text_and_utf8_file_replace_old_instructions(self):
        text = '최신 지시를 기준으로 이어서 진행해줘.\n검증 결과도 확인해줘.'
        self.assertEqual(self.command('--text', text).returncode, 0)
        self.assertEqual(self.store.get(self.sid)['prompt'], text)
        p = Path(self.temp.name) / 'prompt.txt'
        p.write_text('  파일 지시\n', encoding='utf-8')
        self.assertEqual(self.command('--file', str(p)).returncode, 0)
        self.assertEqual(self.store.get(self.sid)['prompt'], '파일 지시')

    def test_blank_prompt_is_rejected_and_does_not_replace_registration(self):
        with self.assertRaises(RunnerError):
            update_prompt(self.store, self.sid, ' \n')
        self.assertEqual(self.store.get(self.sid)['prompt'], 'old instructions')

    def test_model_and_effort_change_only_next_turn_settings(self):
        update_model(self.store, self.sid, effort='high')
        update_model(self.store, self.sid, model='gpt-test')
        after = self.store.get(self.sid)
        self.assertEqual((after['model'], after['effort']), ('gpt-test', 'high'))
        for key in ['status', 'next_run', 'last_run', 'turns', 'pid', 'managed_turn_id', 'prompt']:
            self.assertEqual(after[key], self.job[key])
        update_model(self.store, self.sid, reset=True)
        self.assertEqual((self.store.get(self.sid)['model'], self.store.get(self.sid)['effort']), (None, None))
        for bad in [dict(effort=' '), dict(reset=True, model='gpt-test')]:
            with self.assertRaises(RunnerError):
                update_model(self.store, self.sid, **bad)

    def test_editing_running_or_error_job_changes_only_next_message(self):
        for status in ['running', 'error', 'complete', 'paused']:
            self.store.patch(self.sid, status=status, managed_turn_id='existing-turn')
            update_prompt(self.store, self.sid, 'next message')
            after = self.store.get(self.sid)
            self.assertEqual(after['status'], status)
            self.assertEqual(after['managed_turn_id'], 'existing-turn')
            self.assertEqual(after['turns'], 4)


if __name__ == '__main__':
    unittest.main()
