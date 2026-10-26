"""Drive the user's local managed Codex server without a second session writer."""
import base64
import collections
import hashlib
import json
import os
import queue
import threading
from pathlib import Path
import socket
import stat
import struct
import subprocess
import time

from .cli import BUDGET_LIMIT_STATES, PROMPT, RunnerError, SCHEMA, child_env, private_dir
from . import __version__
from .platforms import WINDOWS, codex_command, stop_process, no_console_options

# Capacity failures on OpenAI's side: retried with backoff instead of stopping overnight work.
TRANSIENT_ERRORS = {'serverOverloaded'}


class ProxySocket:
    """Use Codex's native Windows socket client; keep WebSocket framing above it."""
    def __init__(self, home, path):
        from .cli import resolve_codex
        native = Path(home) / 'packages/app-server-daemon/current/bin/codex.exe'
        executable = str(native) if native.is_file() else resolve_codex()
        self.proc = subprocess.Popen(codex_command(executable, 'app-server', 'proxy', '--sock', str(path)),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0, **no_console_options())
        self.timeout = 15
        self.data = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while not self.stopping.is_set():
                chunk = self.proc.stdout.read(65536)
                while not self.stopping.is_set():
                    try:
                        self.data.put(chunk, timeout=.2)
                        break
                    except queue.Full:
                        continue
                if not chunk:
                    break
        except (OSError, ValueError):
            if not self.stopping.is_set():
                self.data.put(b'')

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, _path):
        pass  # The official proxy process connects to the explicit socket.

    def recv(self, _size):
        try:
            return self.data.get(timeout=self.timeout)
        except queue.Empty:
            if self.proc.poll() is not None:
                raise RunnerError('Codex socket proxy exited; inspect the local daemon') from None
            raise TimeoutError() from None

    def sendall(self, data):
        remaining = memoryview(data)
        while remaining:
            written = self.proc.stdin.write(remaining)
            if not written:
                raise RunnerError('Codex socket proxy closed')
            remaining = remaining[written:]

    def close(self):
        self.stopping.set()
        if self.proc.poll() is None:
            if WINDOWS:
                stop_process(self.proc)  # Proxy descendants only; never the shared server.
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait(timeout=3)
        self.reader.join(timeout=2)
        self.proc.stdin.close()
        self.proc.stdout.close()


