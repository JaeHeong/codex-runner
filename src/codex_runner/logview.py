"""Readable Codex activity, with no model requests or terminal control sequences."""
import collections
import datetime as dt
import json
from pathlib import Path
import re
import shlex
import sys
import time

ANSI = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]')
LABELS = {'queued': '실행 대기', 'running': '작업 중', 'waiting': '대기 중', 'idle': '응답 종료 · 감시 중',
          'paused': '일시 중지', 'complete': '작업 완료', 'blocked': '입력 필요', 'error': '오류'}


def clean(value):
    value = ANSI.sub('', str(value))
    return ''.join(c for c in value if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127))


def command_text(command):
    try:
        parts = shlex.split(command)
        if len(parts) == 3 and Path(parts[0]).name in ('sh', 'bash', 'zsh', 'dash') and 'c' in parts[1]:
            return parts[2]
    except ValueError:
        pass
    return command


class Tail:
    def __init__(self, lines):
        self.lines = collections.deque(maxlen=max(1, lines))
        self.partial = ''

    def write(self, value):
        parts = (self.partial + value).split('\n')
        self.lines.extend(p + '\n' for p in parts[:-1])
        self.partial = parts[-1]

    def value(self):
        values = list(self.lines)
        if self.partial:
            values.append(self.partial)
        return ''.join(values[-self.lines.maxlen:])


