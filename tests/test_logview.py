import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_runner.cli import Store
from codex_runner.logview import Renderer, Tail, clean, job_header, show_logs


def event(method, **params):
    return {'method': method, 'params': {'threadId': 'session', **params}}


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.view = Renderer(self.output.write, '/project')

    def test_korean_deltas_are_joined_and_completion_is_not_duplicated(self):
        item = {'id': 'a', 'type': 'agentMessage', 'text': '', 'phase': 'commentary'}
        self.view.event(event('item/started', item=item))
        for chunk in ['테스트를 ', '실행', '하겠습니다.']:
            self.view.event(event('item/agentMessage/delta', itemId='a', delta=chunk))
        self.view.event(event('item/completed', item={**item, 'text': '테스트를 실행하겠습니다.'}))
        text = self.output.getvalue()
        self.assertEqual(text.count('테스트를 실행하겠습니다.'), 1)
        self.assertNotIn('item/agentMessage', text)

    def test_internal_events_and_reasoning_are_hidden(self):
        self.view.event(event('item/completed', item={'id': 'r', 'type': 'reasoning', 'content': ['private text']}))
        self.view.event(event('thread/tokenUsage/updated', tokenUsage={'total': 123}))
        self.view.event(event('thread/settings/updated', setting='internal'))
        self.assertEqual(self.output.getvalue(), '')

    def test_late_final_phase_does_not_repeat_already_streamed_text(self):
        self.view.event(event('item/agentMessage/delta', itemId='a', delta='이미 출력한 답변'))
        self.view.event(event('item/completed', item={'id': 'a', 'type': 'agentMessage',
                                                  'text': '이미 출력한 답변', 'phase': 'final_answer'}))
        self.assertEqual(self.output.getvalue().count('이미 출력한 답변'), 1)

    def test_commands_and_streamed_output_are_not_repeated(self):
        item = {'id': 'cmd', 'type': 'commandExecution', 'command': "/usr/bin/zsh -lc 'pytest -q'",
                'cwd': '/other', 'status': 'inProgress'}
        self.view.event(event('item/started', item=item))
        self.view.event(event('item/commandExecution/outputDelta', itemId='cmd', delta='2 passed\n'))
        self.view.event(event('item/completed', item={**item, 'aggregatedOutput': '2 passed\n', 'exitCode': 0, 'durationMs': 1200}))
        text = self.output.getvalue()
        self.assertIn('▶ $ pytest -q', text)
        self.assertIn('경로: /other', text)
        self.assertIn('✓ 완료 · exit 0 · 1.2s', text)
        self.assertEqual(text.count('2 passed'), 1)

    def test_structured_final_result_is_readable_without_json(self):
        item = {'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer', 'text': ''}
        self.view.event(event('item/started', item=item))
        raw = json.dumps({'status': 'complete', 'summary': '배포와 검증을 완료했습니다.'}, ensure_ascii=False)
        for chunk in [raw[:20], raw[20:]]:
            self.view.event(event('item/agentMessage/delta', itemId='final', delta=chunk))
        self.view.event(event('item/completed', item={**item, 'text': raw}))
        text = self.output.getvalue()
        self.assertIn('작업 완료', text)
        self.assertIn('배포와 검증을 완료했습니다.', text)
        self.assertNotIn('"status"', text)
        self.assertNotIn('"summary"', text)

    def test_snapshot_racing_with_live_deltas_does_not_duplicate_text(self):
        item = {'id': 'msg', 'type': 'agentMessage', 'text': '현재 테스트 ', 'phase': 'commentary'}
        self.view.item(item, snapshot=True)
        self.view.event(event('item/agentMessage/delta', itemId='msg', delta='테스트 '))
        self.view.event(event('item/agentMessage/delta', itemId='msg', delta='검증 중'))
        self.view.event(event('item/completed', item={**item, 'text': '현재 테스트 검증 중'}))
        text = self.output.getvalue()
        self.assertEqual(text.count('현재 테스트'), 1)
        self.assertNotIn('테스트 테스트', text)
        self.assertIn('검증 중', text)

    def test_large_output_is_compact_but_keeps_final_diagnostic(self):
        output = ''.join(f'line {n}\n' for n in range(100))
        item = {'id': 'c', 'type': 'commandExecution', 'command': 'test', 'aggregatedOutput': output, 'exitCode': 1}
        self.view.item(item, done=True)
        text = self.output.getvalue()
        self.assertIn('추가 출력 생략', text)
        self.assertIn('line 99', text)
        self.assertNotIn('line 50\n', text)
        self.assertIn('✗ 실패', text)
        full = io.StringIO()
        Renderer(full.write, full=True).item(item, done=True)
        self.assertIn('line 50\n', full.getvalue())
        self.assertNotIn('추가 출력 생략', full.getvalue())

    def test_file_changes_plans_and_errors_are_visible(self):
        self.view.event(event('turn/plan/updated', plan=[{'step': '배포 검증', 'status': 'inProgress'}]))
        self.view.item({'id': 'f', 'type': 'fileChange', 'changes': [{'path': 'app.py', 'kind': {'type': 'update'}}]}, done=True)
        self.view.event(event('error', error={'message': '한도 초과'}))
        self.view.event(event('turn/completed', turn={'error': {'message': '한도 초과'}}))
        text = self.output.getvalue()
        self.assertIn('[→] 배포 검증', text)
        self.assertIn('update  app.py', text)
        self.assertEqual(text.count('한도 초과'), 1)

    def test_native_exec_events_are_supported(self):
        self.view.event({'type': 'item.completed', 'item': {'id': 'cmd', 'type': 'command_execution', 'command': 'make test', 'aggregated_output': 'passed\n', 'exit_code': 0}})
        self.view.event({'type': 'item.completed', 'item': {'id': 'msg', 'type': 'agent_message', 'text': '검증 완료'}})
        self.assertIn('make test', self.output.getvalue())
        self.assertIn('passed', self.output.getvalue())
        self.assertIn('검증 완료', self.output.getvalue())

    def test_terminal_control_sequences_are_removed(self):
        self.assertEqual(clean('\x1b[31mred\x1b[0m\x1b]52;c;secret\x07\x00'), 'red')

    def test_line_tail_uses_readable_lines_not_json_records(self):
        tail = Tail(2)
        for value in ['first\nsec', 'ond\nthi', 'rd']:
            tail.write(value)
        self.assertEqual(tail.value(), 'second\nthird')

    def test_waiting_for_user_input_is_not_reported_as_active_work(self):
        text = job_header({'status': 'waiting', 'detail': 'Another turn is active'},
                          {'type': 'active', 'activeFlags': ['waitingOnUserInput']})
        self.assertIn('사용자 입력 대기', text)
        self.assertIn('Codex 터미널에서 질문에 답변', text)

    def test_input_questions_are_shown_without_rpc_fields(self):
        self.view.event(event('item/tool/requestUserInput', questions=[{'question': '어느 환경에 배포할까요?', 'options': [{'label': '개발'}]}]))
        self.assertIn('어느 환경에 배포할까요?', self.output.getvalue())
        self.assertIn('• 개발', self.output.getvalue())
        self.assertNotIn('requestUserInput', self.output.getvalue())


class LogSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'state')
        self.job = {'id': 'session', 'cwd': '/project', 'managed': False, 'status': 'paused', 'detail': 'Paused by user'}
        self.store.add(self.job)
        self.directory = self.store.base / 'jobs/session'
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'events-1.jsonl'
        self.output = io.StringIO()

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_local_format_and_raw_debugging(self):
        raw = json.dumps(event('item/completed', item={'id': 'a', 'type': 'agentMessage', 'text': '테스트 완료'}), ensure_ascii=False) + '\n'
        self.path.write_text(raw)
        show_logs(self.store, 'session', output=self.output)
        self.assertIn('테스트 완료', self.output.getvalue())
        self.assertNotIn('item/completed', self.output.getvalue())
        raw_output = io.StringIO()
        show_logs(self.store, 'session', raw=True, output=raw_output)
        self.assertIn(raw, raw_output.getvalue())
        self.assertNotIn('[Runner]', raw_output.getvalue())

    def test_follow_handles_partial_json_lines_and_new_attempts(self):
        record = json.dumps(event('item/completed', item={'id': 'a', 'type': 'agentMessage', 'text': '처음 결과'}), ensure_ascii=False).encode()
        self.path.write_bytes(record[:40])
        calls = []
        def tick(_):
            calls.append(1)
            if len(calls) == 1:
                with self.path.open('ab') as f:
                    f.write(record[40:] + b'\n')
            elif len(calls) == 2:
                (self.directory / 'events-2.jsonl').write_text(json.dumps(event('item/completed', item={'id': 'b', 'type': 'agentMessage', 'text': '다음 실행'}), ensure_ascii=False) + '\n')
            else:
                raise KeyboardInterrupt()
        with patch('codex_runner.logview.time.sleep', side_effect=tick), self.assertRaises(KeyboardInterrupt):
            show_logs(self.store, 'session', follow=True, output=self.output)
        self.assertEqual(self.output.getvalue().count('처음 결과'), 1)
        self.assertEqual(self.output.getvalue().count('다음 실행'), 1)

    def test_managed_observer_reads_current_work_without_starting_a_turn(self):
        self.store.patch('session', managed=True)
        calls = []
        class Observer:
            def rpc(inner, method, params):
                calls.append(method)
                if method == 'thread/resume':
                    return {}
                if method == 'thread/items/list':
                    return {'data': [{'item': {'id': 'current', 'type': 'agentMessage', 'text': '현재 배포를 검증합니다.'}, 'completedAtMs': 1}]}
                raise AssertionError('Unexpected mutation ' + method)
            def event(inner):
                if not hasattr(inner, 'sent'):
                    inner.sent = True
                    return event('item/completed', item={'id': 'next', 'type': 'agentMessage', 'text': '배포 검증 완료'})
                raise KeyboardInterrupt()
            def close(inner):
                calls.append('close')
        with patch('codex_runner.managed.LocalServer', return_value=Observer()), self.assertRaises(KeyboardInterrupt):
            show_logs(self.store, 'session', follow=True, output=self.output)
        self.assertIn('현재 배포를 검증합니다.', self.output.getvalue())
        self.assertIn('배포 검증 완료', self.output.getvalue())
        self.assertEqual(calls, ['thread/resume', 'thread/items/list', 'close'])

    def test_offline_managed_server_falls_back_to_saved_events(self):
        self.store.patch('session', managed=True)
        self.path.write_text(json.dumps(event('item/completed', item={'id': 'old', 'type': 'agentMessage', 'text': '저장된 작업'}), ensure_ascii=False) + '\n')
        with patch('codex_runner.managed.LocalServer', side_effect=ConnectionRefusedError('offline')):
            show_logs(self.store, 'session', output=self.output)
        self.assertIn('저장된 작업', self.output.getvalue())
        self.assertIn('저장된 로그', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
