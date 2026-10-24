import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

try:
    from codex_runner.discord_bot import RemoteClient, SessionView, QuestionView
    AVAILABLE=True
except ModuleNotFoundError:
    AVAILABLE=False


@unittest.skipUnless(AVAILABLE,'Optional Discord SDK is not installed')
class DiscordUITests(unittest.IsolatedAsyncioTestCase):
    async def test_guild_command_upsert_keeps_other_commands_and_uses_no_privileged_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'guild_id':123456789012345678,'channel_id':234567890123456789,
                    'allowed_users':[],'allow_all_users':True,'token':'not-a-real-token'}
            client=RemoteClient(Path(directory),config)
            async with client:
                with patch.object(client.http,'upsert_guild_command',new_callable=AsyncMock) as upsert:
                    await client.setup_hook()
                    payload=upsert.call_args.args[2]
                    self.assertEqual(payload['name'],'runner')
                    self.assertEqual({c['name'] for c in payload['options']},{'status','limits','resume','pause','send','questions'})
                    self.assertEqual(upsert.call_args.args[1],config['guild_id'])
                    self.assertTrue(client.intents.guilds)
                    self.assertFalse(client.intents.message_content)
                    self.assertFalse(client.intents.members)
                    self.assertFalse(client.intents.presences)
                    client.poll_task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await client.poll_task
                view=SessionView(client,'01a151c8-b419-73f3-8fd2-119ba622ac45')
                self.assertTrue(view.is_persistent())
                self.assertEqual(len(view.children),5)
                self.assertTrue(all(len(b.custom_id)<=100 for b in view.children))
                self.assertTrue(QuestionView(client,'a'*32).is_persistent())

    async def test_foreign_channel_cannot_submit_a_command_or_open_a_modal(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'guild_id':123456789012345678,'channel_id':234567890123456789,
                    'allowed_users':[],'allow_all_users':True,'token':'not-a-real-token'}
            client=RemoteClient(Path(directory),config)
            interaction=type('Interaction',(),{'guild_id':config['guild_id'],'channel_id':1,
                'user':type('User',(),{'id':123})(),'response':AsyncMock()})()
            async with client:
                with patch.object(client.control,'execute') as execute:
                    await client.execute(interaction,'send','session','malicious')
                    execute.assert_not_called()
                    interaction.response.defer.assert_not_called()
                    interaction.response.send_message.assert_awaited_once()