class Renderer:
    def __init__(self, write, cwd='', full=False):
        self.write = write
        self.cwd = cwd
        self.full = full
        self.states = collections.OrderedDict()
        self.active = None
        self.last_error = None
        self.last_plan = None
        self.newline = True

    def out(self, text):
        text = clean(text)
        if text:
            self.write(text)
            self.newline = text.endswith('\n')

    def end(self):
        if not self.newline:
            self.out('\n')
        self.active = None

    def block(self, key, title):
        if self.active != key:
            self.end()
            self.out('\n' + title + '\n')
            self.active = key

    def state(self, item):
        key = item.get('id', '')
        if key not in self.states:
            self.states[key] = {'text': 0, 'output': 0, 'started': False, 'done': False,
                                'muted': False, 'buffer': '', 'phase': item.get('phase'),
                                'visible_lines': 0, 'visible_chars': 0, 'clipped': False}
        state = self.states[key]
        self.states.move_to_end(key)
        while len(self.states) > 2000:
            self.states.popitem(last=False)
        return state

    def tool_output(self, key, state, text):
        if not text or state['clipped']:
            return
        self.block(('command-output', key), '  출력' + (' · ' + state['label'] if state.get('label') else ''))
        visible = text
        if not self.full:
            remaining = max(0, 12 - state['visible_lines'])
            visible = ''.join(text.splitlines(keepends=True)[:remaining])
            visible = visible[:max(0, 4000 - state['visible_chars'])]
        self.out(visible)
        state['visible_lines'] += visible.count('\n')
        state['visible_chars'] += len(visible)
        if len(visible) < len(text):
            self.end()
            self.out('  … 추가 출력 생략 (--full로 전체 보기)\n')
            state['clipped'] = True

    def error(self, message):
        if isinstance(message, dict):
            message = message.get('message', '알 수 없는 오류')
        message = str(message)
        if message != self.last_error:
            self.block(('error', message), '✗ 오류')
            self.out(message + '\n')
            self.last_error = message

    def agent_final(self, text):
        try:
            result = json.loads(text)
        except (ValueError, TypeError):
            return '● Codex', text
        if isinstance(result, dict) and result.get('status') in ('complete', 'continue', 'blocked') and isinstance(result.get('summary'), str):
            label = {'complete': '작업 완료', 'continue': '계속 진행', 'blocked': '입력 또는 외부 조건 필요'}[result['status']]
            return '● Codex · ' + label, result['summary']
        return '● Codex', text

    def item(self, item, done=False, snapshot=False):
        kind, key = item.get('type'), item.get('id', '')
        if kind in ('userMessage', 'reasoning', 'user_message'):
            return
        state = self.state(item)
        if state['done']:
            return
        state['phase'] = item.get('phase') or state['phase']
        if snapshot and not done:
            # Live notifications racing with a history snapshot have no offsets.
            # Reconcile this item's remaining text at completion instead of replaying deltas.
            state['muted'] = True
        if kind == 'agentMessage':
            text = item.get('text', '')
            if done:
                title, formatted = self.agent_final(text)
                if state['buffer'] or formatted != text or (state['phase'] == 'final_answer' and not state['text']):
                    self.block(('agent-final', key), title)
                    self.out(formatted + '\n')
                elif len(text) > state['text']:
                    self.block(('agent', key), title)
                    self.out(text[state['text']:])
                self.end()
            elif text:
                title, formatted = self.agent_final(text)
                if formatted != text or text.lstrip().startswith('{') or state['phase'] == 'final_answer':
                    state['buffer'] = text
                else:
                    self.block(('agent', key), title)
                    self.out(text[state['text']:])
            state['text'] = len(text)
        elif kind == 'commandExecution':
            if not state['started']:
                command = command_text(item.get('command', '(명령 실행)'))
                if not self.full and (len(command.splitlines()) > 6 or len(command) > 1200):
                    command = '\n'.join(command.splitlines()[:6])[:1200] + '\n  … 명령 일부 생략 (--full로 전체 보기)'
                self.block(('command', key), '▶ $ ' + command)
                state['label'] = command.splitlines()[0][:100]
                cwd = item.get('cwd')
                if cwd and cwd != self.cwd:
                    self.out('  경로: ' + cwd + '\n')
                state['started'] = True
            output = item.get('aggregatedOutput') or ''
            if len(output) > state['output']:
                self.tool_output(key, state, output[state['output']:])
                state['output'] = len(output)
            if done:
                self.end()
                if state['clipped'] and output:
                    self.out('  마지막 출력:\n' + '\n'.join(output.splitlines()[-3:])[-1500:] + '\n')
                code = item.get('exitCode')
                duration = item.get('durationMs')
                label = '✓ 완료' if code in (None, 0) and item.get('status') != 'failed' else '✗ 실패'
                detail = (' · exit ' + str(code)) if code is not None else ''
                if duration is not None:
                    detail += f' · {duration / 1000:.1f}s'
                self.out('  └─ ' + label + detail + '\n')
        elif kind == 'fileChange' and done:
            self.block(('files', key), '✎ 파일 변경')
            for change in item.get('changes', []):
                action = change.get('kind', 'update')
                if isinstance(action, dict):
                    action = action.get('type', 'update')
                self.out('  ' + str(action) + '  ' + change.get('path', '') + '\n')
        elif kind in ('mcpToolCall', 'dynamicToolCall', 'webSearch', 'imageView'):
            if not state['started']:
                title = '검색: ' + str(item.get('query') or (item.get('action') or {}).get('query') or '') if kind == 'webSearch' else '도구: ' + str(item.get('server') or item.get('namespace') or '') + '.' + str(item.get('tool') or item.get('name') or kind)
                self.block(('tool', key), '◆ ' + title)
                state['started'] = True
            if done:
                result = item.get('result') or {}
                content = result.get('content', []) if isinstance(result, dict) else []
                for value in content:
                    if value.get('type') == 'text':
                        self.tool_output(key, state, value.get('text', '') + '\n')
                self.end()
                self.out('  └─ ' + ('✗ 실패' if item.get('error') or item.get('status') == 'failed' else '✓ 완료') + '\n')
        state['done'] = done

    def event(self, event):
        method = event.get('method')
        params = event.get('params') or {}
        if method in ('item/started', 'item/completed'):
            self.item(params.get('item') or {}, done=method == 'item/completed')
        elif method in ('item/agentMessage/delta', 'item/commandExecution/outputDelta'):
            key = params.get('itemId', '')
            state = self.state({'id': key})
            if state['done'] or state['muted']:
                return
            delta = params.get('delta', '')
            if method == 'item/agentMessage/delta':
                if state['buffer'] or state['phase'] == 'final_answer' or (not state['text'] and delta.lstrip().startswith('{')):
                    state['buffer'] += delta
                else:
                    self.block(('agent', key), '● Codex')
                    self.out(delta)
                state['text'] += len(delta)
            else:
                self.tool_output(key, state, delta)
                state['output'] += len(delta)
        elif method == 'turn/plan/updated':
            plan = params.get('plan', [])
            if plan != self.last_plan:
                self.block(('plan', str(plan)), '☷ 계획')
                for step in plan:
                    mark = {'completed': '✓', 'inProgress': '→', 'in_progress': '→'}.get(step.get('status'), ' ')
                    self.out('  [' + mark + '] ' + step.get('step', '') + '\n')
                self.last_plan = plan
        elif method == 'error':
            self.error(params.get('error', '알 수 없는 오류'))
        elif method == 'turn/started':
            self.last_error = None
        elif method == 'item/tool/requestUserInput':
            self.block(('questions', str(event.get('id'))), '？ 사용자 입력 필요 · Codex 터미널에서 답변하세요')
            for question in params.get('questions', []):
                self.out(question.get('question', '') + '\n')
                for option in question.get('options') or []:
                    self.out('  • ' + option.get('label', '') + '\n')
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            if turn.get('error'):
                self.error(turn['error'])
            self.end()
        elif event.get('type') in ('item.started', 'item.updated', 'item.completed'):
            item = dict(event.get('item') or {})
            item['type'] = {'agent_message': 'agentMessage', 'command_execution': 'commandExecution',
                            'file_change': 'fileChange', 'mcp_tool_call': 'mcpToolCall',
                            'web_search': 'webSearch'}.get(item.get('type'), item.get('type'))
            for old, new in [('aggregated_output', 'aggregatedOutput'), ('exit_code', 'exitCode')]:
                if old in item:
                    item[new] = item[old]
            self.item(item, done=event['type'] == 'item.completed')
        elif event.get('type') in ('error', 'turn.failed'):
            self.error(event.get('error') or event.get('message', '알 수 없는 오류'))


