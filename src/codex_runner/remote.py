"""Durable human input and question replies, independent of any chat provider."""
import hashlib
import json
from pathlib import Path
import time

from .cli import RunnerError

RELAY_METHODS = {'item/tool/requestUserInput', 'item/commandExecution/requestApproval',
                 'item/fileChange/requestApproval', 'item/permissions/requestApproval',
                 'mcpServer/elicitation/request'}


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS remote_inputs (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, result TEXT);
        CREATE TABLE IF NOT EXISTS remote_questions (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            request_id TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
            answer TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS remote_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')


def setting(store, key, value=None):
    if value is not None:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO remote_settings VALUES (?,?)', (key, json.dumps(value)))
        return value
    row = store.db.execute('SELECT value FROM remote_settings WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def relay_available(store):
    # Keep questions intact while the explicitly connected relay reconnects.
    return bool(setting(store, 'discord_questions_enabled'))


def pending_input(store, sid):
    row = store.db.execute("SELECT id,text FROM remote_inputs WHERE session_id=? AND status='pending' ORDER BY created,id LIMIT 1", (sid,)).fetchone()
    return {'id': row[0], 'text': row[1]} if row else None


def enqueue_input(store, sid, text, identity):
    text = text.strip()
    if not text or len(text) > 12000:
        raise RunnerError('Enter a message of 1-12000 characters')
    if not identity or len(identity) > 160:
        raise RunnerError('A bounded unique message identity is required')
    try:
        store.db.execute('BEGIN IMMEDIATE')
        job = store.get(sid)
        if not job.get('managed'):
            raise RunnerError('Remote messages require a registered managed session')
        if job.get('managed_start_pending'):
            raise RunnerError('Previous turn delivery is uncertain; inspect Codex before sending another message')
        if job.get('pause_requested'):
            raise RunnerError('Wait for the current pause to finish before sending a message')
        old = store.db.execute('SELECT session_id,text,status FROM remote_inputs WHERE id=?', (identity,)).fetchone()
        if old:
            if old[:2] != (sid, text):
                raise RunnerError('Message identity was already used for different input')
            store.db.commit()
            return old[2]
        store.db.execute('INSERT INTO remote_inputs VALUES (?,?,?,?,?,NULL)', (identity, sid, text, 'pending', time.time()))
        if job['status'] != 'running':
            job.update(status='queued', next_run=0, pause_requested=False, watch_external=True,
                       detail='Human follow-up queued; waiting for a safe turn boundary', updated=time.time())
            store.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
        store.db.commit()
        return 'pending'
    except BaseException:
        store.db.rollback()
        raise


def begin_delivery(store, sid, identity=None):
    """Mark before RPC. An uncertain response must never replay a model request."""
    try:
        store.db.execute('BEGIN IMMEDIATE')
        job = store.get(sid)
        if job['status'] != 'running' or job.get('pause_requested') or job.get('managed_start_pending'):
            raise RunnerError('Session state changed before delivery; no message sent')
        if identity:
            changed = store.db.execute("UPDATE remote_inputs SET status='dispatching' WHERE id=? AND session_id=? AND status='pending'", (identity, sid)).rowcount
            if not changed:
                raise RunnerError('Queued input was already claimed; no message sent')
        job.update(managed_start_pending=True, updated=time.time())
        store.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def confirm_delivery(store, sid, tid, identity=None):
    try:
        store.db.execute('BEGIN IMMEDIATE')
        job = store.get(sid)
        job.update(managed_start_pending=False, managed_turn_id=tid, updated=time.time())
        store.db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job), sid))
        if identity:
            store.db.execute("UPDATE remote_inputs SET status='sent',result=? WHERE id=? AND status='dispatching'", (tid, identity))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def question_key(job, message):
    params = message.get('params') or {}
    values = [str(Path(job.get('codex_home') or Path.home()/'.codex').resolve()),
              job['id'], params.get('turnId'), message.get('id')]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32]


