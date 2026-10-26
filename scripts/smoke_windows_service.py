"""Test only disposable Windows workers/tasks; no Codex login or model call."""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
cli = root/'dist/codex-runner.exe'
launcher = root/'dist/codex-runner-background.exe'

def wait_for(test, description, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test(): return
        time.sleep(.2)
    raise AssertionError('Timed out: ' + description)

with tempfile.TemporaryDirectory(prefix='runner 한글 & space ') as directory:
    base = Path(directory)
    # A GUI-subsystem wrapper must allocate no console in its console child,
    # forward quoted Unicode arguments without a shell, and wait for its exit.
    data = launcher.read_bytes()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    assert struct.unpack_from('<H', data, pe + 24 + 68)[0] == 2  # WINDOWS_GUI
    test_launcher = base/launcher.name
    shutil.copy2(launcher, test_launcher)
    shutil.copy2(root/'build/windows_console_probe.exe', base/'codex-runner.exe')
    output = base/'console.txt'
    arguments = ['한글 path & %PATH% $(text)', 'quote"and\\backslash', '']
    proc = subprocess.Popen([str(test_launcher), str(output), *arguments])
    try:
        wait_for(output.exists, 'console probe output')
        assert proc.poll() is None, 'supervisor exited before its child'
        assert proc.wait(timeout=10) == 37, 'child exit code was lost'
        assert output.read_text(encoding='utf-8-sig').splitlines() == ['console=0', *arguments]
    finally:
        if proc.poll() is None: proc.kill(); proc.wait(timeout=5)
    print('GUI launcher: no child console; Unicode/quoting, lifetime and exit code OK', flush=True)
    state = base/'state'
    pidfile = state/'daemon.pid'
    def command(*args):
        result = subprocess.run([str(cli), '--state-dir', str(state), *args],
                                capture_output=True, text=True, encoding='utf-8', timeout=90)
        print(result.stdout, end='', flush=True)
        if result.returncode: raise AssertionError(result.stderr or result.stdout)
        return result
    def launcher_stopped():
        try:
            with launcher.open('r+b'): return True
        except PermissionError:
            return False
    try:
        command('service', 'install', '--no-start')
        assert not pidfile.exists(), '--no-start launched a worker'
        xml = ET.fromstring((state/'service-task.xml').read_bytes())
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        assert Path(xml.find('.//t:Command', ns).text).name == launcher.name
        assert xml.find('.//t:LogonTrigger', ns) is not None
        command('service', 'start')
        wait_for(pidfile.exists, 'scheduled worker startup')
        first_pid = pidfile.read_text()
        command('service', 'stop')
        wait_for(lambda: not pidfile.exists(), 'graceful stop')
        wait_for(launcher_stopped, 'supervisor exit')
        command('service', 'start')
        wait_for(pidfile.exists, 'scheduled worker restart')
        assert first_pid != pidfile.read_text(), 'worker was not restarted'
        command('service', 'uninstall')
        wait_for(lambda: not pidfile.exists(), 'service uninstall')
        wait_for(launcher_stopped, 'supervisor uninstall exit')
    finally:
        # This command addresses only the hashed task for this disposable home.
        subprocess.run([str(cli), '--state-dir', str(state), 'service', 'uninstall'],
                       capture_output=True, timeout=90)
        time.sleep(1)  # Allow the test-only wrapper to reap its stopped child.
    print('Scheduled task install/start/stop/restart/uninstall OK; no model request made', flush=True)
