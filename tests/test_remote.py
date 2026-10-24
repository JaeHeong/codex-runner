import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_managed
from codex_runner.cli import RunnerError, Store
from codex_runner.discord_config import allowed, configure, load_config, set_token
from codex_runner.discord_control import notifications
from codex_runner.remote import (answer_question, begin_delivery, can_answer, confirm_delivery,
    enqueue_input, expire_turn, pending_input, questions, record_question, resolve_question, send_answer, setting)
from codex_runner.relay import RuntimeObserver


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_managed.ManagedTests()
        self.fixture.setUp()
        self.store, self.job = self.fixture.store, self.fixture.job

    def tearDown(self):
        self.fixture.tearDown()

    def question(self, key=7, secret=False, thread=None):
        return {'id': key, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId': thread or self.job['id'], 'turnId': 'live-turn', 'itemId': 'item', 'isBlocking': True,
            'questions': [{'id': 'which', 'header': '방법', 'question': 'private value' if secret else '어떤 방법?',
                           'isSecret': secret, 'options': [{'label': '추천', 'description': '권장'}]}]}}

    def test_explicit_followup_unblocks_completed_blocker_and_is_one_shot(self):
        self.fixture.latest_turn = copy.deepcopy(self.fixture.turn)
        self.fixture.latest_turn['items'][0]['text'] = json.dumps({'status': 'blocked', 'summary': 'Need input'})
        self.assertEqual(self.fixture.run_job()['status'], 'blocked')
        enqueue_input(self.store, self.job['id'], '검증은 보류하고 나머지 진행', 'interaction-1')
        self.assertEqual(self.fixture.run_job()['status'], 'complete')
        starts = [p for m,p in self.fixture.calls if m == 'turn/start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['input'][0]['text'], '검증은 보류하고 나머지 진행')
        self.assertEqual(self.store.get(self.job['id'])['prompt'], 'authorized task')
        self.assertIsNone(pending_input(self.store, self.job['id']))
        self.fixture.run_job()
        self.assertEqual(len([m for m,p in self.fixture.calls if m == 'turn/start']), 1)

    def test_duplicate_interaction_never_queues_a_second_turn(self):
        for _ in range(2):
            self.assertEqual(enqueue_input(self.store, self.job['id'], 'task', 'same'), 'pending')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM remote_inputs').fetchone()[0], 1)
        with self.assertRaises(RunnerError):
            enqueue_input(self.store, self.job['id'], 'different task', 'same')
        self.assertEqual(self.fixture.run_job()['status'], 'complete')
        self.assertEqual(enqueue_input(self.store, self.job['id'], 'task', 'same'), 'sent')
        self.assertEqual(self.store.get(self.job['id'])['status'], 'complete')

    def test_active_native_turn_and_quota_hold_followup_without_sending(self):
        enqueue_input(self.store, self.job['id'], 'later', 'later-1')
        self.fixture.thread_status = 'active'
        self.assertEqual(self.fixture.run_job()['status'], 'waiting')
        self.assertEqual(pending_input(self.store, self.job['id'])['text'], 'later')
        self.fixture.thread_status = 'idle'
        self.fixture.worker.capacity = lambda *a, **kw: 9999999999
        self.assertEqual(self.fixture.run_job()['status'], 'waiting')
        self.assertFalse(any(m in ('turn/start','turn/interrupt') for m,p in self.fixture.calls))
        self.fixture.worker.capacity = lambda *a, **kw: None
        self.assertEqual(self.fixture.run_job()['status'], 'complete')

    def test_lost_start_response_is_not_replayed_by_resume(self):
        enqueue_input(self.store, self.job['id'], 'once only', 'once')
        original = self.fixture.rpc
        def rpc(method, params):
            if method == 'turn/start':
                self.fixture.calls.append((method, params))
                raise RunnerError('lost response')
            return original(method, params)
        self.fixture.server.rpc = rpc
        self.assertEqual(self.fixture.run_job()['status'], 'error')
        self.assertTrue(self.store.get(self.job['id'])['managed_start_pending'])
        self.assertEqual(self.store.db.execute("SELECT status FROM remote_inputs WHERE id='once'").fetchone()[0], 'dispatching')
        self.store.patch(self.job['id'], status='queued')
        self.assertEqual(self.fixture.run_job()['status'], 'error')
        self.assertEqual(len([m for m,p in self.fixture.calls if m == 'turn/start']), 1)

    def test_followups_arriving_during_owned_turn_are_queued_after_finish(self):
        enqueue_input(self.store, self.job['id'], 'first', 'first')
        original = self.fixture.event
        added = []
        def event():
            if not added:
                enqueue_input(self.store, self.job['id'], 'second', 'second'); added.append(True)
            return original()
        self.fixture.server.event = event
        self.assertEqual(self.fixture.run_job()['status'], 'queued')
        self.assertEqual(pending_input(self.store, self.job['id'])['text'], 'second')
        self.assertEqual(self.fixture.run_job()['status'], 'complete')
        self.assertEqual([p['input'][0]['text'] for m,p in self.fixture.calls if m == 'turn/start'], ['first','second'])

    def test_delete_cancels_unsent_inputs_and_does_not_replay_after_readding(self):
        enqueue_input(self.store, self.job['id'], 'old', 'old')
        self.store.delete(self.job['id'])
        self.store.add(self.job)
        self.assertIsNone(pending_input(self.store, self.job['id']))

    def test_question_reply_is_typed_and_claimed_once_on_live_connection(self):
        request = self.question()
        key = record_question(self.store, self.job, request)
        answer_question(self.store, key, {'which': '추천'})
        q = questions(self.store)[0]
        sent=[]
        server=type('Server', (), {'send': lambda self, message: sent.append(message)})()
        self.assertFalse(send_answer(self.store, server, q, set()))
        self.assertTrue(send_answer(self.store, server, q, {key}))
        self.assertFalse(send_answer(self.store, server, q, {key}))
        self.assertEqual(sent, [{'id':7,'result':{'answers':{'which':{'answers':['추천']}}}}])
        resolve_question(self.store, self.job, 7)
        self.assertFalse(questions(self.store))
        with self.assertRaises(RunnerError):answer_question(self.store,key,{'which':'late'})

    def test_replayed_unresolved_reply_requires_a_fresh_human_answer(self):
        request=self.question()
        key=record_question(self.store,self.job,request)
        answer_question(self.store,key,{'which':'first answer'})
        server=type('Server',(),{'send':lambda self,message:None})()
        self.assertTrue(send_answer(self.store,server,questions(self.store)[0],{key}))
        record_question(self.store,self.job,request,replay=True)
        q=questions(self.store)[0]
        self.assertEqual(q['status'],'pending')
        self.assertIsNone(q['answer'])
        self.assertFalse(send_answer(self.store,server,q,{key}))
        answer_question(self.store,key,{'which':'confirmed again'})
        self.assertTrue(send_answer(self.store,server,questions(self.store)[0],{key}))

    def test_online_relay_keeps_native_question_and_uses_its_existing_request(self):
        setting(self.store,'discord_questions_enabled',True)
        setting(self.store,'discord_heartbeat',0)  # A reconnect must not interrupt the turn.
        request=self.question();request['params']['turnId']='turn-owned'
        original=self.fixture.event
        seen=[]
        def event():
            if not seen:
                seen.append(True);return request
            self.assertTrue(questions(self.store))
            self.assertTrue(self.store.get(self.job['id'])['awaiting_input'])
            resolve_question(self.store,self.job,7)
            return original()
        self.fixture.server.event=event
        self.assertEqual(self.fixture.run_job()['status'],'complete')
        self.assertFalse(self.store.get(self.job['id'])['awaiting_input'])
        self.assertFalse(any(m=='turn/interrupt' for m,p in self.fixture.calls))

    def test_secret_and_approval_requests_cannot_be_answered_over_chat(self):
        key=record_question(self.store,self.job,self.question(secret=True))
        q=questions(self.store)[0]
        self.assertNotIn('private value',json.dumps(q))
        self.assertFalse(can_answer(q))
        with self.assertRaises(RunnerError):answer_question(self.store,key,{'which':'secret'})
        request=self.question(key=8);request['method']='item/commandExecution/requestApproval'
        key=record_question(self.store,self.job,request)
        with self.assertRaises(RunnerError):answer_question(self.store,key,{'which':'accept'})

    def test_expired_turn_and_wrong_thread_cannot_receive_answers(self):
        with self.assertRaises(RunnerError):record_question(self.store,self.job,self.question(thread='unrelated'))
        key=record_question(self.store,self.job,self.question())
        expire_turn(self.store,self.job['id'],'live-turn')
        with self.assertRaises(RunnerError):answer_question(self.store,key,{'which':'stale'})

    def test_notifications_ack_after_send_and_persist_deduplication(self):
        self.store.patch(self.job['id'], status='error', detail='Need repair')
        first=notifications(self.store)
        self.assertEqual(len(first),1)
        self.assertEqual(notifications(self.store),first)
        setting(self.store,first[0]['key'],first[0]['fingerprint'])
        self.assertFalse(notifications(self.store))
        self.store.patch(self.job['id'],status='blocked',detail='Need answer')
        self.assertEqual(len(notifications(self.store)),1)

    def test_observer_replays_live_native_and_verified_child_questions(self):
        request=self.question(thread='child')
        owner=self
        stop=threading.Event()
        class Server:
            pending=[]
            def __init__(self): self.calls=[];self.sent=[];self.events=0
            def rpc(self,method,params):
                self.calls.append(method)
                if method=='thread/list':return {'data':[{'id':'child','parentThreadId':owner.job['id']},{'id':'unrelated','parentThreadId':'someone-else'}]}
                if method=='thread/resume':return {'thread':{'id':params['threadId']}}
                raise AssertionError(method)
            def event(self):
                self.events+=1
                if self.events==1:return request
                if self.events==2:
                    answer_question(owner.store,questions(owner.store)[0]['id'],{'which':'추천'});return None
                if self.events==3:return {'method':'serverRequest/resolved','params':{'threadId':'child','requestId':7}}
                stop.set();return None
            def send(self,message):self.sent.append(message)
        observer=object.__new__(RuntimeObserver)
        observer.base,observer.home,observer.stop=self.store.base,str(Path.home()/'.codex'),stop
        server=Server();observer.connected(self.store,server)
        self.assertEqual(len(server.sent),1)
        self.assertEqual(server.calls.count('thread/resume'),2)
        self.assertFalse(questions(self.store))
        self.assertFalse(any(m in ('turn/start','turn/interrupt') for m in server.calls))


class ConfigTests(unittest.TestCase):
    def test_authorization_is_guild_channel_and_user_scoped_even_for_buttons(self):
        config={'guild_id':123456789012345678,'channel_id':234567890123456789,'allowed_users':[123],'allow_all_users':False}
        self.assertTrue(allowed(config,config['guild_id'],config['channel_id'],123))
        self.assertFalse(allowed(config,config['guild_id'],config['channel_id'],124))
        config['allow_all_users']=True
        self.assertTrue(allowed(config,config['guild_id'],config['channel_id'],124))
        self.assertFalse(allowed(config,config['guild_id']+1,config['channel_id'],123))
        self.assertFalse(allowed(config,config['guild_id'],config['channel_id']+1,123))
        self.assertFalse(allowed(config,None,config['channel_id'],123))

    def test_private_token_storage_and_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            with self.assertRaises(RunnerError):configure(base,'bad','bad',[],True)
            with self.assertRaises(RunnerError):configure(base,'123456789012345678','234567890123456789',[],False)
            configure(base,'123456789012345678','234567890123456789',[],True)
            with patch('getpass.getpass',return_value='sample-private-token'):
                set_token(base)
            with patch.dict(os.environ,{'CODEX_RUNNER_DISCORD_TOKEN':''}):
                self.assertEqual(load_config(base,True)['token'],'sample-private-token')
                if os.name!='nt':
                    self.assertEqual((base/'discord.token').stat().st_mode & 0o777,0o600)
                    (base/'discord.token').chmod(0o644)
                    with self.assertRaises(RunnerError):load_config(base,True)
