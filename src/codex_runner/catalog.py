"""Read local Codex conversation metadata and register selected sessions paused."""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .cli import PROMPT, RunnerError, default_name, probe_codex, resolve_codex, validate_name


def account_job():
    return {'codex': resolve_codex(), 'codex_home': str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().absolute()),
            'path': os.environ.get('PATH', ''), 'limit_id': 'codex'}


def title(thread):
    return str(thread.get('name') or thread.get('preview') or thread.get('title') or thread.get('id', ''))


def updated(thread):
    return float(thread.get('updatedAt') or thread.get('updated_at') or thread.get('createdAt') or 0)


def date_text(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M') if value else '기록 없음'


def stored_sessions(home):
    """Offline metadata only. Never read auth files or alter Codex history."""
    database = Path(home) / 'state_5.sqlite'
    if not database.exists():
        return []
    connection = sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(threads)')}
        if not {'id', 'cwd', 'updated_at'} <= columns:
            return []
        selected = [key for key in ['id', 'cwd', 'title', 'name', 'preview', 'updated_at', 'rollout_path'] if key in columns]
        query = 'SELECT ' + ','.join(selected) + ' FROM threads'
        if 'archived' in columns:
            query += ' WHERE archived=0'
        query += ' ORDER BY updated_at DESC'
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def load_sessions(job):
    from .managed import LocalServer
    server = None
    try:
        server = LocalServer(job['codex_home'])
        sessions, cursor, seen = [], None, set()
        while True:
            params = {'limit': 100, 'archived': False, 'sortKey': 'updated_at', 'sortDirection': 'desc'}
            if cursor:
                params['cursor'] = cursor
            page = server.rpc('thread/list', params)
            for thread in page.get('data', []):
                if thread.get('id') and thread.get('cwd') and thread['id'] not in seen:
                    sessions.append(thread)
                    seen.add(thread['id'])
            cursor = page.get('nextCursor')
            if not cursor:
                return sorted(sessions, key=updated, reverse=True)
    except (RunnerError, OSError):
        sessions = stored_sessions(job['codex_home'])
        if sessions:
            return sessions
        raise RunnerError('Cannot list Codex sessions. Open Codex once, or check its local daemon/history.') from None
    finally:
        if server:
            server.close()


def projects(sessions):
    grouped = {}
    for thread in sessions:
        cwd = str(thread.get('cwd', ''))
        if not cwd:
            continue
        group = grouped.setdefault(cwd, {'cwd': cwd, 'count': 0, 'updated': 0})
        group['count'] += 1
        group['updated'] = max(group['updated'], updated(thread))
    return sorted(grouped.values(), key=lambda row: row['updated'], reverse=True)


def register_paused(store, thread, environment, name=None, sandbox='workspace-write'):
    sid = str(uuid.UUID(thread['id']))
    if any(j['id'] == sid for j in store.jobs()):
        return sid
    cwd = str(Path(thread['cwd']).expanduser().resolve(strict=True))
    if not Path(cwd).is_dir():
        raise RunnerError('Project path must be a directory')
    if sandbox not in ('workspace-write', 'danger-full-access', 'read-only'):
        raise RunnerError('Unknown sandbox selection')
    name = validate_name(name) if name else default_name(store, cwd, sid)
    job = dict(id=sid, cwd=cwd, codex=environment['codex'], path=environment.get('path', os.environ.get('PATH', '')),
               codex_home=environment['codex_home'], limit_id=environment.get('limit_id', 'codex'), model=None,
               managed=True, sandbox=sandbox, name=name, status='paused', created=time.time(), updated=time.time(),
               next_run=0, turns=0, prompt=PROMPT, detail='Added from menu; paused until Resume',
               session_title=title(thread), conversation_updated_at=updated(thread))
    probe_codex(job)  # Capability/help check only; no turn or model call.
    store.add(job)
    return sid


def last_context(home, sid):
    """Read the latest context sample, not cumulative session token totals."""
    from .managed import LocalServer
    thread = None
    server = None
    try:
        server = LocalServer(home)
        thread = server.rpc('thread/read', {'threadId': sid, 'includeTurns': False})['thread']
    except (RunnerError, OSError):
        thread = next((v for v in stored_sessions(home) if v['id'] == sid), None)
    finally:
        if server:
            server.close()
    if not thread:
        return None
    path = thread.get('path') or thread.get('rollout_path')
    if not path:
        return None
    base = Path(home).resolve()
    path = Path(path).resolve()
    if not (path.is_relative_to(base / 'sessions') or path.is_relative_to(base / 'archived_sessions')):
        return None
    with path.open('rb') as stream:
        stream.seek(0, 2)
        end = stream.tell()
        stream.seek(max(0, end - 4 * 1024 * 1024))
        data = stream.read()
    for line in reversed(data.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        payload = event.get('payload') or {}
        if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
            continue
        info = payload.get('info') or {}
        last = info.get('last_token_usage') or {}
        window, used = info.get('model_context_window'), last.get('total_tokens')
        if isinstance(window, (int, float)) and window > 0 and isinstance(used, (int, float)) and used >= 0:
            return {'window': window, 'used': used, 'sample_at': event.get('timestamp'),
                    'title': title(thread), 'updated': updated(thread)}
    return None