def job_header(job, runtime=None):
    detail = job.get('detail') or ''
    label = LABELS.get(job['status'], job['status'])
    if detail.startswith('Another turn is active'):
        label, detail = '기존 Codex 작업 진행 중', 'Runner는 작업이 끝나면 자동으로 이어받습니다.'
        if runtime and runtime.get('type') == 'idle':
            label, detail = '자동 인계 대기', '기존 작업이 끝났습니다. Runner가 다음 확인 때 이어받습니다.'
    elif detail.startswith('Account limit ('):
        detail = '계정 사용 제한이 풀렸는지 주기적으로 확인하고 있습니다.'
    elif detail.startswith('Subscription quota unavailable'):
        detail = '사용 한도 복구를 기다리고 있습니다.'
    elif detail.startswith('Codex managed turn '):
        detail = '같은 Codex 세션에서 자동 작업을 실행하고 있습니다.'
    else:
        detail = {'Paused by user': '자동 실행을 일시 중지했습니다.',
                  'Checking subscription quota': '사용 가능한 한도를 확인하고 있습니다.'}.get(detail, detail)
    flags = (runtime or {}).get('activeFlags', [])
    if 'waitingOnUserInput' in flags:
        label, detail = '사용자 입력 대기', 'Codex 터미널에서 질문에 답변해야 작업이 이어집니다.'
    elif 'waitingOnApproval' in flags:
        label, detail = '승인 대기', 'Codex 터미널에서 요청을 확인하세요.'
    text = '\n[Runner] ' + label
    if detail:
        text += ' · ' + detail
    if job.get('status') in ('waiting', 'idle') and job.get('next_run'):
        when = dt.datetime.fromtimestamp(job['next_run']).astimezone().strftime('%m-%d %H:%M:%S %Z')
        text += '\n다음 확인: ' + when
    return clean(text) + '\n'


