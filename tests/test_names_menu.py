import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile
import time
import unittest
if os.name != 'nt':
    import fcntl
    import pty
    import termios
from unittest.mock import patch

from codex_runner.cli import RunnerError, Store, default_name, select_session, validate_name
from codex_runner.menu import (browse_add, button_layout, cell_width, column_widths,
                               delete_registration, fitted, mouse_action, table_row, visible_status)

SID1 = '01a151c8-b419-73f3-8fd2-119ba622ac45'
SID2 = '01a159ab-763b-7bf0-8cfa-caf85e9ca6f4'


def wait_terminal(proc, master, timeout=10):
    # macOS PTYs have much smaller output buffers; drain repaint output while
    # waiting, otherwise curses blocks before it can process the queued Quit.
    end = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < end:
        if select.select([master], [], [], .05)[0]:
            try:
                os.read(master, 65536)
            except OSError:
                break
    return proc.wait(timeout=2)


class NamesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'state')
        for sid, name in [(SID1, 'tech'), (SID2, 'datadog')]:
            self.store.add(dict(id=sid, name=name, cwd=str(self.base / name), status='error',
                                next_run=123, last_run=456, turns=0, prompt='old', pid=None))

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def command(self, *args):
        return subprocess.run([sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base), *args],
                              text=True, capture_output=True, timeout=5)

    def test_names_full_ids_and_unique_prefixes_resolve_to_exact_id(self):
        self.assertEqual(self.store.resolve('TECH'), SID1)
        self.assertEqual(self.store.resolve(SID1), SID1)
        self.assertEqual(self.store.resolve('01a151'), SID1)
        with self.assertRaises(RunnerError):
            self.store.resolve('01a15')

    def test_preflight_checking_is_distinct_from_an_owned_running_turn(self):
        self.assertEqual(visible_status({'status': 'running', 'managed': True}), 'checking')
        self.assertEqual(visible_status({'status': 'running', 'managed': True, 'managed_turn_id': 'owned'}), 'running')
        self.assertEqual(visible_status({'status': 'waiting', 'managed': True}), 'waiting')

    def test_ambiguous_prefix_and_alias_prefix_conflict_are_rejected(self):
        self.store.set_name(SID2, '01a151')
        with self.assertRaisesRegex(RunnerError, 'Ambiguous'):
            self.store.resolve('01a151')
        self.assertEqual(self.store.get(SID1)['status'], 'error')
        self.store.set_name(SID2, 'datadog')
        self.store.add(dict(id='01a151c9-b419-73f3-8fd2-119ba622ac45', name='other', status='paused'))
        with self.assertRaisesRegex(RunnerError, 'Ambiguous'):
            self.store.resolve('01a151')

    def test_rename_and_collision_do_not_change_execution_state(self):
        before = self.store.get(SID1)
        self.assertEqual(self.command('alias', 'tech', 'platform').returncode, 0)
        self.assertEqual(self.store.resolve('platform'), SID1)
        after = self.store.get(SID1)
        for key in ['status', 'next_run', 'last_run', 'turns', 'pid']:
            self.assertEqual(after[key], before[key])
        with self.assertRaises(RunnerError):
            self.store.set_name(SID2, 'PLATFORM')
        self.assertEqual(self.store.get(SID2)['name'], 'datadog')

    def test_custom_names_and_dangerous_or_empty_names(self):
        self.assertEqual(validate_name('한글-프로젝트'), '한글-프로젝트')
        for name in ['', '-option', 'a/b', 'bad name', '\x1b[2J', SID1, 'x' * 49]:
            with self.assertRaises(RunnerError):
                validate_name(name)

    def test_auto_names_handle_same_project_and_shared_timestamp_prefix(self):
        self.assertEqual(default_name(self.store, '/work/project', SID1), 'project')
        self.store.set_name(SID1, 'project')
        candidate = default_name(self.store, '/work/project', SID2)
        self.assertEqual(candidate, 'project-01a159ab')
        self.store.set_name(SID2, candidate)
        candidate2 = default_name(self.store, '/work/project', '01a159ab-9999-7bf0-8cfa-caf85e9ca6f4')
        self.assertEqual(candidate2, 'project-01a159ab9999')

    def test_current_project_auto_selection_and_noninteractive_ambiguity(self):
        with patch('pathlib.Path.cwd', return_value=self.base / 'tech' / 'src'):
            self.assertEqual(select_session(self.store), SID1)
        with patch('pathlib.Path.cwd', return_value=self.base), patch('sys.stdin.isatty', return_value=False):
            with self.assertRaisesRegex(RunnerError, 'Specify'):
                select_session(self.store)

    def test_existing_commands_accept_names_without_real_model_calls(self):
        self.assertEqual(self.command('prompt', 'tech').stdout.strip(), 'old')
        self.assertEqual(self.command('prompt', 'tech', '--text', 'new').returncode, 0)
        self.assertEqual(self.command('resume', 'tech').returncode, 0)
        self.assertEqual(self.store.get(SID1)['status'], 'queued')
        self.assertEqual(self.command('pause', '01a151').returncode, 0)
        self.assertEqual(self.store.get(SID1)['status'], 'paused')
        self.assertEqual(self.command('logs', 'tech', '--local').returncode, 0)

    def test_status_json_keeps_full_id_and_name(self):
        data = json.loads(self.command('status', '--json').stdout)
        self.assertEqual({j['id'] for j in data}, {SID1, SID2})
        self.assertEqual({j['name'] for j in data}, {'tech', 'datadog'})

    def test_mouse_hit_testing_selects_rows_and_requires_action_button(self):
        rows, buttons = [(0, 4), (1, 5)], [('resume', 11, 21, 19)]
        self.assertEqual(mouse_action(12, 5, rows, buttons), ('select', 1))
        self.assertEqual(mouse_action(12, 19, rows, buttons), ('action', 'resume'))
        self.assertEqual(mouse_action(12, 5, rows, buttons, True), ('open', 1))
        self.assertIsNone(mouse_action(30, 19, rows, buttons))

    def test_unicode_labels_respect_terminal_cell_width(self):
        self.assertEqual(fitted('한글abcd', 5), '한글a')
        self.assertEqual(fitted('\x1b[31mhello', 3), 'hel')

    def test_prompt_column_gets_extra_width_and_buttons_remain_clickable(self):
        small, large = column_widths(60), column_widths(180)
        self.assertGreater(large[-1], small[-1] + 80)
        row = table_row(['한글', 'waiting', SID1[:12], '/project', '이전에 하던 작업 이어서 해줘 ' * 20], large)
        self.assertEqual(cell_width(row), 180)
        self.assertTrue(row.rstrip().endswith('…'))
        buttons = button_layout(64, 19)
        self.assertTrue(any(action == 'name' for action, _, _, _ in buttons))
        self.assertTrue(any(action == 'add' for action, _, _, _ in buttons))
        self.assertTrue(any(action == 'delete' for action, _, _, _ in buttons))
        self.assertLessEqual(max(y for _, _, _, y in buttons), 20)
        self.assertTrue(all(x + len(text) <= 62 for _, text, x, _ in buttons))

    def test_model_picker_offers_codex_models_and_their_efforts(self):
        from codex_runner.menu import choose_model, model_text
        models = [{'model': 'gpt-a', 'isDefault': True, 'defaultReasoningEffort': 'medium',
                   'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'medium'}]},
                  {'model': 'gpt-b', 'defaultReasoningEffort': 'high',
                   'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}]}]
        shown = []
        def pick(title, choices):
            shown.append([c['key'] for c in choices if isinstance(c['key'], str)])
            return next(c for c in choices if c['key'] == ('gpt-b' if len(shown) == 1 else 'max'))
        with patch('codex_runner.menu.read_models', return_value=models), patch('codex_runner.menu.choose', side_effect=pick):
            choose_model(self.store, SID1)
        self.assertEqual(shown, [['gpt-a', 'gpt-b'], ['high', 'max']])
        job = self.store.get(SID1)
        self.assertEqual((job['model'], job['effort'], job['status']), ('gpt-b', 'max', 'error'))
        self.assertEqual(model_text(job), 'gpt-b / max')
        with patch('codex_runner.menu.read_models', return_value=models), \
             patch('codex_runner.menu.choose', side_effect=lambda title, choices: choices[-1]):
            choose_model(self.store, SID1)
        self.assertEqual(model_text(self.store.get(SID1)), '세션 설정')

    def test_add_browser_registers_selected_history_paused(self):
        folder = self.base / 'new-project'; folder.mkdir()
        thread = {'id': '01a151c9-b419-73f3-8fd2-119ba622ac45', 'cwd': str(folder), 'name': '테스트 작업', 'updatedAt': 123}
        environment = {'codex': '/fake/codex', 'codex_home': str(self.base), 'path': os.environ['PATH']}
        with patch('codex_runner.catalog.load_sessions', return_value=[thread]), patch('codex_runner.catalog.probe_codex'), \
             patch('codex_runner.menu.choose', side_effect=[{'key': str(folder)}, {'key': thread}, {'key': 'workspace-write'}]), \
             patch('builtins.input', return_value='build'):
            sid = browse_add(self.store, environment)
        self.assertEqual(self.store.get(sid)['status'], 'paused')
        self.assertEqual(self.store.get(sid)['name'], 'build')
        self.assertEqual(self.store.get(sid)['session_title'], '테스트 작업')

    def test_delete_only_unregisters_and_cancel_preserves_state(self):
        path = self.base / 'keep-history.jsonl'; path.write_text('keep')
        with patch('codex_runner.menu.choose', return_value=None):
            delete_registration(self.store, SID1)
        self.assertEqual(self.store.get(SID1)['status'], 'error')
        with patch('codex_runner.menu.choose', return_value={'key': 'delete'}):
            delete_registration(self.store, SID1)
        with self.assertRaises(RunnerError):
            self.store.get(SID1)
        self.assertEqual(path.read_text(), 'keep')
        self.assertEqual(self.store.get(SID2)['status'], 'error')

    @unittest.skipIf(os.name == 'nt', 'POSIX terminal integration')
    def test_name_edit_keeps_selected_session_after_sort_order_changes(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 120, 0, 0))
        proc = subprocess.Popen([sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base)],
                                stdin=slave, stdout=slave, stderr=slave, env=dict(os.environ, TERM='xterm-256color'), close_fds=True)
        os.close(slave)
        received = b''
        def until(needle, seconds=5):
            nonlocal received
            end = time.monotonic() + seconds
            while needle not in received and time.monotonic() < end:
                if select.select([master], [], [], .1)[0]:
                    received += os.read(master, 65536)
            self.assertIn(needle, received)
        try:
            until(b'PROMPT')
            os.write(master, b'n')
            until('새 이름'.encode())
            received = b''; os.write(master, b'zzz\n')
            until(b'Codex Runner')
            self.assertEqual(self.store.get(SID2)['name'], 'zzz')
            os.write(master, b'e'); until('새 문구'.encode())
            received = b''; os.write(master, b'edited\n')
            until(b'Codex Runner')
            os.write(master, b'q'); self.assertEqual(wait_terminal(proc, master), 0)
            self.assertEqual(self.store.get(SID2)['prompt'], 'edited')
            self.assertEqual(self.store.get(SID1)['prompt'], 'old')
            for sid in (SID1, SID2):
                self.assertEqual(self.store.get(sid)['status'], 'error')
                self.assertEqual(self.store.get(sid)['next_run'], 123)
        finally:
            if proc.poll() is None:
                proc.terminate(); wait_terminal(proc, master)
            os.close(master)

    @unittest.skipIf(os.name == 'nt', 'POSIX terminal integration')
    def test_menu_open_and_quit_in_real_terminal_do_not_start_work(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        env = dict(os.environ, TERM='xterm-256color')
        cmd = [sys.executable, '-m', 'codex_runner.cli', '--state-dir', str(self.store.base)]
        proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
        os.close(slave)
        received = b''
        try:
            deadline = time.monotonic() + 5
            while b'Codex Runner' not in received and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    received += os.read(master, 65536)
            self.assertIn(b'Codex Runner', received)
            os.write(master, b'q')
            self.assertEqual(wait_terminal(proc, master), 0)
            self.assertEqual(self.store.get(SID1)['status'], 'error')
            self.assertEqual(self.store.get(SID2)['status'], 'error')
            self.assertEqual(self.store.get(SID1)['next_run'], 123)
        finally:
            if proc.poll() is None:
                proc.terminate(); wait_terminal(proc, master)
            os.close(master)


if __name__ == '__main__':
    unittest.main()