def record_question(store, job, message, trusted_thread_id=None, replay=False):
    params = message.get('params') or {}
    if ('id' not in message or params.get('threadId') != (trusted_thread_id or job['id']) or not params.get('turnId')):
        raise RunnerError('Question does not belong to the registered turn')
    key = question_key(job, message)
    safe = json.loads(json.dumps(message))
    for q in (safe.get('params') or {}).get('questions') or []:
        if q.get('isSecret'):
            q.update(question='Secret input required; answer in the Codex terminal', options=None)
    now = time.time()
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO remote_questions VALUES (?,?,?,?,?,?,?,?,?)',
            (key, job['id'], params['turnId'], json.dumps(message['id']), json.dumps(safe), 'pending', None, now, now))
        if replay:
            store.db.execute("UPDATE remote_questions SET status='pending',answer=NULL,updated=? WHERE id=? AND status='submitting'", (now,key))
    refresh_question_state(store, job['id'])
    return key


def questions(store, sid=None, live_only=True):
    sql = 'SELECT id,session_id,turn_id,request,status,answer FROM remote_questions WHERE 1=1'
    args = []
    if live_only:
        sql += " AND status IN ('pending','answered','submitting')"
    if sid:
        sql += ' AND session_id=?'; args.append(sid)
    rows = store.db.execute(sql + ' ORDER BY created', args)
    return [dict(id=r[0], session_id=r[1], turn_id=r[2], request=json.loads(r[3]), status=r[4],
                 answer=json.loads(r[5]) if r[5] else None) for r in rows]


def can_answer(question):
    message = question['request']
    params = message.get('params') or {}
    items = params.get('questions') or []
    return (message.get('method') == 'item/tool/requestUserInput' and 1 <= len(items) <= 5
            and len({q.get('id') for q in items}) == len(items)
            and not any(q.get('isSecret') for q in items))


def answer_question(store, key, answers):
    try:
        store.db.execute('BEGIN IMMEDIATE')
        question = next((q for q in questions(store) if q['id'] == key), None)
        if not question or question['status'] != 'pending':
            raise RunnerError('Question was already answered or has expired; refresh the session')
        if not can_answer(question):
            raise RunnerError('This request must be handled in the Codex terminal')
        store.get(question['session_id'])
        expected = {q['id'] for q in question['request']['params']['questions']}
        if set(answers) != expected or any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in answers.values()):
            raise RunnerError('Provide one nonempty answer for each question')
        response = {'answers': {key: {'answers': [value.strip()]} for key, value in answers.items()}}
        store.db.execute("UPDATE remote_questions SET status='answered',answer=?,updated=? WHERE id=? AND status='pending'", (json.dumps(response), time.time(), key))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def refresh_question_state(store, sid):
    pending = questions(store, sid)
    blocking = any(q['request'].get('params', {}).get('isBlocking', True) for q in pending)
    try:
        store.patch(sid, awaiting_input=blocking)
    except RunnerError:
        pass


def resolve_question(store, job, request_id):
    with store.db:
        store.db.execute("UPDATE remote_questions SET status='resolved',answer=NULL,updated=? WHERE session_id=? AND request_id=? AND status IN ('pending','answered','submitting')", (time.time(), job['id'], json.dumps(request_id)))
    refresh_question_state(store, job['id'])


def expire_turn(store, sid, tid):
    with store.db:
        store.db.execute("UPDATE remote_questions SET status='expired',answer=NULL,updated=? WHERE session_id=? AND turn_id=? AND status IN ('pending','answered','submitting')", (time.time(), sid, tid))
    refresh_question_state(store, sid)


def send_answer(store, server, question, live_keys):
    # Replayed on this actual connection, not just an old database row.
    if question['id'] not in live_keys or question['status'] != 'answered':
        return False
    with store.db:
        claimed = store.db.execute("UPDATE remote_questions SET status='submitting',updated=? WHERE id=? AND status='answered'", (time.time(), question['id'])).rowcount
    if not claimed:
        return False
    # Keep submitting on an uncertain connection: never replay that reply.
    server.send({'id': question['request']['id'], 'result': question['answer']})
    return True
