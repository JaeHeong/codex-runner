"""Private configuration; credentials never appear in argv or diagnostic output."""
import getpass
import json
import os
from pathlib import Path
import re
import tempfile

from .cli import RunnerError
from .platforms import WINDOWS


def atomic_private(path, text):
    path = Path(path)
    handle, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as out:
            out.write(text)
        if not WINDOWS:
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def snowflake(value):
    text = str(value)
    if not re.fullmatch(r'[1-9][0-9]{14,20}', text):
        raise RunnerError('Discord IDs must be positive numeric snowflake IDs')
    return int(text)


def load_config(base, require_token=False):
    path = Path(base) / 'discord.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise RunnerError('Configure Discord first: codex-runner discord configure --help') from None
    data['guild_id'], data['channel_id'] = snowflake(data.get('guild_id')), snowflake(data.get('channel_id'))
    data['allowed_users'] = [snowflake(v) for v in data.get('allowed_users', [])]
    if not isinstance(data.get('allow_all_users', False), bool):
        raise RunnerError('allow_all_users must be a boolean')
    if not data.get('allow_all_users') and not data['allowed_users']:
        raise RunnerError('Choose allowed users or explicitly enable --allow-all-users')
    token = os.environ.get('CODEX_RUNNER_DISCORD_TOKEN', '').strip()
    token_path = Path(base) / 'discord.token'
    if not token and token_path.is_file():
        if token_path.is_symlink() or (not WINDOWS and token_path.stat().st_mode & 0o077):
            raise RunnerError('Discord token file must be private (chmod 600) and not a symlink')
        token = token_path.read_text(encoding='utf-8').strip()
    if require_token and not token:
        raise RunnerError('Set the token privately: codex-runner discord token')
    data['token'] = token
    return data


def configure(base, guild, channel, users, allow_all):
    if not users and not allow_all:
        raise RunnerError('Choose --allowed-user or explicitly enable --allow-all-users')
    data = {'guild_id': snowflake(guild), 'channel_id': snowflake(channel),
            'allowed_users': [snowflake(v) for v in users], 'allow_all_users': bool(allow_all)}
    atomic_private(Path(base)/'discord.json', json.dumps(data, indent=2) + '\n')


def set_token(base):
    token = getpass.getpass('Discord bot token (hidden): ').strip()
    if not token or any(c.isspace() for c in token):
        raise RunnerError('Enter a nonempty bot token without whitespace')
    atomic_private(Path(base)/'discord.token', token + '\n')


def allowed(config, guild, channel, user):
    return (guild == config['guild_id'] and channel == config['channel_id'] and
            (config.get('allow_all_users') or user in config['allowed_users']))
