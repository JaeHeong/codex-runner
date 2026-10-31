"""Small terminal session picker; no task is started merely by opening it."""
import curses
import sys
import time
import unicodedata

from .cli import (RunnerError, control_job, credit_lines, read_limits, read_models, reset_credits, update_auto_resume,
                  update_model, update_prompt, use_reset_credit, wake_quota_waits)
from .logview import LABELS, clean

ACTIONS = [('add', '[A Add]'), ('delete', '[D Delete]'), ('logs', '[L Logs]'), ('resume', '[R Resume]'), ('pause', '[P Pause]'),
           ('name', '[N Name]'), ('prompt', '[E Prompt]'), ('model', '[M Model]'), ('times', '[T Times]'),
           ('coupons', '[C Coupons]'), ('quit', '[Q Quit]')]


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def fitted(text, width):
    out, size = [], 0
    for char in clean(text).replace('\n', ' '):
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if size + cells > max(0, width):
            break
        out.append(char)
        size += cells
    return ''.join(out)


def column_widths(width):
    name, status, sid = (18, 10, 12) if width >= 90 else (12, 8, 8) if width >= 70 else (10, 8, 8)
    directory = min(32, max(12, width // 5))
    prompt = max(1, width - name - status - sid - directory - 4)
    return name, status, sid, directory, prompt


def table_row(values, widths):
    cells = []
    for text, width in zip(values, widths):
        text = clean(text).replace('\n', ' ')
        clipped = fitted(text, width)
        if cell_width(text) > width:
            clipped = fitted(text, max(0, width - 1)) + '…'
        cells.append(clipped + ' ' * max(0, width - cell_width(clipped)))
    return ' '.join(cells)


def visible_status(job):
    if job.get('awaiting_input') and job['status'] in ('running', 'waiting'):
        return 'input'
    if job['status'] == 'running' and job.get('managed') and not job.get('managed_turn_id'):
        return 'checking'
    return job['status']


def mouse_action(x, y, rows, buttons, double=False):
    for index, row in rows:
        if y == row:
            return ('open' if double else 'select', index)
    for action, left, right, row in buttons:
        if y == row and left <= x < right:
            return ('action', action)
    return None


def button_layout(width, first_row):
    x, y, buttons = 2, first_row, []
    for action, text in ACTIONS:
        if x + len(text) > width - 2:
            x, y = 2, y + 1
        buttons.append((action, text, x, y))
        x += len(text) + 1
    return buttons


def draw(win, y, x, text, width, attr=0):
    try:
        win.addstr(y, x, fitted(text, width), attr)
    except curses.error:
        pass


def screen(win, store, select_only=False, initial_sid=None, usage=None):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    win.keypad(True)
    win.timeout(500)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        curses.mouseinterval(200)
    except curses.error:
        pass
    selected, cursor, action_index = initial_sid, 0, 2
    default_environment = None
    while True:
        jobs = sorted(store.jobs(), key=lambda j: (j.get('name') or j['id']).casefold())
        if selected:
            cursor = next((i for i, j in enumerate(jobs) if j['id'] == selected), min(cursor, max(0, len(jobs) - 1)))
        cursor = min(max(0, cursor), max(0, len(jobs) - 1))
        selected = jobs[cursor]['id'] if jobs else None
        h, w = win.getmaxyx()
        win.erase()
        if h < 20 or w < 64:
            draw(win, 0, 0, 'Resize terminal to at least 64 columns x 20 rows. Q: quit', w - 1)
            win.refresh()
            if win.getch() in (ord('q'), ord('Q'), 27):
                return None
            continue
        draw(win, 0, 2, 'Codex Runner  |  세션 선택' if select_only else 'Codex Runner  |  세션 관리', w - 4, curses.A_BOLD)
        draw(win, 1, 2, '방향키: 선택  Enter: 실행  마우스: 행 선택 / 아래 버튼 클릭  Q: 닫기', w - 4)
        widths = column_widths(w - 4)
        draw(win, 3, 2, table_row(['NAME', 'STATUS', 'SESSION', 'DIRECTORY', 'PROMPT'], widths), w - 4, curses.A_BOLD)
        visible = h - 17
        offset = max(0, cursor - visible + 1)
        rows, buttons = [], []
        for i in range(offset, min(len(jobs), offset + visible)):
            job = jobs[i]
            y = 4 + i - offset
            text = table_row([job.get('name') or '-', visible_status(job), job['id'][:12],
                              job.get('cwd', ''), job.get('prompt') or ''], widths)
            draw(win, y, 2, text, w - 4, curses.A_REVERSE if i == cursor else 0)
            rows.append((i, y))
        value, environment = None, None
        if jobs:
            job = jobs[cursor]
            environment = job if job.get('codex') and job.get('codex_home') else None
            status = visible_status(job)
            label = '실행 전 상태 확인 중' if status == 'checking' else LABELS.get(status, status)
            draw(win, h - 13, 2, '상태: ' + label + ' · 모델: ' + model_text(job) + ' · 자동 재개: ' + times_text(job) + ' · ' + (job.get('detail') or ''), w - 4)
            draw(win, h - 11, 2, '문구: ' + (job.get('prompt') or ''), w - 4)
        else:
            draw(win, 5, 2, '등록된 세션이 없습니다. A / Add로 기존 Codex 세션을 선택하세요.', w - 4)
            if default_environment is None:
                try:
                    from .catalog import account_job
                    default_environment = account_job()
                except RunnerError:
                    default_environment = {}
            environment = default_environment or None
        if usage and environment:
            usage.request(environment)
            value = usage.get(environment)
        if not select_only:
            from .usage import usage_lines
            if not environment:
                value = {'fetched_at': time.time(), 'limits_error': 'Codex runtime is not configured'}
            panel_width = min(78, w - 4)
            panel_x = w - panel_width - 2
            for i, line in enumerate(usage_lines(value)):
                draw(win, h - 10 + i, panel_x, line, panel_width)
            if jobs:
                from .catalog import date_text
                info = (value or {}).get('context') or {}
                native_title = info.get('title') or job.get('session_title') or ''
                native_time = info.get('updated') or job.get('conversation_updated_at')
                draw(win, h - 12, 2, '최근 대화: ' + date_text(native_time) + ' · ' + native_title, w - 4)
        if select_only:
            draw(win, h - 5, 2, '[Enter 선택]  [Q 취소]  행 더블클릭: 선택', w - 4, curses.A_BOLD)
        else:
            for i, (action, text, x, y) in enumerate(button_layout(w, h - 5)):
                draw(win, y, x, text, w - x - 1, curses.A_REVERSE if i == action_index else 0)
                buttons.append((action, x, x + len(text), y))
        draw(win, h - 2, 2, 'A 추가 · D 등록해제 · N 이름 · E 문구 · M 모델 · T 횟수 · C 쿠폰 · L 로그 · R 재개 · P 중지 · F5 한도갱신', w - 4)
        win.refresh()
        key = win.getch()
        action = None
        if key in (ord('q'), ord('Q'), 27):
            return None
        if key in (curses.KEY_UP, ord('k')):
            cursor = max(0, cursor - 1)
            selected = jobs[cursor]['id'] if jobs else None
        elif key in (curses.KEY_DOWN, ord('j')):
            cursor = min(len(jobs) - 1, cursor + 1)
            selected = jobs[cursor]['id'] if jobs else None
        elif key in (9, curses.KEY_RIGHT):
            action_index = (action_index + 1) % len(ACTIONS)
        elif key == curses.KEY_LEFT:
            action_index = (action_index - 1) % len(ACTIONS)
        elif key in (10, 13, curses.KEY_ENTER):
            if jobs or (not select_only and ACTIONS[action_index][0] in ('add', 'coupons', 'quit')):
                if not select_only and ACTIONS[action_index][0] == 'quit':
                    return None
                return selected if select_only else (ACTIONS[action_index][0], selected)
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
                pressed = state & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_PRESSED)
                hit = mouse_action(x, y, rows, buttons, bool(state & curses.BUTTON1_DOUBLE_CLICKED)) if pressed else None
                if hit:
                    if hit[0] in ('select', 'open'):
                        cursor = hit[1]
                        selected = jobs[cursor]['id']
                        if hit[0] == 'open':
                            return selected if select_only else ('logs', selected)
                    elif hit[0] == 'action':
                        action = hit[1]
            except curses.error:
                pass
        elif not select_only:
            action = {'a': 'add', 'd': 'delete', 'l': 'logs', 'r': 'resume', 'p': 'pause', 'n': 'name', 'e': 'prompt', 'm': 'model', 't': 'times', 'c': 'coupons'}.get(chr(key).lower() if 0 <= key < 256 else '')
        if key == curses.KEY_F5 and usage and environment:
            usage.request(environment, force=True)
        if action == 'quit':
            return None
        if action and (jobs or action in ('add', 'coupons')) and not select_only:
            return action, selected


def interactive():
    if not sys.stdin.isatty() or not sys.stdout.isatty() or not sys.stderr.isatty():
        raise RunnerError('Menu requires an interactive terminal; use status or a short session name')


def pick_session(store):
    interactive()
    return show_screen(store, True)


def show_screen(store, select_only=False, initial_sid=None, usage=None):
    try:
        return curses.wrapper(screen, store, select_only, initial_sid, usage)
    except curses.error as exc:
        raise RunnerError('Cannot open the terminal menu; check TERM or use a session name: ' + str(exc)) from None


def choice_screen(win, title, choices):
    """Searchable project/session/option list; selecting never starts a model."""
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    win.keypad(True)
    win.timeout(-1)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    query, cursor = '', 0
    while True:
        h, w = win.getmaxyx()
        win.erase()
        if h < 12 or w < 40:
            draw(win, 0, 0, 'Resize to at least 40 columns x 12 rows. Esc: cancel', w - 1)
            win.refresh()
            if win.get_wch() == '\x1b':
                return None
            continue
        filtered = [v for v in choices if query.casefold() in (v['label'] + ' ' + v.get('detail', '')).casefold()]
        cursor = min(cursor, max(0, len(filtered) - 1))
        draw(win, 0, 2, title, w - 4, curses.A_BOLD)
        draw(win, 1, 2, '검색: ' + query + '  (글자 입력 / Backspace)', w - 4)
        draw(win, 3, 2, f'{len(filtered)}개 · ↑↓ 선택 / Enter 또는 더블클릭 / Esc 취소', w - 4)
        offset, visible = max(0, cursor - max(1, h - 9) + 1), max(1, h - 9)
        rows = []
        for i in range(offset, min(len(filtered), offset + visible)):
            y = 4 + i - offset
            draw(win, y, 2, filtered[i]['label'], w - 4, curses.A_REVERSE if i == cursor else 0)
            rows.append((i, y))
        if filtered:
            draw(win, h - 4, 2, filtered[cursor].get('detail', ''), w - 4)
        draw(win, h - 2, 2, '[Enter 선택]    [Esc 취소]', w - 4)
        win.refresh()
        key = win.get_wch()
        if key == '\x1b':
            return None
        if key in (curses.KEY_UP,):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN,):
            cursor = min(max(0, len(filtered) - 1), cursor + 1)
        elif key in ('\n', '\r', curses.KEY_ENTER):
            if filtered:
                return filtered[cursor]
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
                hit = mouse_action(x, y, rows, [('choose', 2, 16, h - 2), ('cancel', 17, 32, h - 2)], bool(state & curses.BUTTON1_DOUBLE_CLICKED))
                if not state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
                    continue
                if hit:
                    if hit[0] in ('select', 'open'):
                        cursor = hit[1]
                        if hit[0] == 'open':
                            return filtered[cursor]
                    elif hit[1] == 'cancel':
                        return None
                    elif filtered:
                        return filtered[cursor]
            except curses.error:
                pass
        elif key in ('\b', '\x7f', curses.KEY_BACKSPACE):
            query, cursor = query[:-1], 0
        elif key == '\x15':
            query, cursor = '', 0
        elif isinstance(key, str) and key.isprintable():
            query, cursor = query + key, 0


