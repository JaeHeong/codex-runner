"""Auth-free IPC smoke test: a private test home, no model or user session."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
from codex_runner.cli import resolve_codex
from codex_runner.platforms import codex_command, stop_process
from codex_runner.managed import LocalServer

with tempfile.TemporaryDirectory(prefix='cr-', dir=os.environ.get('CODEX_RUNNER_TEST_TMP')) as directory:
    home = Path(directory)
    env = dict(os.environ, CODEX_HOME=str(home))
    executable = resolve_codex()
    os.environ['CODEX_HOME'] = str(home)
    command = codex_command(executable, 'app-server', 'daemon')
    owned = None
    try:
        if os.name == 'nt':
            # GitHub's outer Windows job forbids detached descendants even for
            # a standard user. Test the same native socket/WebSocket transport
            # with a test-owned server that is always stopped before exit.
            owned = subprocess.Popen(codex_command(executable, 'app-server', '--listen', 'unix://'),
                                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            end = time.monotonic() + 30
            while not (home/'app-server-control/app-server-control.sock').exists():
                if owned.poll() is not None or time.monotonic() > end:
                    raise RuntimeError('Test-owned Windows socket server did not start')
                time.sleep(.1)
        else:
            subprocess.run(command + ['start'], env=env, check=True, timeout=120)
        server = LocalServer(home)
        try:
            result = server.rpc('thread/list', {'limit': 1})
            assert isinstance(result.get('data'), list)
            print('Native local WebSocket IPC OK; no model request made')
        finally:
            server.close()
    finally:
        if owned:
            stop_process(owned)  # Include the test server’s own code-host children.
        else:
            subprocess.run(command + ['stop'], env=env, timeout=60, check=False)