def managed_view(store, job, lines, follow, output, full=False):
    from .managed import LocalServer
    server = LocalServer(job.get('codex_home') or str(Path.home() / '.codex'))
    tail = Tail(lines)
    renderer = Renderer(tail.write, job['cwd'], full)
    try:
        if follow:
            thread = server.rpc('thread/resume', {'threadId': job['id'], 'excludeTurns': True})
        else:
            thread = server.rpc('thread/read', {'threadId': job['id'], 'includeTurns': False})
        runtime = thread.get('thread', {}).get('status') or {}
        history = server.rpc('thread/items/list', {'threadId': job['id'], 'limit': min(200, max(40, lines)), 'sortDirection': 'desc'})
        for row in reversed(history.get('data', [])):
            item = row.get('item', row)
            done = row.get('completedAtMs') is not None
            renderer.item(item, done=done, snapshot=True)
        renderer.end()
        output.write(tail.value())
        output.write(job_header(store.get(job['id']), runtime))
        output.flush()
        renderer.write = output.write
        previous = (job['status'], job.get('detail'), str(runtime))
        last_check = time.monotonic()
        while follow:
            try:
                event = server.event()
            except TimeoutError:
                event = None
            if event and (event.get('params') or {}).get('threadId') == job['id']:
                if event.get('method') == 'thread/status/changed':
                    runtime = event['params'].get('status') or {}
                renderer.event(event)
                output.flush()
            if time.monotonic() - last_check >= 1:
                current = store.get(job['id'])
                state = (current['status'], current.get('detail'), str(runtime))
                if state != previous:
                    renderer.end()
                    output.write(job_header(current, runtime))
                    output.flush()
                    previous = state
                last_check = time.monotonic()
    finally:
        server.close()


def local_view(store, job, lines, follow, raw, output, full=False):
    directory = store.base / 'jobs' / job['id']
    positions = {}
    renderer = Renderer(output.write, job['cwd'], full)
    previous = None
    first = True
    while True:
        current = store.get(job['id'])
        paths = ([directory / 'runner.log'] if raw else []) + sorted(directory.glob('events-*.jsonl'))[-1:]
        paths += sorted(directory.glob('stderr-*.log'))[-1:] if raw or current['status'] == 'error' else []
        for path in paths:
            if not path.exists():
                continue
            info = path.stat()
            old = positions.get(path)
            position, pending = (old[1], old[2]) if old and old[0] == info.st_ino and old[1] <= info.st_size else (0, b'')
            with path.open('rb') as stream:
                stream.seek(position)
                data = stream.read()
                positions[path] = (info.st_ino, stream.tell(), b'')
            if raw:
                text = data.decode('utf-8', errors='replace')
                if not position:
                    text = ''.join(collections.deque(text.splitlines(keepends=True), maxlen=max(1, lines)))
                if text:
                    output.write(path.name + ':\n' + text)
            elif path.name.startswith('events-'):
                tail = Tail(lines)
                renderer.write = tail.write if not position else output.write
                chunks = (pending + data).split(b'\n')
                positions[path] = (info.st_ino, position + len(data), chunks[-1])
                for line in chunks[:-1]:
                    try:
                        renderer.event(json.loads(line))
                    except (ValueError, UnicodeDecodeError):
                        continue
                if not position:
                    renderer.end()
                    output.write(tail.value())
                renderer.write = output.write
            elif data:
                renderer.end()
                output.write('\n✗ Codex stderr\n' + clean(''.join(data.decode('utf-8', errors='replace').splitlines(keepends=True)[-lines:])))
        state = (current['status'], current.get('detail'))
        if not raw and (first or state != previous):
            renderer.end()
            output.write(job_header(current))
        output.flush()
        previous, first = state, False
        if not follow:
            break
        time.sleep(1)


def show_logs(store, sid, lines=60, follow=False, raw=False, local=False, full=False, output=None):
    output = output or sys.stdout
    job = store.get(sid)
    if not raw:
        output.write(clean('Codex · ' + job['cwd'] + '\n세션: ' + sid + '\n'))
    if job.get('managed') and not raw and not local:
        from .cli import RunnerError
        try:
            managed_view(store, job, lines, follow, output, full)
            return
        except (RunnerError, OSError, ValueError) as exc:
            output.write('\n실시간 기록에 연결할 수 없어 저장된 로그를 표시합니다: ' + clean(exc) + '\n')
    local_view(store, job, lines, follow, raw, output, full)