def choose(title, choices):
    if not choices:
        raise RunnerError('선택할 항목이 없습니다.')
    try:
        return curses.wrapper(choice_screen, title, choices)
    except curses.error as exc:
        raise RunnerError('Cannot show selection: ' + str(exc)) from None


def browse_add(store, source=None):
    from pathlib import Path
    from .catalog import account_job, date_text, load_sessions, projects, register_paused, title, updated
    environment = source if source and source.get('codex') and source.get('codex_home') else account_job()
    print('Codex 프로젝트·세션 기록을 불러오는 중…')
    sessions = load_sessions(environment)
    choices = [{'key': row['cwd'], 'label': f'{date_text(row["updated"])}  {row["count"]:>3} sessions  {row["cwd"]}',
                'detail': row['cwd']} for row in projects(sessions)]
    choices.append({'key': None, 'label': '폴더 경로 직접 입력', 'detail': '선택한 폴더의 기존 Codex 세션을 찾습니다.'})
    project = choose('Add · 프로젝트 폴더 선택 (최근 대화 순)', choices)
    if project is None:
        return None
    cwd = project['key']
    if cwd is None:
        cwd = input('프로젝트 폴더 (빈칸: 취소): ').strip()
        if not cwd:
            return None
    cwd = str(Path(cwd).expanduser().resolve(strict=True))
    existing = {j['id']: j.get('name') or j['id'][:12] for j in store.jobs()}
    options = []
    for thread in sessions:
        if str(Path(thread['cwd']).expanduser().resolve()) != cwd:
            continue
        suffix = ' · 등록됨: ' + existing[thread['id']] if thread['id'] in existing else ''
        options.append({'key': thread, 'label': date_text(updated(thread)) + '  ' + title(thread) + suffix,
                        'detail': thread['id'] + ' · ' + cwd})
    if not options:
        raise RunnerError('이 폴더에 기존 Codex 대화가 없습니다. 먼저 해당 폴더에서 Codex 세션을 만드세요.')
    selected = choose('Add · 최근 대화 시각 / 작업·대화 이름', options)
    if selected is None:
        return None
    thread = selected['key']
    if thread['id'] in existing:
        return thread['id']
    permission = choose('권한 선택 · 등록 후에는 일시 중지, Resume 시 시작', [
        {'key': 'workspace-write', 'label': '프로젝트 파일 수정 (기본)', 'detail': '프로젝트 범위의 파일 변경을 허용합니다.'},
        {'key': 'danger-full-access', 'label': 'Full Access', 'detail': '현재 Linux 계정 권한으로 프로젝트 밖의 작업도 허용합니다.'},
        {'key': 'read-only', 'label': '읽기 전용', 'detail': '파일 변경 없이 검토합니다.'},
    ])
    if permission is None:
        return None
    name = input('Runner 이름 (Enter: 폴더명으로 자동 지정): ').strip() or None
    return register_paused(store, thread, environment, name=name, sandbox=permission['key'])


