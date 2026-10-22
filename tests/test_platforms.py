import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from codex_runner.platforms import codex_command, default_state_dir, process_alive
from codex_runner.service import launchd_definition, windows_definition


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
        with patch('codex_runner.service.command', return_value=['C:\\Program Files\\Runner\\codex-runner.exe', '--state-dir', str(base), 'service', 'run']):
            data = windows_definition(base, 'S-1-5-21-123')
        tree = ET.fromstring(data)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(tree.find('.//t:RunLevel', ns).text, 'LeastPrivilege')
        self.assertEqual(tree.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertEqual(tree.find('.//t:Command', ns).text, 'C:\\Program Files\\Runner\\codex-runner.exe')
        self.assertIn(str(base), tree.find('.//t:Arguments', ns).text)
        self.assertEqual(tree.find('.//t:ExecutionTimeLimit', ns).text, 'PT0S')
        payload = launchd_definition(base, 'test.runner')
        self.assertEqual(plistlib.loads(plistlib.dumps(payload))['ProgramArguments'], payload['ProgramArguments'])
        self.assertNotIn('UserName', payload)
