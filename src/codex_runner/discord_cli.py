"""CLI setup does not import optional networking packages or reveal credentials."""
import importlib
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

from .cli import RunnerError
from .discord_config import configure, load_config, set_token


def application_info(config):
    request = urllib.request.Request('https://discord.com/api/v10/oauth2/applications/@me',
        headers={'Authorization': 'Bot ' + config['token'],
                 'User-Agent': 'DiscordBot (https://github.com/JaeHeong/codex-runner, 0.8.0)'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            application = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RunnerError('Discord application lookup failed: HTTP ' + str(exc.code)) from None
    query = urllib.parse.urlencode({'client_id': application['id'], 'scope': 'bot applications.commands',
        'permissions': '84992', 'guild_id': str(config['guild_id']), 'disable_guild_select': 'true'})
    return application['id'], 'https://discord.com/oauth2/authorize?' + query


def import_bot():
    try:
        return importlib.import_module('.discord_bot', __package__)
    except ModuleNotFoundError as exc:
        # The optional Debian companion carries the pinned pure-Python library;
        # aiohttp comes from the distribution rather than an unmanaged root pip.
        optional = Path(__file__).resolve().parent.parent / 'optional'
        if optional.is_dir():
            sys.path.insert(0, str(optional))
            try:
                return importlib.import_module('.discord_bot', __package__)
            except ModuleNotFoundError:
                pass
        raise RunnerError('Discord support is missing. APT: install codex-runner-discord. Python: install codex-runner[discord].') from None


def discord_cli(store, args):
    action = args.discord_action
    if action == 'configure':
        configure(store.base, args.guild_id, args.channel_id, args.allowed_user, args.allow_all_users)
        print('Saved private Discord configuration. Set a token with: codex-runner discord token')
    elif action == 'token':
        set_token(store.base)
        print('Token stored privately; its value was not printed.')
    elif action == 'doctor' and getattr(args, 'offline', False):
        import_bot()
        print('Discord libraries OK. No credentials, chat message or network request was used.')
    elif action in ('invite', 'doctor'):
        config = load_config(store.base, require_token=True)
        app, url = application_info(config)
        print('Application ID:', app)
        print('Guild:', config['guild_id'], 'Channel:', config['channel_id'])
        print('Allowed users:', 'all users in this channel' if config.get('allow_all_users') else len(config['allowed_users']))
        print('Invite:', url)
        if action == 'doctor':
            import_bot()
            print('Bot authentication and optional dependencies OK. No chat message or model request was sent.')
    elif action == 'run':
        import_bot().run_bot(store)
    elif action == 'service':
        from .service import manage_service
        manage_service(store, args.action, component='discord', start=not args.no_start)
        if args.action == 'uninstall':
            from .remote import setting
            setting(store, 'discord_questions_enabled', False)