def delete_registration(store, sid):
    job = store.get(sid)
    if job['status'] == 'running' or job.get('managed_turn_id'):
        raise RunnerError('실행 중인 작업은 먼저 Pause로 중지 확인 후 등록 해제하세요.')
    result = choose('Delete · ' + (job.get('name') or sid[:12]), [
        {'key': 'cancel', 'label': '취소', 'detail': '기존 설정을 유지합니다.'},
        {'key': 'delete', 'label': 'Runner 등록 해제', 'detail': 'Codex 대화 기록과 프로젝트 파일은 유지합니다.'},
    ])
    if result and result['key'] == 'delete':
        store.delete(sid)


def model_text(job):
    if not job.get('model') and not job.get('effort'):
        return '세션 설정'
    return (job.get('model') or '세션 모델') + ' / ' + (job.get('effort') or '세션 effort')


def times_text(job):
    limit = job.get('max_quota_resumes')
    return '무제한' if limit is None else f"{job.get('quota_resumes', 0)}/{limit}회"


def choose_times(store, sid):
    job = store.get(sid)
    picked = choose('Times · 한도 리셋 후 자동 재개 횟수 · 현재 ' + times_text(job), [
        {'key': None, 'label': '무제한 (기본)', 'detail': '완료될 때까지 리셋마다 이어서 작업합니다.'}] + [
        {'key': n, 'label': f'{n}회', 'detail': f'리셋 후 {n}번 이어서 작업하고, 그다음 한도가 떨어지면 일시정지합니다.'} for n in (1, 2, 3)] + [
        {'key': 0, 'label': '0회', 'detail': '한도가 떨어지면 기다리지 않고 바로 일시정지합니다.'},
        {'key': 'custom', 'label': '직접 입력', 'detail': '0 이상의 숫자를 입력합니다.'}])
    if picked is None:
        return
    times = picked['key']
    if times == 'custom':
        value = input('자동 재개 횟수 (빈칸: 취소): ').strip()
        if not value:
            return
        times = int(value)
    update_auto_resume(store, sid, times)


