import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from codex_runner.platforms import codex_command, default_state_dir, process_alive, child_options, no_console_options
from codex_runner.service import launchd_definition, windows_definition, windows_command, manage_service
from codex_runner.cli import RunnerError
from types import SimpleNamespace


class PlatformTests(unittest.TestCase):
    def test_liveness_probe_does_not_terminate_its_target(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        try:
            self.assertTrue(process_alive(child.pid))
            self.assertIsNone(child.poll())
            child.terminate(); child.wait(timeout=5)
            self.assertFalse(process_alive(child.pid))
        finally:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)

    def test_windows_npm_shim_uses_node_without_a_command_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            entry = base / 'node_modules/@openai/codex/bin/codex.js'
            entry.parent.mkdir(parents=True); entry.touch()
            (base / 'node.exe').touch()
            with patch('codex_runner.platforms.WINDOWS', True):
                command = codex_command(str(base / 'codex.cmd'), 'resume', 'quote & %PATH% $(test)')
            self.assertEqual(command, [str(base / 'node.exe'), str(entry), 'resume', 'quote & %PATH% $(test)'])

    def test_platform_state_defaults_and_override(self):
        with patch.dict(os.environ, {'CODEX_RUNNER_STATE_DIR': '/custom/state'}):
            self.assertEqual(default_state_dir(), '/custom/state')
        with patch.dict(os.environ, {'CODEX_RUNNER_STATE_DIR': '', 'LOCALAPPDATA': str(Path.home() / 'local')}), \
                patch('codex_runner.platforms.WINDOWS', True):
            self.assertEqual(Path(default_state_dir()).name, 'CodexRunner')

    def test_services_preserve_paths_and_run_as_current_user(self):
        base = Path.home() / '한글 project & tools'
        with patch('codex_runner.service.windows_command', return_value=['C:\\Program Files\\Runner\\codex-runner-background.exe', '--state-dir', str(base), 'service', 'run']):
            data = windows_definition(base, 'S-1-5-21-123')
        tree = ET.fromstring(data)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(tree.find('.//t:RunLevel', ns).text, 'LeastPrivilege')
        self.assertEqual(tree.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertEqual(tree.find('.//t:Command', ns).text, 'C:\\Program Files\\Runner\\codex-runner-background.exe')
        self.assertIn(str(base), tree.find('.//t:Arguments', ns).text)
        self.assertEqual(tree.find('.//t:ExecutionTimeLimit', ns).text, 'PT0S')
        payload = launchd_definition(base, 'test.runner')
        self.assertEqual(plistlib.loads(plistlib.dumps(payload))['ProgramArguments'], payload['ProgramArguments'])
        self.assertNotIn('UserName', payload)

    def test_windows_background_requires_full_zip_and_preserves_bot_command(self):
        with tempfile.TemporaryDirectory(prefix='한글 space ') as directory:
            base = Path(directory)
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(base/'codex-runner.exe')):
                with self.assertRaisesRegex(RunnerError, 'full Windows ZIP'):
                    windows_command(base)
                (base/'codex-runner-background.exe').touch()
                args = windows_command(base, 'discord')
                self.assertEqual(args, [str(base/'codex-runner-background.exe'), '--state-dir', str(base), 'discord', 'service', 'run'])

    def test_python_source_service_uses_pythonw_and_no_console_children(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base/'pythonw.exe').touch()
            with patch.object(sys, 'executable', str(base/'python.exe')), patch.object(sys, 'frozen', False, create=True):
                self.assertEqual(windows_command(base)[:3], [str(base/'pythonw.exe'), '-m', 'codex_runner.windows_background'])
        with patch('codex_runner.platforms.WINDOWS', True):
            self.assertEqual(no_console_options()['creationflags'], 0x08000000)
            self.assertEqual(child_options()['creationflags'], 0x08000200)

    def test_windows_registration_can_be_updated_without_starting_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SimpleNamespace(base=Path(directory))
            with patch('codex_runner.service.WINDOWS', True), patch('codex_runner.service.run') as run, \
                    patch('codex_runner.service.subprocess.check_output', return_value='"USER","S-1-5-21-123"'), \
                    patch('codex_runner.service.windows_definition', return_value=b'<Task/>'):
                manage_service(store, 'install', start=False)
                self.assertEqual(len(run.call_args_list), 1)
                self.assertEqual(run.call_args.args[:2], ('schtasks', '/Create'))
