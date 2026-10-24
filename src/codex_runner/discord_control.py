"""Provider-independent commands and durable notification fingerprints."""
import hashlib
import json
import time

from .cli import Store, QuotaCache, RunnerError, control_job
from .remote import enqueue_input, pending_input, questions, setting
from .usage import usage_lines


class Control:
    def __init__(self, base):
        self.base = base
        self.quotas = QuotaCache()

    def execute(self, action, reference=None, text=None, identity=None):
        store = Store(self.base)
        try:
            sid = store.resolve(reference) if reference else None
            if action == 'status':
                jobs = [store.get(sid)] if sid else store.jobs()
                return '\n\n'.join(self.status(store, job) for job in jobs) or '등록된 세션이 없어. 서버에서 먼저 세션을 등록해줘.'
            if action == 'limits':
                jobs = [store.get(sid)] if sid else store.jobs()
                if not jobs:
                    raise RunnerError('Register a Codex session on the server first')
                job = jobs[0]
                from .cli import select_limit
                data = self.quotas.read(job)
                value = {'fetched_at': time.time(), 'limits': select_limit(data, job.get('limit_id', 'codex'))}
                return '\n'.join(line for line in usage_lines(value) if not line.startswith('Context window:')) + '\n(계정 전체 한도 · 모델 요청 없이 조회)'
            if not sid:
                raise RunnerError('Choose a registered session')
            if action in ('pause', 'resume'):
                control_job(store, sid, action)
                return self.status(store, store.get(sid))
            if action == 'send':
                result = enqueue_input(store, sid, text or '', identity)
                return '추가 지시 ' + ('접수됨' if result == 'pending' else '이미 접수됨: ' + result) + '\n현재 턴을 중단하지 않고, 안전한 시점과 한도 복구를 기다려 한 번만 전달해.'
            if action == 'questions':
                current = questions(store, sid)
                return current
            raise RunnerError('Unknown remote action')
        finally:
            store.db.close()

    @staticmethod
    def status(store, job):
        name = job.get('name') or job['id'][:12]
        detail = (job.get('detail') or '')[:900]
        pending = pending_input(store, job['id'])
        qs = questions(store, job['id'])
        return (name + ' · ' + ('input' if job.get('awaiting_input') else job['status']) + '\n' + detail +
                ('\n추가 지시 대기 중' if pending else '') +
                ('\n확인할 요청: ' + str(len(qs)) + '개' if qs else ''))


def notifications(store):
    """Return candidates; callers acknowledge only after Discord accepts a send."""
    result = []
    jobs = {j['id']: j for j in store.jobs()}
    for sid, job in jobs.items():
        detail = job.get('detail') or ''
        quota_wait = job['status'] == 'waiting' and any(v in detail for v in (
            'Account limit (', 'Subscription quota unavailable', 'usageLimitExceeded'))
        if job['status'] not in ('blocked', 'error', 'complete', 'idle', 'paused') and not quota_wait:
            # Clear the terminal-state fingerprint when real work resumes, so
            # the same blocker in a later attempt generates a new alert.
            if job['status'] == 'waiting' and 'Another turn is active' in detail:
                setting(store, 'discord:job:' + sid, '')
            continue
        key = 'discord:job:' + sid
        identity = [job['status'], job.get('idle_turn_id'), job.get('summary'),
                    detail.split('; next check')[0] if quota_wait else detail]
        fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if setting(store, key) != fingerprint:
            result.append({'key': key, 'fingerprint': fingerprint, 'session_id': sid,
                           'question_id': None, 'text': Control.status(store, job)})
    for q in questions(store):
        if q['status'] != 'pending' or q['session_id'] not in jobs:
            continue
        key = 'discord:question:' + q['id']
        if setting(store, key) == 'sent':
            continue
        params = q['request'].get('params') or {}
        text = (jobs[q['session_id']].get('name') or q['session_id'][:12]) + ' · 입력 요청\n'
        if q['request']['method'] == 'item/tool/requestUserInput':
            for index, question in enumerate(params.get('questions') or [], 1):
                text += '\n' + str(index) + '. ' + question.get('question', '')[:700]
                for number, option in enumerate(question.get('options') or [], 1):
                    text += '\n  ' + str(number) + ') ' + option.get('label', '')[:150]
        else:
            text += '승인 또는 별도 입력이 필요해. Codex 터미널에서 확인해줘.\n' + q['request']['method']
        result.append({'key': key, 'fingerprint': 'sent', 'session_id': q['session_id'],
                       'question_id': q['id'], 'text': text})
    return result