def choose_model(store, sid):
    job = store.get(sid)
    print('Codex 모델 목록을 불러오는 중…')
    models = read_models(job)
    keep, reset = object(), object()
    choices = [{'key': keep, 'label': '모델 유지 · ' + (job.get('model') or '세션의 마지막 설정'), 'detail': 'effort만 바꿉니다.'}]
    choices += [{'key': m['model'], 'label': m['model'] + ('  (Codex 기본)' if m.get('isDefault') else ''),
                 'detail': m.get('description') or m.get('displayName') or ''} for m in models]
    choices.append({'key': reset, 'label': '지정 해제', 'detail': 'Runner가 덮어쓰지 않고 세션의 마지막 모델·effort를 그대로 씁니다.'})
    model = choose('Model · ' + (job.get('name') or sid[:12]) + ' · 다음 턴부터 적용', choices)
    if model is None:
        return
    if model['key'] is reset:
        update_model(store, sid, reset=True)
        return
    name = None if model['key'] is keep else model['key']
    chosen = next((m for m in models if m['model'] == (name or job.get('model'))), None)
    if chosen:
        options = [(e['reasoningEffort'], e.get('description', ''), chosen.get('defaultReasoningEffort'))
                   for e in chosen.get('supportedReasoningEfforts') or []]
    else:
        names = sorted({e['reasoningEffort'] for m in models for e in m.get('supportedReasoningEfforts') or []})
        options = [(e, '', None) for e in names]
    effort = choose('Effort · ' + (name or job.get('model') or '세션 모델'), [
        {'key': keep, 'label': 'effort 유지 · ' + (job.get('effort') or '세션의 마지막 설정'), 'detail': ''}] + [
        {'key': value, 'label': value + ('  (모델 기본)' if value == default else ''), 'detail': detail}
        for value, detail, default in options])
    if effort is None:
        return
    update_model(store, sid, model=name, effort=None if effort['key'] is keep else effort['key'], models=models)


