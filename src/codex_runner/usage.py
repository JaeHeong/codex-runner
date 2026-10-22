"""Background, read-only quota display for the terminal menu."""
import datetime as dt
import queue
import threading
import time

from .catalog import last_context
from .cli import RunnerError, read_limits, select_limit


def identity(job):
    return tuple(job.get(k) for k in ('id', 'codex', 'codex_home', 'limit_id'))


def snapshot(job):
    result = {'fetched_at': time.time()}
    try:
        result['limits'] = select_limit(read_limits(job), job.get('limit_id', 'codex'))
    except (RunnerError, OSError, ValueError) as exc:
        result['limits_error'] = str(exc)
    if job.get('id'):
        try:
            result['context'] = last_context(job['codex_home'], job['id'])
        except Exception as exc:
            result['context_error'] = str(exc)
    return result


class UsagePoller:
    def __init__(self, interval=30):
        self.interval = interval
        self.cache = {}
        self.requested = {}
        self.requests = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, job, force=False):
        key = identity(job)
        now = time.monotonic()
        if not force and now - self.requested.get(key, -self.interval) < self.interval:
            return
        try:
            self.requests.put_nowait(dict(job))
        except queue.Full:
            try:
                removed = self.requests.get_nowait()
                self.requested.pop(identity(removed), None)
            except queue.Empty:
                pass
            try:
                self.requests.put_nowait(dict(job))
            except queue.Full:
                return
        self.requested[key] = now

    def get(self, job):
        return self.cache.get(identity(job))

    def _run(self):
        while not self.stop.is_set():
            try:
                job = self.requests.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                value = snapshot(job)
                old = self.cache.get(identity(job), {})
                if value.get('limits_error') and old.get('limits'):
                    value['limits'] = old['limits']
                    value['limits_at'] = old.get('limits_at', old['fetched_at'])
                else:
                    value['limits_at'] = value['fetched_at']
                self.cache[identity(job)] = value
            except Exception as exc:
                self.cache[identity(job)] = {'fetched_at': time.time(), 'limits_error': str(exc)}

    def close(self):
        self.stop.set()
        self.thread.join(timeout=.2)


def reset_text(timestamp, weekly=False):
    if timestamp is None:
        return 'reset unknown'
    value = dt.datetime.fromtimestamp(timestamp).astimezone()
    if weekly or value.date() != dt.datetime.now().astimezone().date():
        return 'resets ' + value.strftime('%H:%M on %d %b')
    return 'resets ' + value.strftime('%H:%M')


def limit_line(label, window, weekly=False):
    if not window or window.get('usedPercent') is None:
        return label + ': N/A'
    left = max(0, min(100, 100 - float(window['usedPercent'])))
    blocks = round(left / 5)
    bar = '█' * blocks + '░' * (20 - blocks)
    return f'{label}: [{bar}] {left:.0f}% left ({reset_text(window.get("resetsAt"), weekly)})'


def usage_lines(value):
    if value is None:
        return ['Codex usage · 조회 중…  F5: 새로고침', 'Context window: 조회 중…', '5h limit: 조회 중…', 'Weekly limit: 조회 중…']
    when = dt.datetime.fromtimestamp(value.get('limits_at', value['fetched_at'])).astimezone().strftime('%H:%M:%S')
    warning = ' · cached / 조회 오류' if value.get('limits_error') and value.get('limits') else ' · 조회 오류' if value.get('limits_error') else ''
    state = value.get('limits') or {}
    if state.get('rateLimitReachedType') or state.get('spendControlReached'):
        warning += ' · 계정 사용 제한'
    lines = ['Codex usage · ' + when + warning + ' · F5 refresh']
    context = value.get('context')
    if context:
        left = max(0, min(100, 100 * (1 - context['used'] / context['window'])))
        lines.append(f'Context window: ~{left:.0f}% left ({context["used"] / 1000:.0f}K used / {context["window"] / 1000:.0f}K)')
    else:
        lines.append('Context window: N/A (사용량 기록 없음)')
    limits = value.get('limits') or {}
    primary = limits.get('primary') or {}
    minutes = primary.get('windowDurationMins')
    label = '5h limit' if minutes in (None, 300) else f'{minutes}m limit'
    lines.append(limit_line(label, primary))
    lines.append(limit_line('Weekly limit', limits.get('secondary'), True))
    return lines
