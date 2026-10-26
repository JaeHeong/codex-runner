"""Per-user Codex queue. Python standard library only."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .platforms import (WINDOWS, child_options, codex_command, default_state_dir, no_console_options,
                        lock_file, process_alive, stop_process)

if __name__ == '__main__':
    # Keep exception/classes identical when optional UI modules import .cli.
    sys.modules.setdefault('codex_runner.cli', sys.modules[__name__])

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'description': 'Report the result of continuing this session. Inspect current files before repeating interrupted operations, preserve other people\'s changes, and respect existing instructions and permissions.',
    'properties': {
        'status': {'type': 'string', 'enum': ['complete', 'continue', 'blocked'],
                   'description': 'complete only when the user\'s requested outcome is verified; continue when useful authorized work remains; blocked when user input or permissions are required.'},
        'summary': {'type': 'string', 'description': 'What changed, verification evidence, and any remaining work or blocker.'},
    }, 'required': ['status', 'summary'],
}
BUDGET_LIMIT_STATES = {
    'workspace_owner_credits_depleted', 'workspace_member_credits_depleted',
    'workspace_owner_usage_limit_reached', 'workspace_member_usage_limit_reached',
}
PROMPT = '이전에 하던 작업 이어서 해줘'


class RunnerError(Exception):
    pass


def stamp(value=None):
    return dt.datetime.fromtimestamp(value or time.time(), dt.timezone.utc).isoformat(timespec='seconds')


def private_dir(path):
    path = Path(path).expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (not WINDOWS and path.stat().st_uid != os.getuid()):
        raise RunnerError('State directory must be owned by this user and not be a symlink')
    if not WINDOWS:
        path.chmod(0o700)
    return path


class Store:
    def __init__(self, path):
        self.base = private_dir(path)
        self.db = sqlite3.connect(self.base / 'state.sqlite3', timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.commit()
        from .remote import initialize
        initialize(self.db)

    def get(self, sid):
        row = self.db.execute('SELECT data FROM jobs WHERE id=?', (sid,)).fetchone()
        if not row:
            raise RunnerError('Session is not registered: ' + sid)
        return json.loads(row[0])

    def jobs(self):
        return [json.loads(x[0]) for x in self.db.execute('SELECT data FROM jobs ORDER BY id')]

    def resolve(self, reference):
        reference = reference.strip()
        jobs = self.jobs()
        exact = [j['id'] for j in jobs if j['id'] == reference]
        if exact:
            return exact[0]
        prefix = len(reference) >= 6 and re.fullmatch(r'[0-9a-fA-F-]+', reference)
        matches = [j for j in jobs if (j.get('name') or '').casefold() == reference.casefold()
                   or (prefix and j['id'].startswith(reference.lower()))]
        if len(matches) == 1:
            return matches[0]['id']
        if len(matches) > 1:
            raise RunnerError('Ambiguous session: ' + reference + '; choose ' + ', '.join(j.get('name') or j['id'] for j in matches))
        raise RunnerError('No registered session matches: ' + reference)

    def set_name(self, sid, name):
        name = validate_name(name)
        try:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(sid)
            if any(j['id'] != sid and (j.get('name') or '').casefold() == name.casefold() for j in self.jobs()):
                raise RunnerError('Session name already in use: ' + name)
            job.update(name=name, updated=time.time())
            self.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def add(self, job):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if job.get('name') and any((j.get('name') or '').casefold() == job['name'].casefold() for j in self.jobs()):
                raise RunnerError('Session name already in use: ' + job['name'])
            self.db.execute('INSERT INTO jobs VALUES (?,?)', (job['id'], json.dumps(job)))
            self.db.commit()
        except sqlite3.IntegrityError:
            self.db.rollback()
            raise RunnerError('Session already registered; use resume or remove') from None
        except BaseException:
            self.db.rollback()
            raise

    def patch(self, sid, expected=None, wake_for_input=False, **values):
        # Compare-and-set prevents a finishing child from overwriting a CLI pause.
        try:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(sid)
            if expected is None or job['status'] in expected:
                if wake_for_input and values.get('status') in ('complete', 'blocked'):
                    from .remote import pending_input
                    if pending_input(self, sid):
                        values.update(status='queued', detail='Human follow-up queued; waiting for a safe turn boundary', next_run=0)
                job.update(values, updated=time.time())
                self.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
            self.db.commit()
            return job
        except BaseException:
            self.db.rollback()
            raise

    def claim(self, sid):
        """Atomically acquire a queued registration, never an already running one."""
        try:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(sid)
            if job['status'] not in ('queued', 'waiting', 'idle'):
                self.db.commit()
                return None
            job.update(status='running', detail='Checking current task and subscription quota',
                       last_run=time.time(), updated=time.time())
            self.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
            self.db.commit()
            return job
        except BaseException:
            self.db.rollback()
            raise

    def migrate_idle(self):
        """Reclassify only the old ordinary-response guard, without resuming work."""
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for job in self.jobs():
                if (job['status'] == 'blocked' and job.get('managed') and not job.get('managed_turn_id')
                        and (job.get('detail') or '').startswith('Last Codex turn completed; task completion is unknown.')):
                    job.update(status='idle', watch_external=True, idle_turn_id=job.get('preflight_turn_id'),
                               next_run=time.time() + 15, updated=time.time(),
                               detail='Response finished; watching for new work without sending a message')
                    self.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def delete(self, sid):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            job = self.get(sid)
            if job['status'] == 'running':
                raise RunnerError('Pause the session and wait for its process to exit before removing it')
            if process_alive(job.get('pid')):
                raise RunnerError('Session process is still stopping; retry remove shortly')
            self.db.execute("UPDATE remote_inputs SET status='cancelled' WHERE session_id=? AND status='pending'", (sid,))
            self.db.execute("UPDATE remote_questions SET status='expired',answer=NULL WHERE session_id=? AND status IN ('pending','answered','submitting')", (sid,))
            self.db.execute('DELETE FROM jobs WHERE id=?', (sid,))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def migrate_runtime_errors(self):
        """Recheck the old systemError guard; this never grants a turn retry."""
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for job in self.jobs():
                if (job['status'] == 'error' and job.get('managed')
                        and not job.get('managed_turn_id') and not job.get('managed_start_pending')
                        and job.get('detail') == 'Cannot confirm Codex runtime state: systemError'):
                    job.update(status='waiting', watch_external=True, next_run=time.time(),
                               approved_completed_turn=None, updated=time.time(),
                               detail='Rechecking the latest Codex runtime and quota failure; no message sent')
                    self.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise


def child_env(job):
    env = dict(os.environ)
    env['PATH'] = str(Path(job['codex']).parent) + os.pathsep + job.get('path', env.get('PATH', ''))
    if job.get('codex_home'):
        env['CODEX_HOME'] = job['codex_home']
    # Session identity belongs to the resumed child, never to the daemon's caller.
    env.pop('CODEX_THREAD_ID', None)
    return env


def read_limits(job, timeout=30):
    """Use the documented RPC; never read auth tokens or call a model."""
    proc = subprocess.Popen(codex_command(job['codex'], 'app-server'), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding='utf-8', env=child_env(job), **child_options())
    messages = queue.Queue()

    def reader():
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put(None)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    def send(message):
        proc.stdin.write(json.dumps(message) + '\n')
        proc.stdin.flush()

    def rpc(number, method, params=None):
        send({'id': number, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if msg is None:
                raise RunnerError('Codex app-server exited before the quota response')
            if msg.get('id') == number:
                if 'error' in msg:
                    raise RunnerError('Codex quota RPC failed: ' + str(msg['error'].get('message', 'unknown error')))
                return msg['result']
        raise RunnerError('Codex quota lookup timed out; no model run was started')

    try:
        rpc(1, 'initialize', {'clientInfo': {'name': 'codex_runner', 'version': __version__}})
        send({'method': 'initialized', 'params': {}})
        account = rpc(2, 'account/read').get('account')
        if not account or account.get('type') not in ('chatgpt', 'chatgptAuthTokens'):
            raise RunnerError('ChatGPT login is required; API-key billing is not used as a fallback')
        return rpc(3, 'account/rateLimits/read')
    except (BrokenPipeError, OSError) as exc:
        raise RunnerError('Could not communicate with Codex app-server: ' + str(exc)) from None
    finally:
        stop_process(proc)
        proc.stdin.close()
        thread.join(timeout=2)
        proc.stdout.close()


def select_limit(data, limit_id):
    buckets = data.get('rateLimitsByLimitId') or {}
    if limit_id in buckets:
        return buckets[limit_id]
    single = data.get('rateLimits')
    if single and single.get('limitId') in (None, limit_id):
        return single
    raise RunnerError('No quota snapshot for bucket ' + limit_id + '; check codex-runner limits')


def retry_at(snapshot, now=None, grace=45, poll_seconds=300):
    now = time.time() if now is None else now
    windows = [snapshot.get(k) for k in ('primary', 'secondary')]
    exhausted = [w for w in windows if w and w.get('usedPercent', 0) >= 100]
    reached = snapshot.get('rateLimitReachedType')
    if reached and reached not in BUDGET_LIMIT_STATES | {'rate_limit_reached'}:
        raise RunnerError('Unknown account limit state: ' + reached)
    budget_blocked = reached in BUDGET_LIMIT_STATES or snapshot.get('spendControlReached') is True
    if exhausted:
        if any(w.get('resetsAt') is None for w in exhausted):
            return now + poll_seconds
        reset = max(now + grace, max(w['resetsAt'] for w in exhausted) + grace)
        return min(reset, now + poll_seconds) if budget_blocked else reset
    if reached or budget_blocked:
        # A workspace credit error may clear after an included allowance resets.
        # Recheck metadata only; never make model calls or buy credits while blocked.
        return now + poll_seconds
    if not any(windows):
        raise RunnerError('No subscription quota windows were returned')
    return None


def resolve_codex(value=None):
    found = shutil.which(value or 'codex')
    if not found:
        raise RunnerError('Codex CLI is not on PATH; use --codex /absolute/path/to/codex')
    # Keep the bin symlink location: its sibling node binary can be required by nvm.
    return str(Path(found).absolute())


def probe_codex(job):
    proc = subprocess.run(codex_command(job['codex'], 'exec', 'resume', '--help'), capture_output=True,
                          text=True, encoding='utf-8', env=child_env(job), timeout=20, **no_console_options())
    if proc.returncode or any(flag not in proc.stdout for flag in ('--json', '--output-schema', '--output-last-message')):
        raise RunnerError('Codex CLI must support exec resume with JSON and output-schema (tested: 0.163.0)')


def build_command(job, schema, output):
    cmd = [job['codex'], '--ask-for-approval', 'never', '--sandbox', job['sandbox'],
           'exec', 'resume', '--json', '--output-schema', str(schema),
           '--output-last-message', str(output)]
    if job.get('network') and job['sandbox'] == 'workspace-write':
        cmd += ['-c', 'sandbox_workspace_write.network_access=true']
    if job.get('model'):
        cmd += ['--model', job['model']]
    return codex_command(cmd[0], *cmd[1:], job['id'], job.get('prompt') or PROMPT)


class KeyLocks:
    def __init__(self):
        self.guard = threading.Lock()
        self.locks = {}

    def get(self, key):
        with self.guard:
            return self.locks.setdefault(key, threading.Lock())


class QuotaCache:
    def __init__(self):
        self.entries = {}
        self.locks = KeyLocks()

    def read(self, job, refresh=False, stopping=None):
        key = (job['codex'], job.get('codex_home'), job['limit_id'])
        lock = self.locks.get(key)
        while not lock.acquire(timeout=.2):
            if stopping and stopping.is_set():
                return None
        try:
            if stopping and stopping.is_set():
                return None
            cached = self.entries.get(key)
            if refresh or cached is None or time.monotonic() - cached[0] > 20:
                data = read_limits(job)
                self.entries[key] = (time.monotonic(), data)
            return self.entries[key][1]
        finally:
            lock.release()


class Worker:
    def __init__(self, store, stopping=None, quotas=None, runtimes=None):
        self.store = store
        self.stopping = stopping or threading.Event()
        self.quotas = quotas or QuotaCache()
        self.runtimes = runtimes or KeyLocks()
        self.wait_reasons = {}

    def note(self, job, message):
        directory = private_dir(self.store.base / 'jobs' / job['id'])
        with (directory / 'runner.log').open('a', encoding='utf-8') as out:
            out.write(stamp() + ' ' + message + '\n')
        print(stamp(), job['id'], message, flush=True)

    def capacity(self, job, refresh=False):
        data = self.quotas.read(job, refresh, self.stopping)
        if data is None:
            return None  # Shutdown: caller checks stopping before starting work.
        snapshot = select_limit(data, job['limit_id'])
        state = snapshot.get('rateLimitReachedType')
        if state in BUDGET_LIMIT_STATES or snapshot.get('spendControlReached') is True:
            self.wait_reasons[job['id']] = 'Account limit (' + (state or 'spend control') + '); polling availability'
        else:
            self.wait_reasons[job['id']] = 'Subscription quota unavailable'
        return retry_at(snapshot)

    def schedule_wait(self, job, when):
        reason = self.wait_reasons.get(job['id'], 'Subscription quota unavailable')
        self.store.patch(job['id'], expected=['running'], status='waiting', next_run=when,
                         detail=reason + '; next check ' + stamp(when), pid=None)
        self.note(job, reason + '; next check ' + stamp(when))

    def finish(self, job, result, waiting_for=None):
        if result.get('status') not in ('complete', 'continue', 'blocked') or not isinstance(result.get('summary'), str):
            raise RunnerError('Missing or invalid completion status; stopped')
        current = self.store.get(job['id'])
        summary = result['summary']
        status = {'complete': 'complete', 'continue': 'queued', 'blocked': 'blocked'}[result['status']]
        repeats = current.get('same_summary_count', 0) + 1 if summary == current.get('summary') else 0
        if status == 'queued' and repeats >= 2:
            status = 'blocked'
            summary = 'Repeated continuation without new progress: ' + summary
        detail = summary
        if waiting_for and status in ('queued', 'complete'):
            status, detail = 'waiting', waiting_for
        self.store.patch(job['id'], expected=['running'], wake_for_input=True, status=status, summary=summary,
                         same_summary_count=repeats, detail=detail, next_run=time.time() + (15 if status == 'waiting' else 2),
                         turns=current.get('turns', 0) + 1, pid=None, managed_turn_id=None)
        self.note(job, status + ': ' + detail)

    def run(self, job):
        if self.stopping.is_set():
            return
        try:
            job = self.store.claim(job['id'])
        except RunnerError:
            return  # A CLI remove can race with the scheduler selecting a queued job.
        if job is None:
            return
        proc = None
        try:
            if job.get('managed'):
                from .managed import run_managed
                run_managed(self, job)
                return
            when = self.capacity(job)
            if when:
                self.schedule_wait(job, when)
                return
            if self.stopping.is_set() or self.store.get(job['id']).get('pause_requested'):
                return
            directory = private_dir(self.store.base / 'jobs' / job['id'])
            schema = directory / 'output-schema.json'
            schema.write_text(json.dumps(SCHEMA))
            run_id = str(time.time_ns())
            output = directory / ('result-' + run_id + '.json')
            logfile = directory / ('events-' + run_id + '.jsonl')
            # Keep at most 20 attempts, including output files; credentials remain local.
            for pattern in ('events-*.jsonl', 'stderr-*.log', 'result-*.json'):
                for old in sorted(directory.glob(pattern))[:-19]:
                    old.unlink()
            self.note(job, 'Resuming session in ' + job['cwd'])
            with logfile.open('w', encoding='utf-8') as log, (directory / ('stderr-' + run_id + '.log')).open('w', encoding='utf-8') as err:
                proc = subprocess.Popen(build_command(job, schema, output), cwd=job['cwd'],
                                        stdout=log, stderr=err, env=child_env(job),
                                        **child_options())
                self.store.patch(job['id'], expected=['running'], pid=proc.pid,
                                 detail='Codex is running', logfile=str(logfile))
                while proc.poll() is None:
                    if self.stopping.wait(1) or self.store.get(job['id']).get('pause_requested'):
                        stop_process(proc)
                        break
            current = self.store.get(job['id'])
            if current.get('pause_requested'):
                self.store.patch(job['id'], status='paused', pause_requested=False, detail='Paused by user')
                return
            if self.stopping.is_set():
                self.store.patch(job['id'], expected=['running'], status='queued', detail='Daemon stopped; will resume on restart')
                return
            if proc.returncode:
                when = self.capacity(job, refresh=True)
                if when:
                    self.schedule_wait(job, when)
                    return
                raise RunnerError('Codex exited with code ' + str(proc.returncode) + '; inspect logs before resuming')
            # A child must resume the requested thread, not silently create another one.
            terminal = None
            saw_thread = False
            with logfile.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'thread.started':
                        if event.get('thread_id') != job['id']:
                            raise RunnerError('Codex returned a different session ID; stopped')
                        saw_thread = True
                    if event.get('type') in ('turn.failed', 'turn.completed'):
                        terminal = event['type']
            if not saw_thread or terminal != 'turn.completed':
                raise RunnerError('Codex did not confirm a completed turn on the requested thread; inspect logs')
            self.finish(job, json.loads(output.read_text(encoding='utf-8')))
        except (RunnerError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.store.patch(job['id'], expected=['running'], status='error', detail=str(exc), pid=None)
            self.note(job, 'Stopped: ' + str(exc))
        finally:
            if proc is not None:
                stop_process(proc)
            self.store.patch(job['id'], pid=None)
            if self.store.get(job['id']).get('pause_requested'):
                current = self.store.get(job['id'])
                if job.get('managed') and current.get('managed_turn_id'):
                    self.store.patch(job['id'], status='error', pause_requested=False,
                                     detail='Managed cancellation was not confirmed; inspect Codex before taking over')
                else:
                    self.store.patch(job['id'], status='paused', pause_requested=False, detail='Paused by user')
            elif self.stopping.is_set():
                self.store.patch(job['id'], expected=['running'], status='queued', detail='Daemon stopped; will resume on restart')


class Scheduler:
    """Independent session workers, each with its own SQLite connection."""
    def __init__(self, store, stopping, max_parallel=4):
        if max_parallel < 1:
            raise RunnerError('--max-parallel must be at least 1')
        self.store, self.stopping, self.max_parallel = store, stopping, max_parallel
        self.active = {}
        self.quotas, self.runtimes = QuotaCache(), KeyLocks()

    def _run(self, job):
        local = None
        try:
            local = Store(self.store.base)
            Worker(local, self.stopping, self.quotas, self.runtimes).run(job)
        except Exception as exc:
            print(stamp(), job['id'], 'Session worker failed:', str(exc), file=sys.stderr, flush=True)
            if local:
                with contextlib.suppress(RunnerError, sqlite3.Error):
                    local.patch(job['id'], expected=['running'], status='error', detail=str(exc))
        finally:
            if local:
                local.db.close()

    def tick(self):
        for sid, thread in list(self.active.items()):
            if not thread.is_alive():
                thread.join()
                del self.active[sid]
        if self.stopping.is_set():
            return
        ready = sorted((j for j in self.store.jobs() if j['id'] not in self.active
                        and j['status'] in ('queued', 'waiting', 'idle') and j.get('next_run', 0) <= time.time()),
                       key=lambda j: j.get('last_run', 0))
        for job in ready[:max(0, self.max_parallel - len(self.active))]:
            if self.stopping.is_set():
                break
            thread = threading.Thread(target=self._run, args=(job,), name='session-' + job['id'][:12])
            self.active[job['id']] = thread
            try:
                thread.start()
            except BaseException:
                del self.active[job['id']]
                raise
        if len(self.active) >= self.max_parallel:
            detail = 'Waiting for a session worker slot (' + str(self.max_parallel) + ' concurrent workers)'
            for job in ready:
                if job['id'] not in self.active and job['status'] == 'queued' and job.get('detail') != detail:
                    with contextlib.suppress(RunnerError):
                        self.store.patch(job['id'], expected=['queued'], detail=detail)

    def close(self):
        self.stopping.set()
        # Keep the daemon lock until all workers release their processes/stores.
        for thread in self.active.values():
            thread.join()


def daemon(store, once=False, max_parallel=4):
    if max_parallel < 1:
        raise RunnerError('--max-parallel must be at least 1')
    lock = (store.base / 'daemon.lock').open('a+b')
    try:
        lock_file(lock)
    except BlockingIOError:
        lock.close()
        raise RunnerError('A daemon is already running for this state directory') from None
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    scheduler = Scheduler(store, stopping, max_parallel)
    try:
        (store.base / 'stop.request').unlink(missing_ok=True)
        (store.base / 'daemon.pid').write_text(str(os.getpid()), encoding='ascii')
        store.migrate_idle()
        store.migrate_runtime_errors()
        for job in store.jobs():
            if job['status'] == 'running':
                if process_alive(job.get('pid')):
                    store.patch(job['id'], status='error', detail='A previous Codex process is still alive; inspect it before resuming')
                else:
                    store.patch(job['id'], status='paused' if job.get('pause_requested') else 'queued',
                                pause_requested=False, pid=None, detail='Recovered after daemon interruption')
        while not stopping.is_set():
            if (store.base / 'stop.request').exists():
                stopping.set()
                break
            if once:
                ready = sorted((j for j in store.jobs() if j['status'] in ('queued', 'waiting', 'idle')
                                and j.get('next_run', 0) <= time.time()), key=lambda j: j.get('last_run', 0))
                if ready:
                    Worker(store, stopping).run(ready[0])
                return
            scheduler.tick()
            stopping.wait(.5)
    finally:
        scheduler.close()
        (store.base / 'daemon.pid').unlink(missing_ok=True)
        lock.close()


def session_id(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError('Use the UUID printed by codex resume') from None


def update_prompt(store, sid, text):
    text = text.strip()
    if not text:
        raise RunnerError('Continuation prompt must not be empty')
    store.patch(sid, prompt=text)


def validate_name(name):
    name = name.strip()
    if not name or len(name) > 48 or not name[0].isalnum() or any(not (c.isalnum() or c in '._-') for c in name):
        raise RunnerError('Use a name of 1-48 letters/digits, dots, underscores or hyphens, starting with a letter/digit')
    try:
        uuid.UUID(name)
    except ValueError:
        return name
    raise RunnerError('A session name cannot be a full UUID')


def default_name(store, cwd, sid):
    name = ''.join(c if c.isalnum() or c in '._-' else '-' for c in Path(cwd).name).strip('._-')[:35] or 'session'
    existing = {(j.get('name') or '').casefold() for j in store.jobs()}
    if name.casefold() not in existing:
        return validate_name(name)
    for length in (8, 12, 16, 32):
        suffix = sid.replace('-', '')[:length]
        candidate = name[:48 - len(suffix) - 1] + '-' + suffix
        if candidate.casefold() not in existing:
            return validate_name(candidate)
    raise RunnerError('Choose an explicit --name for this session')


def select_session(store, reference=None):
    if reference is not None:
        return store.resolve(reference)
    jobs = store.jobs()
    if not jobs:
        raise RunnerError('No sessions registered; use add first')
    here = Path.cwd().resolve()
    candidates = [j for j in jobs if j.get('cwd') and here.is_relative_to(Path(j['cwd']).resolve())]
    if candidates:
        longest = max(len(str(Path(j['cwd']).resolve())) for j in candidates)
        candidates = [j for j in candidates if len(str(Path(j['cwd']).resolve())) == longest]
    elif len(jobs) == 1:
        candidates = jobs
    if len(candidates) == 1:
        return candidates[0]['id']
    if sys.stdin.isatty() and sys.stdout.isatty():
        from .menu import pick_session
        sid = pick_session(store)
        if sid:
            return sid
        raise RunnerError('Session selection cancelled')
    raise RunnerError('Specify a session name/UUID prefix, or run interactively to select one')


def control_job(store, sid, action, continue_completed=False, confirm=None):
    if action not in ('pause', 'resume'):
        raise RunnerError('Unknown control action: ' + action)
    job = store.get(sid)
    if action == 'resume' and (job['status'] == 'running' or process_alive(job.get('pid'))):
        raise RunnerError('Session process is still active; wait for pause to finish')
    preflight = {}
    if action == 'resume':
        from .managed import resume_preflight
        preflight = resume_preflight(store, job, continue_completed, confirm)
    if action == 'pause' and job['status'] == 'running':
        store.patch(sid, pause_requested=True, detail='Pausing; waiting for the current turn to stop')
    else:
        values = dict(status='paused' if action == 'pause' else 'queued', next_run=0,
                      pause_requested=False, detail='Paused by user' if action == 'pause' else 'Resumed by user')
        values.update(preflight)
        current = store.patch(sid, expected=['queued', 'waiting', 'idle', 'paused', 'complete', 'blocked', 'error'], **values)
        if current['status'] == 'running':
            if action == 'pause':
                store.patch(sid, pause_requested=True, detail='Pausing; waiting for the current turn to stop')
            else:
                raise RunnerError('Runner started work during the resume check; its running state was preserved')


def cli(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Resume local Codex sessions after subscription quota resets')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--state-dir', default=default_state_dir())
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('menu', aliases=['ui'], help='Interactive session list with keyboard and mouse actions')
    add = sub.add_parser('add', help='Queue an existing session; use --managed to share its local app-server')
    add.add_argument('session_id', type=session_id)
    add.add_argument('--cwd', required=True)
    add.add_argument('--name', help='Short unique session name; defaults to the project directory name')
    add.add_argument('--managed', action='store_true', help='Use the existing local Codex app-server; the terminal may stay connected for observation')
    add.add_argument('--codex')
    add.add_argument('--model')
    add.add_argument('--limit-id', default='codex')
    add.add_argument('--sandbox', choices=['read-only', 'workspace-write', 'danger-full-access'], default='workspace-write')
    add.add_argument('--network', action='store_true', help='Allow network access inside workspace-write (for example a local browser debugging port)')
    add.add_argument('--prompt-file', help='Additional scoped continuation instructions')
    for name in ['pause', 'resume', 'remove']:
        command = sub.add_parser(name)
        command.add_argument('session_id', nargs='?', help='Name, UUID, or unique UUID prefix (6+ characters)')
        if name == 'resume':
            command.add_argument('--continue-completed', action='store_true', help='Explicitly continue the checked completed turn after reviewing it')
    alias = sub.add_parser('alias', help='Give a registered session a short unique name without starting it')
    alias.add_argument('session_id')
    alias.add_argument('name')
    prompt = sub.add_parser('prompt', help='Read or change the next continuation message without starting work')
    prompt.add_argument('session_id', nargs='?')
    prompt_options = prompt.add_mutually_exclusive_group()
    prompt_options.add_argument('--text', help='Replace the entire continuation message')
    prompt_options.add_argument('--file', help='Read the entire continuation message from a UTF-8 file')
    prompt_options.add_argument('--default', action='store_true', help='Reset to the current built-in default')
    status = sub.add_parser('status'); status.add_argument('--json', action='store_true')
    logs = sub.add_parser('logs'); logs.add_argument('session_id', nargs='?')
    logs.add_argument('--lines', type=int, default=60, help='Recent readable lines (raw mode: lines per file)')
    logs.add_argument('--follow', '-f', action='store_true')
    logs.add_argument('--raw', action='store_true', help='Show original JSON/stderr for debugging')
    logs.add_argument('--local', action='store_true', help='Read saved logs without connecting to the managed server')
    logs.add_argument('--full', action='store_true', help='Show full commands and tool output instead of compact previews')
    run = sub.add_parser('daemon'); run.add_argument('--once', action='store_true', help='Process at most one ready session, then exit')
    run.add_argument('--max-parallel', type=int, default=os.environ.get('CODEX_RUNNER_MAX_PARALLEL', '4'),
                     help='Maximum concurrent session workers (default: 4; CODEX_RUNNER_MAX_PARALLEL)')
    send = sub.add_parser('send', help='Queue one human follow-up for a managed session')
    send.add_argument('session_id')
    send.add_argument('--text', required=True)
    chat = sub.add_parser('discord', help='Private Discord setup and bot service')
    chat_sub = chat.add_subparsers(dest='discord_action', required=True)
    config = chat_sub.add_parser('configure')
    config.add_argument('--guild-id', required=True)
    config.add_argument('--channel-id', required=True)
    config.add_argument('--allowed-user', action='append', default=[])
    config.add_argument('--allow-all-users', action='store_true')
    for name in ['token', 'doctor', 'invite', 'run']:
        command = chat_sub.add_parser(name)
        if name == 'doctor':
            command.add_argument('--offline', action='store_true', help='Check bundled SDK without credentials or network')
    chat_service = chat_sub.add_parser('service')
    chat_service.add_argument('action', choices=['install','start','stop','status','uninstall','run'])
    chat_service.add_argument('--no-start', action='store_true', help='Register the service without starting it (install only)')
    service = sub.add_parser('service', help='Manage this user’s background service')
    service.add_argument('action', choices=['install', 'start', 'stop', 'status', 'uninstall', 'run'])
    service.add_argument('--no-start', action='store_true', help='Register the service without starting it (install only)')
    for name in ['limits', 'doctor']:
        p = sub.add_parser(name); p.add_argument('--codex'); p.add_argument('--limit-id', default='codex')
    args = parser.parse_args(argv)
    if not WINDOWS and os.geteuid() == 0:
        raise RunnerError('Run as your Codex login user, not root; sudo is only needed to install the package')
    store = Store(args.state_dir)
    if args.command in ('pause', 'resume', 'remove', 'prompt', 'logs', 'alias', 'send'):
        args.session_id = select_session(store, args.session_id)
    if args.command in (None, 'menu', 'ui'):
        from .menu import run_menu
        run_menu(store)
    elif args.command == 'add':
        cwd = str(Path(args.cwd).expanduser().resolve(strict=True))
        if not Path(cwd).is_dir():
            raise RunnerError('--cwd must be a directory')
        if args.network and args.sandbox != 'workspace-write':
            raise RunnerError('--network applies only to --sandbox workspace-write')
        name = validate_name(args.name) if args.name else default_name(store, cwd, args.session_id)
        job = dict(id=args.session_id, cwd=cwd, codex=resolve_codex(args.codex), path=os.environ.get('PATH', ''),
                   codex_home=str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().absolute()),
                   model=args.model, limit_id=args.limit_id, sandbox=args.sandbox, network=args.network, managed=args.managed, name=name, status='queued',
                   created=time.time(), updated=time.time(), next_run=0, turns=0,
                   prompt=PROMPT + ('\nAdditional instructions:\n' + Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else ''))
        probe_codex(job)
        store.add(job)
        print('Queued', args.session_id, 'in', cwd)
        print('Start the per-user service: codex-runner service install')
    elif args.command == 'alias':
        store.set_name(args.session_id, args.name)
        print('Named', args.session_id, 'as', args.name, '(execution state unchanged)')
    elif args.command == 'prompt':
        store.get(args.session_id)
        text = PROMPT if args.default else Path(args.file).expanduser().read_text(encoding='utf-8') if args.file else args.text
        if text is not None:
            update_prompt(store, args.session_id, text)
            print('Updated continuation message; applies on the next turn. Execution state is unchanged.')
        else:
            print(store.get(args.session_id).get('prompt') or PROMPT)
    elif args.command in ('pause', 'resume'):
        control_job(store, args.session_id, args.command, getattr(args, 'continue_completed', False))
        print(args.command, args.session_id)
    elif args.command == 'remove':
        store.delete(args.session_id); print('Removed registration; Codex history and private logs are preserved')
    elif args.command == 'status':
        jobs = store.jobs()
        if args.json:
            print(json.dumps(jobs, indent=2))
        else:
            print('NAME                  STATUS     NEXT CHECK (UTC)           SESSION       DIRECTORY')
            for j in jobs:
                next_check = stamp(j['next_run']) if j['status'] in ('waiting', 'idle') else '-'
                print(f"{(j.get('name') or '-'):<21} {j['status']:<10} {next_check:<26} {j['id'][:12]:<13} {j['cwd']}")
                if j.get('detail'):
                    print('  ' + j['detail'])
    elif args.command == 'logs':
        if args.lines < 1:
            raise RunnerError('--lines must be positive')
        from .logview import show_logs
        show_logs(store, args.session_id, args.lines, args.follow, args.raw, args.local, args.full)
    elif args.command in ('limits', 'doctor'):
        job = dict(codex=resolve_codex(args.codex), path=os.environ.get('PATH', ''), codex_home=os.environ.get('CODEX_HOME'))
        probe_codex(job)
        data = read_limits(job)
        snapshot = select_limit(data, args.limit_id)
        when = retry_at(snapshot)
        # Print quota metadata only, never account identities or credentials.
        print(json.dumps({'codex': job['codex'], 'limit_id': args.limit_id,
                          'primary': snapshot.get('primary'), 'secondary': snapshot.get('secondary'),
                          'retry_at': stamp(when) if when else None, 'version': __version__}, indent=2))
        if args.command == 'doctor':
            print('CLI capabilities and ChatGPT quota access OK. No model request was made.')
    elif args.command == 'send':
        from .remote import enqueue_input
        identity = 'cli:' + str(uuid.uuid4())
        enqueue_input(store, args.session_id, args.text, identity)
        print('Queued one human follow-up:', identity)
    elif args.command == 'discord':
        from .discord_cli import discord_cli
        discord_cli(store, args)
    elif args.command == 'service':
        from .service import manage_service
        manage_service(store, args.action, start=not args.no_start)
    elif args.command == 'daemon':
        daemon(store, args.once, args.max_parallel)
    store.db.close()


def main():
    try:
        cli()
    except KeyboardInterrupt:
        return 130
    except (RunnerError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print('codex-runner: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
