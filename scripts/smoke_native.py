"""Auth-free IPC smoke test: a private test home, no model or user session."""
import os
from pathlib import Path
import subprocess
import tempfile
from codex_runner.cli import resolve_codex
from codex_runner.platforms import codex_command
from codex_runner.managed import LocalServer

with tempfile.TemporaryDirectory(prefix='cr-', dir=os.environ.get('CODEX_RUNNER_TEST_TMP')) as directory:
    home = Path(directory)
    env = dict(os.environ, CODEX_HOME=str(home))
    executable = resolve_codex()
    os.environ['CODEX_HOME'] = str(home)
    command = codex_command(executable, 'app-server', 'daemon')
    try:
        subprocess.run(command + ['start'], env=env, check=True, timeout=120)
        server = LocalServer(home)
        try:
            result = server.rpc('thread/list', {'limit': 1})
            assert isinstance(result.get('data'), list)
            print('Native shared-daemon WebSocket IPC OK; no model request made')
        finally:
            server.close()
    finally:
        subprocess.run(command + ['stop'], env=env, timeout=60, check=False)
