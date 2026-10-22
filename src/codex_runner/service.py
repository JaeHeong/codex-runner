"""Per-user systemd, launchd and Task Scheduler lifecycle management."""
import contextlib
import csv
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from .cli import RunnerError, daemon
from .platforms import WINDOWS, process_alive


def command(base, action='daemon'):
    entry = shutil.which('codex-runner')
    if getattr(sys, 'frozen', False):
        # Preserve a stable Homebrew/WinGet link across package upgrades.
        prefix = [entry if entry and os.path.samefile(entry, sys.executable) else sys.executable]
    else:
        prefix = [entry] if entry else [sys.executable, '-m', 'codex_runner.cli']
    return prefix + ['--state-dir', str(base), *action.split()]


def launchd_definition(base, label):
    return {'Label': label, 'ProgramArguments': command(base), 'RunAtLoad': True,
            'KeepAlive': True, 'ThrottleInterval': 15, 'ExitTimeOut': 45,
            'EnvironmentVariables': {'PATH': os.environ.get('PATH', ''),
                                     'CODEX_HOME': os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))},
            'StandardOutPath': str(base / 'service.log'), 'StandardErrorPath': str(base / 'service.log')}


def windows_definition(base, user):
    root = ET.Element('Task', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task', version='1.2')
    trigger = ET.SubElement(ET.SubElement(root, 'Triggers'), 'LogonTrigger')
    ET.SubElement(trigger, 'UserId').text = user
    principal = ET.SubElement(ET.SubElement(root, 'Principals'), 'Principal', id='Author')
    for tag, value in [('UserId', user), ('LogonType', 'InteractiveToken'), ('RunLevel', 'LeastPrivilege')]:
        ET.SubElement(principal, tag).text = value
    settings = ET.SubElement(root, 'Settings')
    for tag, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                       ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'true'),
                       ('ExecutionTimeLimit', 'PT0S')]:
        ET.SubElement(settings, tag).text = value
    retry = ET.SubElement(settings, 'RestartOnFailure')
    ET.SubElement(retry, 'Interval').text = 'PT1M'
    ET.SubElement(retry, 'Count').text = '3'
    action = ET.SubElement(ET.SubElement(root, 'Actions', Context='Author'), 'Exec')
    args = command(base, 'service run')
    ET.SubElement(action, 'Command').text = args[0]
    ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline(args[1:])
    ET.SubElement(action, 'WorkingDirectory').text = str(base)
    return ET.tostring(root, encoding='utf-16', xml_declaration=True)


def run(*args, check=True):
    return subprocess.run(args, check=check)


def graceful_stop(base):
    # Stop only Runner; a managed Codex turn remains in the shared daemon.
    pidfile = base / 'daemon.pid'
    try:
        pid = int(pidfile.read_text(encoding='ascii'))
    except (FileNotFoundError, ValueError):
        return
    (base / 'stop.request').touch()
    deadline = time.monotonic() + 60
    while pidfile.exists() and process_alive(pid) and time.monotonic() < deadline:
        time.sleep(.2)
    if pidfile.exists() and process_alive(pid):
        raise RunnerError('Runner is still stopping; no Codex process was forcibly terminated')


def manage_service(store, action):
    base = store.base
    if action == 'run':
        with (base / 'service.log').open('a', encoding='utf-8', buffering=1) as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                daemon(store)
        return
    label = 'com.jaehyeong.codex-runner'
    if WINDOWS:
        task = 'CodexRunner-' + hashlib.sha256(str(base).encode()).hexdigest()[:10]
        if action == 'install':
            identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True)
            user = next(csv.reader(identity.splitlines()))[1]
            path = base / 'service-task.xml'
            path.write_bytes(windows_definition(base, user))
            run('schtasks', '/Create', '/TN', task, '/XML', str(path), '/F')
            run('schtasks', '/Run', '/TN', task)
        elif action == 'start':
            run('schtasks', '/Run', '/TN', task)
        elif action == 'status':
            run('schtasks', '/Query', '/TN', task, '/V', '/FO', 'LIST')
        elif action in ('stop', 'uninstall'):
            graceful_stop(base)
            if action == 'uninstall':
                run('schtasks', '/Delete', '/TN', task, '/F')
        return
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        target = 'gui/' + str(os.getuid())
        if action == 'install':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(launchd_definition(base, label)))
            path.chmod(0o600)
            if subprocess.run(['launchctl', 'print', target + '/' + label],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                run('launchctl', 'bootstrap', target, str(path))
        elif action == 'start':
            if subprocess.run(['launchctl', 'print', target + '/' + label],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                run('launchctl', 'bootstrap', target, str(path))
            else:
                run('launchctl', 'kickstart', target + '/' + label)
        elif action == 'status':
            run('launchctl', 'print', target + '/' + label)
        elif action in ('stop', 'uninstall'):
            run('launchctl', 'bootout', target + '/' + label)
            if action == 'uninstall':
                path.unlink(missing_ok=True)
        return
    if action == 'install':
        path = Path.home() / '.config/systemd/user/codex-runner.service'
        path.parent.mkdir(parents=True, exist_ok=True)
        # systemd has its own %-specifier expansion, even inside quoted arguments.
        quoted = ' '.join('"' + part.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
                          for part in command(base))
        path.write_text('[Unit]\nDescription=Codex Runner\n[Service]\nType=simple\nExecStart=' + quoted +
                        '\nRestart=on-failure\nRestartSec=15\nTimeoutStopSec=45\nKillMode=process\nUMask=0077\n'
                        '[Install]\nWantedBy=default.target\n', encoding='utf-8')
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', '--now', 'codex-runner.service')
    elif action == 'uninstall':
        run('systemctl', '--user', 'disable', '--now', 'codex-runner.service')
        (Path.home() / '.config/systemd/user/codex-runner.service').unlink(missing_ok=True)
        run('systemctl', '--user', 'daemon-reload')
    else:
        run('systemctl', '--user', action, 'codex-runner.service')
