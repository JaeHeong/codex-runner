"""Platform primitives; never use Windows kill(pid, 0) as a liveness probe."""
import contextlib
import os
import platform
from pathlib import Path
import shutil
import signal
import subprocess
import sys

WINDOWS = os.name == 'nt'


def default_state_dir():
    if os.environ.get('CODEX_RUNNER_STATE_DIR'):
        return os.environ['CODEX_RUNNER_STATE_DIR']
    if WINDOWS:
        return str(Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'CodexRunner')
    if sys.platform == 'darwin':
        return str(Path.home() / 'Library/Application Support/CodexRunner')
    return str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'codex-runner')


def lock_file(handle):
    if WINDOWS:
        import msvcrt
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'\0'); handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError('Daemon state is already locked') from exc
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def process_alive(pid):
    if not pid or pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def child_options():
    return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {'start_new_session': True}


def stop_process(proc):
    if proc.poll() is not None:
        return
    if WINDOWS:
        # Only the subprocess created by Runner, never the shared Codex daemon.
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        proc.wait(timeout=10)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def codex_command(executable, *args):
    path = Path(executable)
    if path.suffix.lower() == '.py':
        return [sys.executable, str(path), *args]
    if WINDOWS and path.suffix.lower() in ('.cmd', '.bat'):
        # Invoke npm's JS entry point directly, avoiding cmd.exe prompt escaping.
        arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
        triple = 'aarch64-pc-windows-msvc' if arch == 'arm64' else 'x86_64-pc-windows-msvc'
        modules = path.parent / 'node_modules/@openai'
        # Prefer the actual native process so closing a metadata/proxy child
        # never leaves an npm launcher descendant holding the socket open.
        for vendor in [modules / ('codex-win32-' + arch) / 'vendor',
                       modules / 'codex/node_modules/@openai' / ('codex-win32-' + arch) / 'vendor',
                       modules / 'codex/vendor']:
            native = vendor / triple / 'bin/codex.exe'
            if native.is_file():
                return [str(native), *args]
        entry = path.parent / 'node_modules/@openai/codex/bin/codex.js'
        node = path.parent / 'node.exe'
        node = str(node) if node.is_file() else shutil.which('node')
        if not entry.is_file() or not node:
            raise OSError('Use --codex with codex.exe, or install Codex with npm and Node.js on PATH')
        return [node, str(entry), *args]
    return [str(path), *args]