class LocalServer:
    """Small bounded RFC 6455 client, restricted to an owned Unix socket."""
    def __init__(self, home, opt_out=None):
        path = Path(home) / 'app-server-control/app-server-control.sock'
        if WINDOWS:
            if not path.exists():
                raise FileNotFoundError(str(path))
            self.sock = ProxySocket(home, path)
        else:
            info = path.stat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise RunnerError('Managed Codex socket must be owned by the current user')
            self.sock = socket.socket(socket.AF_UNIX)
        self.buffer = b''
        self.pending = collections.deque()
        self.number = 0
        try:
            self.sock.settimeout(15)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                               'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                               'Sec-WebSocket-Key: ' + key + '\r\n\r\n').encode())
            while b'\r\n\r\n' not in self.buffer:
                self._more()
                if len(self.buffer) > 16384:
                    raise RunnerError('Oversized local WebSocket handshake')
            headers, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            fields = dict(line.split(b':', 1) for line in headers.split(b'\r\n')[1:] if b':' in line)
            fields = {k.lower(): v.strip() for k, v in fields.items()}
            if b' 101 ' not in headers.split(b'\r\n')[0] or fields.get(b'sec-websocket-accept') != expected:
                raise RunnerError('Invalid local Codex WebSocket handshake')
            self.rpc('initialize', {'clientInfo': {'name': 'codex_runner', 'version': __version__},
                                    'capabilities': dict(experimentalApi=True, **({'optOutNotificationMethods': opt_out} if opt_out else {}))})
            self.send({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _more(self):
        data = self.sock.recv(65536)
        if not data:
            raise RunnerError('Managed Codex connection closed; no new turn was replayed')
        self.buffer += data

    def _send_frame(self, data, opcode=1):
        n = len(data)
        header = bytes([0x80 | opcode, 0x80 | (n if n < 126 else 126 if n < 65536 else 127)])
        if n >= 126:
            header += struct.pack('!H' if n < 65536 else '!Q', n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send(self, message):
        self._send_frame(json.dumps(message).encode())

    def receive(self, timeout=1):
        self.sock.settimeout(timeout)
        while True:
            while len(self.buffer) < 2:
                self._more()
            a, b = self.buffer[:2]
            offset, size = 2, b & 127
            extra = 2 if size == 126 else 8 if size == 127 else 0
            while len(self.buffer) < offset + extra:
                self._more()
            if extra:
                size = int.from_bytes(self.buffer[offset:offset + extra], 'big')
                offset += extra
            if size > 16 * 1024 * 1024 or b & 128 or not a & 128:
                raise RunnerError('Unsupported or oversized local WebSocket frame')
            while len(self.buffer) < offset + size:
                self._more()
            data, self.buffer = self.buffer[offset:offset + size], self.buffer[offset + size:]
            opcode = a & 15
            if opcode == 9:
                self._send_frame(data, 10)
            elif opcode == 8:
                raise RunnerError('Managed Codex WebSocket closed')
            elif opcode == 1:
                return json.loads(data)
            elif opcode != 10:
                raise RunnerError('Unsupported local WebSocket message')

    def rpc(self, method, params):
        self.number += 1
        number = self.number
        self.send({'id': number, 'method': method, 'params': params})
        end = time.monotonic() + 30
        while time.monotonic() < end:
            try:
                message = self.receive(min(1, max(.01, end - time.monotonic())))
            except TimeoutError:
                continue
            if message.get('id') == number and 'method' not in message:
                if 'error' in message:
                    raise RunnerError('Managed Codex RPC: ' + message['error'].get('message', 'unknown error'))
                return message['result']
            self.pending.append(message)
        raise RunnerError('Managed Codex RPC timed out; inspect the session before retrying')

    def event(self):
        return self.pending.popleft() if self.pending else self.receive()


def sandbox_policy(job):
    if job['sandbox'] == 'danger-full-access':
        return {'type': 'dangerFullAccess'}
    if job['sandbox'] == 'read-only':
        return {'type': 'readOnly', 'networkAccess': False}
    return {'type': 'workspaceWrite', 'writableRoots': [job['cwd']], 'networkAccess': bool(job.get('network'))}


def find_turn(server, sid, tid):
    cursor = None
    while True:
        params = {'threadId': sid, 'limit': 50, 'sortDirection': 'desc', 'itemsView': 'full'}
        if cursor:
            params['cursor'] = cursor
        result = server.rpc('thread/turns/list', params)
        for turn in result.get('data', []):
            if turn['id'] == tid:
                return turn
        cursor = result.get('nextCursor')
        if not cursor:
            raise RunnerError('Owned managed turn was not found; inspect the session')


def check_children(server, sid, turn):
    """Only follow agents referenced by this task. Shell daemons are not agents."""
    pending, seen = [(sid, turn)], {sid}
    while pending:
        parent, current = pending.pop()
        targets = set()
        for item in (current or {}).get('items', []):
            if item.get('type') != 'collabAgentToolCall' or item.get('senderThreadId') != parent:
                continue
            targets.update(item.get('receiverThreadIds') or [])
        for child in sorted(targets - seen):
            seen.add(child)
            if len(seen) > 32:
                return 'review', 'Too many linked agents to confirm; inspect Codex before continuing'
            thread = server.rpc('thread/read', {'threadId': child, 'includeTurns': False})['thread']
            # The API parent relationship prevents unrelated referenced threads
            # (e.g. listAgents) from holding this task indefinitely.
            if thread.get('parentThreadId') != parent:
                continue
            state = (thread.get('status') or {}).get('type')
            if state == 'active':
                return 'wait', 'Linked agent ' + child[:12] + ' is active; next check in 15 seconds'
            if state not in ('idle', 'notLoaded'):
                return 'review', 'Cannot confirm linked agent state: ' + child[:12]
            page = server.rpc('thread/turns/list', {'threadId': child, 'limit': 1,
                                                  'sortDirection': 'desc', 'itemsView': 'full'})
            latest = next(iter(page.get('data') or []), None)
            if latest and latest.get('status') == 'inProgress':
                fresh = server.rpc('thread/read', {'threadId': child, 'includeTurns': False})['thread']
                if (fresh.get('status') or {}).get('type') == 'active':
                    return 'wait', 'Linked agent ' + child[:12] + ' is active; next check in 15 seconds'
                return 'review', 'Linked agent has an unfinished saved turn without a confirmed active runtime: ' + child[:12]
            pending.append((child, latest))
    return None


def inspect_latest(server, job):
    """Read runtime + last turn. A successful turn alone is not task completion."""
    sid = job['id']
    thread = server.rpc('thread/read', {'threadId': sid, 'includeTurns': False})['thread']
    if thread['id'] != sid or Path(thread['cwd']).resolve() != Path(job['cwd']).resolve():
        raise RunnerError('Managed thread ID or working directory does not match the registration')
    runtime = (thread.get('status') or {}).get('type')
    if runtime not in ('idle', 'active', 'notLoaded', 'systemError'):
        raise RunnerError('Cannot confirm Codex runtime state: ' + str(runtime))
    page = server.rpc('thread/turns/list', {'threadId': sid, 'limit': 1,
                                          'sortDirection': 'desc', 'itemsView': 'full'})
    turn = next(iter(page.get('data') or []), None)
    decision = {'turn_id': turn.get('id') if turn else None}
    error = (turn or {}).get('error') or {}
    quota_error = (turn or {}).get('status') == 'failed' and (
        error.get('codexErrorInfo') == 'usageLimitExceeded' or any(
            state in json.dumps(error) for state in BUDGET_LIMIT_STATES))
    retryable = quota_error or ((turn or {}).get('status') == 'failed' and error.get('codexErrorInfo') in TRANSIENT_ERRORS)
    # Codex keeps a quota-failed thread in systemError until its next turn.
    # Requiring idle here would permanently stop the very recovery we manage.
    # Only the latest persisted, explicit quota or capacity failure permits that
    # exception; unknown runtime failures still fail closed, without sending a message.
    if runtime == 'systemError' and not retryable:
        raise RunnerError('Cannot confirm Codex runtime state: systemError')
    if runtime == 'active':
        return dict(decision, action='wait', detail='Another turn is active; observing without interrupting, next check in 15 seconds')
    if not turn:
        return dict(decision, action='blocked', detail='No previous Codex turn to continue; enter a task in Codex first')
    if turn.get('status') == 'inProgress':
        fresh = server.rpc('thread/read', {'threadId': sid, 'includeTurns': False})['thread']
        if (fresh.get('status') or {}).get('type') == 'active':
            return dict(decision, action='wait', detail='Another turn is active; observing without interrupting, next check in 15 seconds')
        return dict(decision, action='blocked', detail='Saved turn is unfinished but no live active runtime was confirmed; inspect Codex before continuing')
    child_state = check_children(server, sid, turn)
    if child_state:
        return dict(decision, action=child_state[0], detail=child_state[1], linked_agent_check=True)
    status = turn.get('status')
    if job.get('_explicit_input') and status in ('completed', 'failed', 'interrupted'):
        return dict(decision, action='resume', detail='Explicit human follow-up for this session')
    if status in ('failed', 'interrupted'):
        if job.get('watch_external') and not retryable:
            return dict(decision, action='idle', detail='External turn ' + status + '; no automatic retry without a confirmed quota failure')
        return dict(decision, action='resume', detail='Previous turn was ' + status)
    if status != 'completed':
        raise RunnerError('Cannot confirm last Codex turn state: ' + str(status))
    if job.get('approved_completed_turn') == turn.get('id'):
        return dict(decision, action='resume', detail='Explicit continuation of this completed turn')
    finals = [v.get('text', '') for v in turn.get('items', [])
              if v.get('type') == 'agentMessage' and v.get('phase') in (None, 'final_answer')]
    result = None
    if finals:
        try:
            result = json.loads(finals[-1])
        except ValueError:
            pass
    if isinstance(result, dict) and result.get('status') in ('complete', 'continue', 'blocked') and isinstance(result.get('summary'), str):
        action = {'complete': 'complete', 'continue': 'resume', 'blocked': 'blocked'}[result['status']]
        return dict(decision, action=action, detail=result['summary'])
    return dict(decision, action='idle', detail='Response finished; watching for new work without sending a message',
                preview=finals[-1][:500] if finals else '(No final answer available)')


def resume_preflight(store, job, allow_completed=False, confirm=None):
    """Check before queuing as well as inside the worker; never starts a turn."""
    if not job.get('managed') or job.get('managed_turn_id'):
        if job['status'] == 'complete' and not allow_completed:
            raise RunnerError('Task is already complete; use --continue-completed only to explicitly continue it')
        return {}
    server = LocalServer(job.get('codex_home') or str(Path.home() / '.codex'))
    try:
        # A manual Resume authorizes retry of a stopped turn. Automatic idle
        # observation, by contrast, only retries confirmed quota failures.
        decision = inspect_latest(server, dict(job, watch_external=False))
    finally:
        server.close()
    action = decision['action']
    if action == 'idle':
        if allow_completed:
            return {'approved_completed_turn': decision['turn_id'], 'watch_external': False}
        return {'status': 'idle', 'watch_external': True, 'idle_turn_id': decision['turn_id'],
                'next_run': time.time() + 15, 'detail': decision['detail'], 'approved_completed_turn': None}
    if action in ('complete', 'review'):
        if not decision.get('linked_agent_check') and (allow_completed or confirm and confirm(decision)):
            return {'approved_completed_turn': decision['turn_id']}
        store.patch(job['id'], expected=['queued', 'waiting', 'idle', 'paused', 'complete', 'blocked', 'error'],
                    status='complete' if action == 'complete' else 'blocked',
                    next_run=0, preflight_turn_id=decision['turn_id'], detail=decision['detail'])
        if decision.get('linked_agent_check'):
            raise RunnerError(decision['detail'] + '\nNo message sent; resolve the linked agent state first.')
        raise RunnerError(decision['detail'] + '\nNo message sent. To explicitly continue: codex-runner resume ' +
                          (job.get('name') or job['id']) + ' --continue-completed')
    if action == 'blocked':
        store.patch(job['id'], expected=['queued', 'waiting', 'idle', 'paused', 'complete', 'blocked', 'error'],
                    status='blocked', next_run=0, detail=decision['detail'])
        raise RunnerError(decision['detail'] + '\nNo message sent.')
    return {'approved_completed_turn': None, 'watch_external': action == 'wait'}


def preflight_worker(worker, job, server):
    from .remote import pending_input
    decision = inspect_latest(server, dict(job, _explicit_input=bool(pending_input(worker.store, job['id']))))
    action = decision['action']
    if action == 'resume':
        return True
    if action == 'wait':
        worker.store.patch(job['id'], expected=['running'], status='waiting', next_run=time.time() + 15,
                           detail=decision['detail'], watch_external=True)
    elif action == 'idle':
        worker.store.patch(job['id'], expected=['running'], status='idle', next_run=time.time() + 15,
                           detail=decision['detail'], idle_turn_id=decision['turn_id'], watch_external=True,
                           approved_completed_turn=None, last_response_preview=decision.get('preview'))
        if job.get('idle_turn_id') != decision['turn_id']:
            worker.note(job, 'Idle: ' + decision['detail'] + ' (no message sent)')
    else:
        worker.store.patch(job['id'], expected=['running'], status='complete' if action == 'complete' else 'blocked',
                           next_run=0, preflight_turn_id=decision['turn_id'], detail=decision['detail'])
        worker.note(job, 'Preflight ' + action + ': ' + decision['detail'] + ' (no message sent)')
    return False


def run_managed(worker, job):
    sid = job['id']
    store = worker.store
    home = job.get('codex_home') or str(Path.home() / '.codex')
    with worker.runtimes.get(str(Path(home).resolve())):
        try:
            server = LocalServer(home)
        except (FileNotFoundError, ConnectionRefusedError):
            # Serialize bootstrap for sessions sharing this runtime. Never
            # restart a responding server or start a second session writer.
            if worker.stopping.is_set():
                return
            started = subprocess.run(codex_command(job['codex'], 'app-server', 'daemon', 'start'),
                                     env=child_env(job), capture_output=True, timeout=45, **no_console_options())
            if started.returncode:
                reason = (started.stderr or b'').decode('utf-8', errors='replace')
                if WINDOWS and any(text in reason for text in (
                        'non-elevated terminal', 'prevents background processes from outliving it')):
                    store.patch(sid, expected=['running'], status='waiting', next_run=time.time() + 30,
                                watch_external=True,
                                detail='Waiting for the shared Codex daemon; open Codex desktop/CLI or run codex app-server daemon start in a normal terminal')
                    return
                raise RunnerError('Could not start the local managed Codex daemon; inspect codex app-server daemon')
            server = LocalServer(home)
    tid = job.get('managed_turn_id')
    directory = private_dir(store.base / 'jobs' / sid)
    logfile = directory / ('events-' + str(time.time_ns()) + '.jsonl')
    for old in sorted(directory.glob('events-*.jsonl'))[:-19]:
        old.unlink()
    try:
        # Subscribe to the existing server's thread. Never start a second CLI writer.
        thread = server.rpc('thread/resume', {'threadId': sid, 'excludeTurns': True})['thread']
        if thread['id'] != sid or Path(thread['cwd']).resolve() != Path(job['cwd']).resolve():
            raise RunnerError('Managed thread ID or working directory does not match the registration')
        if job.get('managed_start_pending') and not tid:
            raise RunnerError('Previous turn/start had no confirmed ID; inspect the session before retrying')
        if tid:
            turn = find_turn(server, sid, tid)
        else:
            if not preflight_worker(worker, job, server):
                return
            when = worker.capacity(job)
            if when:
                worker.schedule_wait(job, when)
                return
            # Quota/auth requests can take seconds. The terminal may start or
            # finish work meanwhile: discard the earlier idle snapshot.
            current = store.get(sid)
            if worker.stopping.is_set() or current.get('pause_requested') or current['status'] != 'running':
                return
            if not preflight_worker(worker, current, server):
                return
            from .remote import pending_input, begin_delivery, confirm_delivery
            followup = pending_input(store, sid)
            params = {'threadId': sid, 'input': [{'type': 'text', 'text': followup['text'] if followup else job.get('prompt') or PROMPT}],
                      'cwd': job['cwd'], 'approvalPolicy': 'never', 'sandboxPolicy': sandbox_policy(job),
                      'outputSchema': SCHEMA}
            if job.get('model'):
                params['model'] = job['model']
            # A lost start response must never cause the same request to be replayed.
            begin_delivery(store, sid, followup['id'] if followup else None)
            turn = server.rpc('turn/start', params)['turn']
            tid = turn['id']
            confirm_delivery(store, sid, tid, followup['id'] if followup else None)
            worker.note(job, 'Started managed turn ' + tid + ' in ' + job['cwd'])
        store.patch(sid, expected=['running'], detail='Codex managed turn ' + tid + ' is running', logfile=str(logfile))
        interrupted = False
        needs_input = False
        last_check = time.monotonic()
        with logfile.open('a', encoding='utf-8') as log:
            while turn.get('status') == 'inProgress':
                current = store.get(sid)
                if current.get('pause_requested') and not interrupted:
                    server.rpc('turn/interrupt', {'threadId': sid, 'turnId': tid})
                    interrupted = True
                if worker.stopping.is_set():
                    # The managed server owns the work; a service restart must not cancel it.
                    store.patch(sid, expected=['running'], status='queued',
                                detail='Runner stopped; managed turn continues and will be reattached')
                    return
                try:
                    message = server.event()
                except TimeoutError:
                    message = None
                if message:
                    params = message.get('params', {})
                    if params.get('threadId') == sid:
                        log.write(json.dumps(message, ensure_ascii=False) + '\n')
                        log.flush()
                        if message.get('method') == 'turn/completed' and params.get('turn', {}).get('id') == tid:
                            turn = params['turn']
                        if 'id' in message and 'method' in message and params.get('turnId', tid) == tid:
                            from .remote import RELAY_METHODS, relay_available, record_question
                            if relay_available(store):
                                if message['method'] in RELAY_METHODS and params.get('turnId'):
                                    record_question(store, job, message)
                                    store.patch(sid, detail='Waiting for human input; question relayed to Discord')
                            else:
                                # Keep the original unattended behavior until a relay is online.
                                server.rpc('turn/interrupt', {'threadId': sid, 'turnId': tid})
                                needs_input = True
                if time.monotonic() - last_check >= 10:
                    turn = find_turn(server, sid, tid)
                    last_check = time.monotonic()
        from .remote import expire_turn
        expire_turn(store, sid, tid)
        if needs_input:
            store.patch(sid, status='blocked', managed_turn_id=None,
                        detail='Managed turn requested interactive input; inspect Codex')
            return
        if store.get(sid).get('pause_requested') or interrupted:
            store.patch(sid, status='paused', pause_requested=False, managed_turn_id=None,
                        detail='Paused by user')
            return
        if turn.get('status') in ('failed', 'interrupted'):
            store.patch(sid, managed_turn_id=None)
            when = worker.capacity(job, refresh=True)
            info = (turn.get('error') or {}).get('codexErrorInfo') if turn.get('status') == 'failed' else None
            if not when and info == 'usageLimitExceeded':
                # The turn can report the limit before the quota endpoint catches up.
                worker.wait_reasons[sid] = 'Codex reported usageLimitExceeded; rechecking account availability'
                when = time.time() + 300
            elif not when and info in TRANSIENT_ERRORS:
                retries = store.get(sid).get('overload_retries', 0)
                store.patch(sid, overload_retries=retries + 1)
                worker.wait_reasons[sid] = 'Codex reported ' + info + '; retrying'
                when = time.time() + min(300 * 2 ** retries, 1800)
            if when:
                worker.schedule_wait(job, when)
                return
            raise RunnerError('Managed turn ' + str(turn.get('status')) + ': ' + json.dumps(turn.get('error')))
        if turn.get('status') != 'completed':
            raise RunnerError('Managed turn has an unknown terminal status')
        # Notifications may omit items; read the persisted final turn before evaluating it.
        turn = find_turn(server, sid, tid)
        finals = [v.get('text', '') for v in turn.get('items', [])
                  if v.get('type') == 'agentMessage' and v.get('phase') in (None, 'final_answer')]
        if not finals:
            raise RunnerError('Managed turn completed without a final answer')
        result = json.loads(finals[-1])
        children = check_children(server, sid, turn)
        if children and children[0] != 'wait':
            store.patch(sid, expected=['running'], status='blocked', managed_turn_id=None,
                        next_run=0, detail=children[1])
            return
        # Record the finished parent once and release its turn ownership. Future
        # preflight polls observe linked children before any new continuation.
        worker.finish(job, result, waiting_for=children[1] if children else None)
        store.patch(sid, managed_turn_id=None, overload_retries=0)
    finally:
        server.close()
