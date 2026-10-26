#!/usr/bin/python3
"""Deterministic fake CLI: no network, credentials or model calls."""
import json
import os
from pathlib import Path
import sys
import time

state = Path(os.environ['FAKE_CODEX_STATE'])
def load():
    return json.loads(state.read_text())

if '--help' in sys.argv:
    print('--json --output-schema --output-last-message')
elif sys.argv[1] == 'app-server':
    for line in sys.stdin:
        msg = json.loads(line)
        if 'id' not in msg:
            continue
        method = msg['method']
        if method == 'account/read':
            result = {'account': {'type': load().get('auth_type', 'chatgpt')}}
        elif method == 'account/rateLimits/read':
            data = load()
            snapshot = {'limitId': 'codex', 'primary': {'usedPercent': data.get('used', 0),
                        'windowDurationMins': 300, 'resetsAt': data.get('reset', time.time() + 90)},
                        'secondary': data.get('secondary'), 'rateLimitReachedType': data.get('limit_type')}
            result = {'rateLimits': snapshot}
        else:
            result = {}
        print(json.dumps({'id': msg['id'], 'result': result}), flush=True)
else:
    data = load()
    data['mode'] = data.get('modes', {}).get(sys.argv[-2], data.get('mode'))
    # Windows append handles do not serialize two independent fixture writers.
    # Use the same cross-platform file lock as the daemon for this audit stream.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
    from codex_runner.platforms import lock_file
    with state.with_suffix('.calls.lock').open('a+b') as lock:
        while True:
            try:
                lock_file(lock)
                break
            except BlockingIOError:
                time.sleep(.01)
        with state.with_suffix('.calls').open('a', encoding='utf-8') as out:
            out.write(json.dumps(sys.argv[1:]) + '\n')
    if data.get('mode') == 'quota_error':
        data['used'] = 100
        state.write_text(json.dumps(data))
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Usage limit reached'}}))
        sys.exit(1)
    if data.get('mode') == 'credit_error':
        data['limit_type'] = 'workspace_owner_credits_depleted'
        state.write_text(json.dumps(data))
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Your workspace is out of credits'}}))
        sys.exit(1)
    if data.get('mode') == 'overloaded':
        print(json.dumps({'type': 'thread.started', 'thread_id': sys.argv[-2]}))
        print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Selected model is at capacity. Please try a different model.'}}))
        sys.exit(1)
    if data.get('mode') == 'error':
        sys.exit(1)
    if data.get('mode') == 'sleep':
        time.sleep(120)
    sid = sys.argv[-2]
    print(json.dumps({'type': 'thread.started', 'thread_id': data.get('wrong_id', sid)}), flush=True)
    result = {'status': data.get('result', 'complete'), 'summary': 'Verified fake result'}
    output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
    output.write_text(json.dumps(result))
    print(json.dumps({'type': 'turn.completed', 'usage': {}}), flush=True)