def choose_coupon(store, sid):
    from .catalog import account_job
    job = store.get(sid) if sid else account_job()
    print('리셋 쿠폰을 불러오는 중…')
    count, credits = reset_credits(read_limits(job))
    if not count:
        print('사용할 수 있는 리셋 쿠폰이 없습니다.')
        input('Enter: 메뉴로 돌아가기 ')
        return
    lines = credit_lines(count, credits)
    choices = [{'key': c, 'label': c.get('title') or '리셋 쿠폰', 'detail': line} for c, line in zip(credits, lines)]
    if count > len(credits):
        choices.append({'key': None, 'label': '다음 사용 가능한 쿠폰', 'detail': lines[-1]})
    picked = choose(f'Coupons · 사용 가능 {count}장 · 쓸 쿠폰 선택 (Q: 취소)', choices)
    if picked is None:
        return
    credit = picked['key']
    sure = choose('정말 이 쿠폰을 지금 쓸까요? 되돌릴 수 없습니다', [
        {'key': False, 'label': '취소 (기본)', 'detail': '쿠폰을 쓰지 않고 메뉴로 돌아갑니다.'},
        {'key': True, 'label': '사용 · ' + picked['label'], 'detail': '지금 5시간·주간 한도 중 리셋 대상이 초기화됩니다.'}])
    if not sure or not sure['key']:
        return
    outcome = use_reset_credit(job, credit and credit['id'])
    if outcome == 'reset':
        wake_quota_waits(store)
    print({'reset': '쿠폰을 썼습니다. 한도가 초기화됐습니다.',
           'nothingToReset': '지금은 초기화할 한도가 없어서 쿠폰이 그대로 남아 있습니다.',
           'noCredit': 'Codex에 남은 쿠폰이 없습니다.',
           'alreadyRedeemed': '이미 처리된 사용 요청입니다.'}.get(outcome, 'Codex 응답: ' + str(outcome)))
    input('Enter: 메뉴로 돌아가기 ')


