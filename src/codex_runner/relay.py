"""Subscribe to native questions without becoming another model-turn writer."""
import contextlib
from pathlib import Path
import threading
import time

from .cli import Store
from .managed import LocalServer
from .remote import (RELAY_METHODS, expire_turn, question_key, questions, record_question, resolve_question, send_answer)


def home_for(job):
    return str(Path(job.get('codex_home') or Path.home()/'.codex').resolve())


class RuntimeObserver:
    def __init__(self, base, home, stop):
        self.base, self.home, self.stop = base, home, stop
        self.thread = threading.Thread(target=self.run, name='codex-question-observer', daemon=True)
        self.error = None
        self.thread.start()

    def run(self):
        store = Store(self.base)
        try:
            while not self.stop.is_set():
                server = None
                try:
                    server = LocalServer(self.home, opt_out=[
                        'item/agentMessage/delta','item/reasoning/textDelta','item/reasoning/summaryTextDelta',
                        'item/reasoning/summaryPartAdded','item/commandExecution/outputDelta',
                        'item/fileChange/outputDelta','turn/diff/updated','item/mcpToolCall/progress',
                        'item/started','item/completed','turn/plan/updated'])
                    self.connected(store, server)
                except Exception as exc:
                    # Error classes only: RPC strings may contain user content.
                    self.error = type(exc).__name__
                    self.stop.wait(5)
                finally:
                    if server:
                        with contextlib.suppress(Exception):
                            server.close()
        finally:
            store.db.close()

    def connected(self, store, server):
        owners, live_keys, latest_turns = {}, set(), {}
        sync_at, connected_at = 0, time.monotonic()
        reconciled = False
        while not self.stop.is_set():
            now = time.monotonic()
            if now >= sync_at:
                registered = {j['id']: j for j in store.jobs() if j.get('managed') and home_for(j) == self.home}
                desired = dict(registered)
                # Native agents can ask questions too. Only follow the API's
                # verified parent relationship, never arbitrary mentioned IDs.
                page = server.rpc('thread/list', {'limit': 100})
                candidates = page.get('data') or []
                for _ in range(8):
                    changed = False
                    for child in candidates:
                        parent, sid = child.get('parentThreadId'), child.get('id')
                        if parent in desired and sid not in desired and len(desired) < len(registered) + 32:
                            desired[sid] = desired[parent]; changed = True
                    if not changed:
                        break
                for sid, job in desired.items():
                    if sid not in owners:
                        thread = server.rpc('thread/resume', {'threadId': sid, 'excludeTurns': True})['thread']
                        if thread['id'] != sid:
                            raise RuntimeError('Unexpected subscribed thread')
                        owners[sid] = job
                for sid in set(owners) - set(desired):
                    server.rpc('thread/unsubscribe', {'threadId': sid})
                    del owners[sid]
                sync_at = now + 15
                self.error = None
            try:
                message = server.event()
            except TimeoutError:
                message = None
            if message:
                params = message.get('params') or {}
                sid = params.get('threadId')
                job = owners.get(sid)
                method = message.get('method')
                if job:
                    if 'id' in message and method in RELAY_METHODS:
                        if not params.get('turnId'):
                            # A few approval request shapes omit a turn id.
                            # Never answer these automatically or mislabel them.
                            continue
                        key = question_key(job, message)
                        key = record_question(store, job, message, trusted_thread_id=sid, replay=key not in live_keys)
                        live_keys.add(key)
                    elif method == 'serverRequest/resolved':
                        for q in questions(store, job['id']):
                            if q['request']['id'] == params.get('requestId'):
                                live_keys.discard(q['id'])
                        resolve_question(store, job, params.get('requestId'))
                    elif method == 'turn/completed':
                        tid = (params.get('turn') or {}).get('id')
                        expire_turn(store, job['id'], tid)
                        latest_turns[sid] = tid
            # Replies require a request replayed on this connection. Old rows
            # survive for notifications but cannot answer a future/reused id.
            for q in questions(store):
                if q['session_id'] in {j['id'] for j in owners.values()}:
                    send_answer(store, server, q, live_keys)
            if not reconciled and now - connected_at > 3 and not getattr(server, 'pending', None):
                tracked = {j['id'] for j in owners.values()}
                with store.db:
                    for q in questions(store):
                        if q['session_id'] in tracked and q['id'] not in live_keys:
                            store.db.execute("UPDATE remote_questions SET status='expired',answer=NULL,updated=? WHERE id=?", (time.time(), q['id']))
                reconciled = True


class Observers:
    def __init__(self, base):
        self.base, self.stop = base, threading.Event()
        self.workers = {}

    def sync(self, jobs):
        homes = {home_for(j) for j in jobs if j.get('managed')}
        for home in homes - self.workers.keys():
            self.workers[home] = RuntimeObserver(self.base, home, self.stop)

    def close(self):
        self.stop.set()
        for worker in self.workers.values():
            worker.thread.join(timeout=35)
