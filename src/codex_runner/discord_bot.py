"""Optional Discord gateway client: slash commands, persistent buttons and modals."""
import asyncio
import contextlib
import logging
from pathlib import Path
import time
from typing import Optional

import discord
from discord import app_commands

from .cli import RunnerError, Store
from .discord_config import allowed, load_config
from .discord_control import Control, notifications
from .platforms import lock_file
from .relay import Observers
from .remote import answer_question, can_answer, questions, setting

MENTIONS = discord.AllowedMentions.none()


class MessageModal(discord.ui.Modal, title='Codex에 추가 지시'):
    def __init__(self, client, session):
        super().__init__(timeout=600)
        self.client, self.session = client, session
        self.message = discord.ui.TextInput(label='다음 작업 또는 막힌 질문에 대한 답', style=discord.TextStyle.paragraph,
                                            max_length=4000, required=True)
        self.add_item(self.message)

    async def on_submit(self, interaction):
        await self.client.execute(interaction, 'send', self.session, str(self.message))


class AnswerModal(discord.ui.Modal, title='Codex 질문에 답하기'):
    def __init__(self, client, question):
        super().__init__(timeout=600)
        self.client, self.question = client, question
        self.fields = []
        for q in question['request']['params']['questions']:
            field = discord.ui.TextInput(label=(q.get('header') or q['id'])[:45],
                placeholder='선택지 번호 또는 답변', style=discord.TextStyle.paragraph, max_length=4000, required=True)
            self.fields.append((q, field)); self.add_item(field)

    async def on_submit(self, interaction):
        if not await self.client.authorize(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = {}
        for question, field in self.fields:
            text = str(field).strip()
            options = question.get('options') or []
            if text.isdecimal() and 1 <= int(text) <= len(options):
                text = options[int(text)-1]['label']
            answers[question['id']] = text
        try:
            await asyncio.to_thread(self.client.answer, self.question['id'], answers)
            await interaction.followup.send('답변을 접수했어. 현재 질문이 유효한지 확인한 뒤 Codex에 전달할게.', ephemeral=True, allowed_mentions=MENTIONS)
        except RunnerError as exc:
            await interaction.followup.send(str(exc)[:1800], ephemeral=True, allowed_mentions=MENTIONS)


class SessionView(discord.ui.View):
    def __init__(self, client, sid):
        super().__init__(timeout=None)
        self.client, self.sid = client, sid
        for action, label, style in [('status', '상태', discord.ButtonStyle.secondary),
                                      ('limits', '한도', discord.ButtonStyle.secondary),
                                      ('resume', '재개', discord.ButtonStyle.success),
                                      ('pause', '일시정지', discord.ButtonStyle.danger),
                                      ('send', '추가 지시', discord.ButtonStyle.primary)]:
            button = discord.ui.Button(label=label, style=style, custom_id='cr:1:' + action + ':' + sid)
            async def callback(interaction, selected=action):
                if selected == 'send':
                    if await client.authorize(interaction):
                        await interaction.response.send_modal(MessageModal(client, sid))
                else:
                    await client.execute(interaction, selected, sid)
            button.callback = callback
            self.add_item(button)


class QuestionView(discord.ui.View):
    def __init__(self, client, key):
        super().__init__(timeout=None)
        button = discord.ui.Button(label='질문에 답하기', style=discord.ButtonStyle.primary, custom_id='cr:1:answer:' + key)
        async def callback(interaction):
            if not await client.authorize(interaction):
                return
            q = client.question(key)
            if not q:
                await interaction.response.send_message('이미 처리됐거나 만료된 질문이야.', ephemeral=True)
            elif not can_answer(q):
                await interaction.response.send_message('이 요청은 Codex 터미널에서 확인해야 해.', ephemeral=True)
            else:
                await interaction.response.send_modal(AnswerModal(client, q))
        button.callback = callback
        self.add_item(button)


class RemoteClient(discord.Client):
    def __init__(self, base, config):
        intents = discord.Intents.none(); intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=MENTIONS)
        self.base, self.config = Path(base), config
        self.control = Control(base)
        self.tree = app_commands.CommandTree(self)
        self.observers = Observers(base)
        self.poll_task = None
        self.view_ids = set()

    async def authorize(self, interaction):
        if allowed(self.config, interaction.guild_id, interaction.channel_id, interaction.user.id):
            return True
        await interaction.response.send_message('이 서버·채널 또는 사용자에게는 명령 권한이 없어.', ephemeral=True, allowed_mentions=MENTIONS)
        return False

    def question(self, key):
        store = Store(self.base)
        try:
            return next((q for q in questions(store) if q['id'] == key), None)
        finally:
            store.db.close()

    def answer(self, key, answers):
        store = Store(self.base)
        try:
            answer_question(store, key, answers)
        finally:
            store.db.close()

    async def execute(self, interaction, action, session=None, text=None):
        if not await self.authorize(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        identity = 'discord:' + str(interaction.user.id) + ':' + str(interaction.id)
        try:
            result = await asyncio.to_thread(self.control.execute, action, session, text, identity)
            if action == 'questions':
                if not result:
                    await interaction.followup.send('대기 중인 질문이 없어.', ephemeral=True)
                for q in result[:5]:
                    await interaction.followup.send('질문 · ' + q['request']['method'], view=QuestionView(self, q['id']), ephemeral=True, allowed_mentions=MENTIONS)
                return
            view = SessionView(self, session) if session else None
            await interaction.followup.send(result[:1900], view=view, ephemeral=True, allowed_mentions=MENTIONS)
        except RunnerError as exc:
            # A blocked resume explains its reason and offers an explicit new
            # instruction. It never silently forces a completed/blocked turn.
            await interaction.followup.send(str(exc)[:1800], view=SessionView(self, session) if session else None,
                                             ephemeral=True, allowed_mentions=MENTIONS)
        except Exception as exc:
            await interaction.followup.send('처리 중 오류: ' + type(exc).__name__ + '. 서버 로그를 확인해줘.', ephemeral=True)

    async def setup_hook(self):
        group = app_commands.Group(name='runner', description='Codex Runner 상태와 작업 관리', guild_only=True)
        @group.command(name='status', description='등록된 세션 상태 보기')
        async def status(interaction: discord.Interaction, session: Optional[str] = None):
            await self.execute(interaction, 'status', session)
        @group.command(name='limits', description='Codex 5시간·주간 한도 조회')
        async def limits(interaction: discord.Interaction, session: Optional[str] = None):
            await self.execute(interaction, 'limits', session)
        @group.command(name='resume', description='세션 자동 관찰과 재개 켜기')
        async def resume(interaction: discord.Interaction, session: str):
            await self.execute(interaction, 'resume', session)
        @group.command(name='pause', description='러너 일시정지')
        async def pause(interaction: discord.Interaction, session: str):
            await self.execute(interaction, 'pause', session)
        @group.command(name='send', description='추가 지시를 한 번 전달하고 재개')
        async def send(interaction: discord.Interaction, session: str, message: str):
            await self.execute(interaction, 'send', session, message)
        @group.command(name='questions', description='대기 중인 질문에 답하기')
        async def pending_questions(interaction: discord.Interaction, session: str):
            await self.execute(interaction, 'questions', session)
        async def autocomplete(interaction, current):
            if not allowed(self.config, interaction.guild_id, interaction.channel_id, interaction.user.id):
                return []
            store = Store(self.base)
            try:
                return [app_commands.Choice(name=((j.get('name') or j['id'][:12]) + ' · ' + j['status'])[:100], value=j['id'])
                        for j in store.jobs() if current.lower() in (j.get('name') or j['id']).lower()][:25]
            finally:
                store.db.close()
        for command in group.commands:
            command.autocomplete('session')(autocomplete)
        guild = discord.Object(id=self.config['guild_id'])
        self.tree.add_command(group, guild=guild)
        # Upsert our command group without deleting other guild commands.
        await self.http.upsert_guild_command(self.application_id, self.config['guild_id'], group.to_dict(self.tree))
        self.poll_task = asyncio.create_task(self.poll())

    async def poll(self):
        await self.wait_until_ready()
        store = Store(self.base)
        announced = False
        try:
            while not self.is_closed():
                if (self.base/'discord-stop.request').exists():
                    await self.close(); return
                if not self.is_ready():
                    await asyncio.sleep(2); continue
                try:
                    channel = self.get_channel(self.config['channel_id']) or await self.fetch_channel(self.config['channel_id'])
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config['guild_id']:
                        raise RunnerError('Configured channel must be a text channel in the configured guild')
                    permissions = channel.permissions_for(channel.guild.me)
                    if not (permissions.view_channel and permissions.send_messages and permissions.embed_links and permissions.read_message_history):
                        raise RunnerError('Bot needs View Channel, Send Messages, Embed Links and Read Message History')
                    setting(store, 'discord_questions_enabled', True)
                    setting(store, 'discord_heartbeat', time.time())
                    jobs = store.jobs()
                    self.observers.sync(jobs)
                    for job in jobs:
                        key = 'job:' + job['id']
                        if key not in self.view_ids:
                            self.add_view(SessionView(self, job['id'])); self.view_ids.add(key)
                    for q in questions(store):
                        if q['id'] not in self.view_ids:
                            self.add_view(QuestionView(self, q['id'])); self.view_ids.add(q['id'])
                    if not announced:
                        await channel.send('Codex Runner 연결됨 · `/runner status` · `/runner limits`\nblocked/error/응답 종료와 질문을 이 채널로 알려줄게.', allowed_mentions=MENTIONS)
                        announced = True
                    for event in notifications(store)[:20]:
                        view = QuestionView(self, event['question_id']) if event['question_id'] else SessionView(self, event['session_id'])
                        await channel.send(event['text'][:1900], view=view, allowed_mentions=MENTIONS)
                        setting(store, event['key'], event['fingerprint'])
                except Exception as exc:
                    setting(store, 'discord_heartbeat', 0)
                    print('Discord relay check failed:', str(exc) if isinstance(exc, RunnerError) else type(exc).__name__, flush=True)
                await asyncio.sleep(5)
        finally:
            setting(store, 'discord_heartbeat', 0)
            store.db.close()

    async def close(self):
        await super().close()

    async def on_error(self, event_method, *args, **kwargs):
        # Do not log event bodies, interaction tokens or private message text.
        print('Discord event handling failed:', event_method, flush=True)


def run_bot(store):
    config = load_config(store.base, require_token=True)
    lock = (store.base/'discord.lock').open('a+b')
    try:
        lock_file(lock)
    except BlockingIOError:
        lock.close(); raise RunnerError('A Discord bridge is already running for this state directory') from None
    (store.base/'discord-stop.request').unlink(missing_ok=True)
    import os
    (store.base/'discord.pid').write_text(str(os.getpid()), encoding='ascii')
    client = RemoteClient(store.base, config)
    logging.getLogger('discord').setLevel(logging.WARNING)
    async def main():
        async with client:
            await client.start(config['token'])
    try:
        asyncio.run(main())
    finally:
        client.observers.close()
        setting(store, 'discord_heartbeat', 0)
        (store.base/'discord.pid').unlink(missing_ok=True)
        lock.close()