def confirm_completed(decision):
    selected = choose('Resume · 마지막 턴이 이미 끝났습니다', [
        {'key': 'cancel', 'label': '문구 전송 없이 멈추기 (기본)', 'detail': decision.get('preview') or decision['detail']},
        {'key': 'continue', 'label': '확인했습니다 · 이 턴 뒤에 계속 지시하기', 'detail': '저장된 문구를 다음 턴으로 보냅니다. 이미 끝난 작업인지 먼저 확인하세요.'},
    ])
    return bool(selected and selected['key'] == 'continue')


def run_menu(store):
    from .usage import UsagePoller
    interactive()
    focused = None
    usage = UsagePoller()
    try:
        while True:
            selected = show_screen(store, initial_sid=focused, usage=usage)
            if not selected:
                return
            action, sid = selected
            focused = sid
            try:
                if action == 'add':
                    focused = browse_add(store, store.get(sid) if sid else None) or focused
                elif action == 'delete':
                    delete_registration(store, sid)
                elif action == 'logs':
                    from .logview import show_logs
                    print('Ctrl+C: 메뉴로 돌아가기 (Codex 작업은 계속됩니다)')
                    show_logs(store, sid, follow=True)
                elif action == 'prompt':
                    print('현재 문구:\n' + store.get(sid).get('prompt', ''))
                    value = input('새 문구 (빈칸: 취소): ')
                    if value.strip():
                        update_prompt(store, sid, value)
                elif action == 'model':
                    choose_model(store, sid)
                elif action == 'times':
                    choose_times(store, sid)
                elif action == 'coupons':
                    choose_coupon(store, sid)
                elif action == 'name':
                    print('현재 이름: ' + (store.get(sid).get('name') or '(없음)'))
                    value = input('새 이름 (빈칸: 취소): ')
                    if value.strip():
                        store.set_name(sid, value)
                elif action in ('resume', 'pause'):
                    control_job(store, sid, action, confirm=confirm_completed)
            except (KeyboardInterrupt, EOFError):
                pass
            except (RunnerError, OSError, ValueError) as exc:
                print(clean(exc))
                try:
                    input('Enter: 메뉴로 돌아가기 ')
                except (KeyboardInterrupt, EOFError):
                    pass
    finally:
        usage.close()
