"""Single-channel, allowlisted-participant AI chat bot. Run exactly one instance."""
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import discord
from discord import app_commands
from dotenv import load_dotenv
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('discord_ai_chat')


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    channel_id: int
    allowed_ids: frozenset[int]
    owner_id: int
    api_key: str
    model: str
    interval: int
    enabled: bool
    max_responses: int = 1
    skip_low_content: bool = True
    min_interval: int = 10
    max_interval: int = 120

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / '.env')
        def required(name):
            value = os.getenv(name, '').strip()
            if not value:
                raise ValueError(f'Missing {name}')
            return value
        def positive(name):
            value = int(required(name))
            if value <= 0:
                raise ValueError(f'{name} must be positive')
            return value
        ids = required('ALLOWED_USER_IDS').split(',')
        if not all(x.strip().isascii() and x.strip().isdecimal() for x in ids):
            raise ValueError('ALLOWED_USER_IDS must contain comma-separated positive numeric IDs')
        allowed = frozenset(int(x.strip()) for x in ids)
        if not allowed or min(allowed) <= 0:
            raise ValueError('ALLOWED_USER_IDS must contain at least one positive numeric ID')
        enabled = os.getenv('BOT_ENABLED', 'true').lower().strip()
        if enabled not in ('true', 'false'):
            raise ValueError('BOT_ENABLED must be true or false')
        interval = int(os.getenv('SCAN_INTERVAL_SECONDS', '30'))
        if interval <= 0:
            raise ValueError('SCAN_INTERVAL_SECONDS must be positive')
        minimum_interval = int(os.getenv('MIN_SCAN_INTERVAL_SECONDS', '10'))
        maximum_interval = int(os.getenv('MAX_SCAN_INTERVAL_SECONDS', '120'))
        if not 0 < minimum_interval <= maximum_interval:
            raise ValueError('Require 0 < MIN_SCAN_INTERVAL_SECONDS <= MAX_SCAN_INTERVAL_SECONDS')
        maximum = int(os.getenv('MAX_RESPONSES_PER_SCAN', '1'))
        if maximum <= 0:
            raise ValueError('MAX_RESPONSES_PER_SCAN must be positive')
        skip = os.getenv('SKIP_LOW_CONTENT_MESSAGES', 'true').strip().lower()
        if skip not in ('true', 'false'):
            raise ValueError('SKIP_LOW_CONTENT_MESSAGES must be true or false')
        return cls(required('DISCORD_BOT_TOKEN'), positive('DISCORD_GUILD_ID'),
                   positive('DISCORD_CHANNEL_ID'), allowed, positive('OWNER_USER_ID'),
                   required('GEMINI_API_KEY'), os.getenv('GEMINI_MODEL', '').strip()
                   or 'gemini-flash-latest', interval, enabled == 'true', maximum, skip == 'true',
                   minimum_interval, maximum_interval)


class AdaptiveHeartbeat:
    """Learn human message gaps, ignoring re-read messages and bot traffic."""
    def __init__(self, config):
        self.minimum, self.maximum = config.min_interval, config.max_interval
        self.fallback = self.clamp(config.interval)
        self.reset()

    def clamp(self, seconds):
        return max(self.minimum, min(self.maximum, seconds))

    def reset(self):
        self.last_id = None
        self.last_timestamp = None
        self.average_gap = None
        self.activity_at = time.monotonic()

    def observe(self, message_id, timestamp):
        if self.last_id is not None and message_id <= self.last_id:
            return
        if self.last_timestamp is not None:
            gap = self.clamp(max(0, timestamp - self.last_timestamp))
            self.average_gap = gap if self.average_gap is None else .35 * gap + .65 * self.average_gap
        self.last_id, self.last_timestamp = message_id, timestamp
        self.activity_at = time.monotonic()

    def delay(self):
        target = self.fallback if self.average_gap is None else self.average_gap
        idle = max(0, time.monotonic() - self.activity_at)
        return self.clamp(max(target, idle / 2))


class State:
    def __init__(self, path, default_enabled):
        self.path = Path(path)
        self.enabled = default_enabled
        self.last_seen_message_id = None
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError('state must be an object')
            if isinstance(data.get('enabled'), bool):
                self.enabled = data['enabled']
            value = data.get('last_seen_message_id')
            if type(value) is int and value > 0:
                self.last_seen_message_id = value
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            log.warning('State unavailable or malformed; a fresh baseline will be used')

    def save(self, enabled, message_id):
        # Write, fsync, and atomically replace before changing in-memory state.
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as file:
            json.dump({'enabled': enabled, 'last_seen_message_id': message_id}, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.path)
        self.enabled, self.last_seen_message_id = enabled, message_id


def low_content(text):
    """Conservative filter: never discard substantive text mixed with laughter."""
    words = re.findall(r"[^\W_]+", text.casefold())
    if not words:
        return True
    if all(re.fullmatch(r'(?:k{2,}|(?:ha){2,}|(?:he){2,}|(?:rs){2,}|lol|lmao|xd)', w)
           for w in words):
        return True
    if len(words) == 1:
        word = words[0]
        if word in {'ok', 'blz', 'aham', 'uhum'}:
            return True
        # Common keyboard rows, at least six characters; avoid guessing arbitrary words.
        return len(word) >= 6 and bool(re.fullmatch(r'(?:asdf|sdfg)[asdfghjkl]*|qwer[qwertyuiop]*|zxcv[zxcvbnm]*|(?:asd|qwe){2,}', word))
    return False


def split_message(text, limit=2000):
    # Discord measures UTF-16 code units; emoji may occupy two units.
    while text:
        units = 0
        end = 0
        for char in text:
            size = len(char.encode('utf-16-le')) // 2
            if units + size > limit:
                break
            units += size
            end += 1
        if end < len(text):
            boundary = max(text.rfind('\n', 0, end), text.rfind(' ', 0, end))
            if boundary > end // 2:
                end = boundary + 1
        yield text[:end]
        text = text[end:]


def sources_from(response):
    sources = []
    for candidate in response.candidates or []:
        metadata = candidate.grounding_metadata
        if not metadata:
            continue
        for chunk in metadata.grounding_chunks or []:
            web = chunk.web
            if web and web.uri and urlsplit(web.uri).scheme == 'https':
                if web.uri not in sources:
                    sources.append(web.uri)
    return sources[:3]


class Gemini:
    def __init__(self, config):
        self.model = config.model
        self.client = genai.Client(api_key=config.api_key,
                                   http_options=types.HttpOptions(timeout=60000))
        prompt_path = Path(os.getenv('SYSTEM_PROMPT_PATH') or 'system_prompt.txt').expanduser()
        if not prompt_path.is_absolute():
            prompt_path = ROOT / prompt_path
        with prompt_path.open(encoding='utf-8', newline='') as prompt_file:
            self.prompt = prompt_file.read()

    async def answer(self, context):
        async def generate(grounded):
            caution = '' if grounded else (
                '\nGoogle Search indisponível nesta resposta: diga que não conseguiu '
                'verificar fatos atuais. Não afirme fatos atuais sem verificação; '
                'limite-se a argumentos condicionais e fatos fornecidos na conversa.')
            return await asyncio.wait_for(self.client.aio.models.generate_content(
                model=self.model, contents=json.dumps(context, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=self.prompt + caution,
                    tools=[types.Tool(google_search=types.GoogleSearch())] if grounded else None,
                    max_output_tokens=1800,
                )), timeout=75)
        try:
            response = await generate(True)
        except Exception as error:
            # Unsupported search, quota, transport and timeout failures all degrade safely.
            log.warning('Grounded generation failed (%s, code=%s); trying cautious response',
                        type(error).__name__, getattr(error, 'code', None))
            response = await generate(False)
        text = (response.text or '').strip()
        if not text:
            raise ValueError('Gemini returned an empty response')
        if text == '[NO_REPLY]':
            return None
        sources = sources_from(response)
        if sources:
            text += '\n\nfontes: ' + ' | '.join(f'<{url}>' for url in sources)
        return text


class ChatBot(discord.Client):
    def __init__(self, config, state, gemini):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none(),
                         proxy=os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY'))
        self.config, self.state, self.gemini = config, state, gemini
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()
        self.worker = None
        self.pacing = AdaptiveHeartbeat(config)
        guild = discord.Object(id=config.guild_id)
        for name, description, action in [
            ('bot_pause', 'Pause chat responses', 'pause'),
            ('bot_resume', 'Resume from the newest current message', 'resume'),
            ('bot_status', 'Show current bot status', 'status'),
        ]:
            def callback_for(action):
                async def callback(interaction: discord.Interaction):
                    await self.control(interaction, action)
                return callback
            self.tree.add_command(app_commands.Command(
                name=name, description=description, callback=callback_for(action)), guild=guild)

    async def setup_hook(self):
        self.worker = asyncio.create_task(self.heartbeat())

    async def on_ready(self):
        log.info('Discord connected as bot ID %s', self.user.id)
        try:
            await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
        except Exception as error:
            log.error('Slash command sync failed (%s); will retry on reconnect', type(error).__name__)

    async def channel(self):
        channel = self.get_channel(self.config.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config.guild_id:
            raise ValueError('Configured channel must be a text channel in configured guild')
        return channel

    async def baseline(self, channel, enabled):
        newest = [m async for m in channel.history(limit=1)]
        # A snowflake representing now also works when the channel is empty.
        value = newest[0].id if newest else discord.utils.time_snowflake(discord.utils.utcnow())
        self.state.save(enabled, value)
        self.pacing.reset()

    def relevant(self, message):
        return (message.guild is not None and message.guild.id == self.config.guild_id
                and message.channel.id == self.config.channel_id
                and not message.author.bot and message.author.id in self.config.allowed_ids)

    async def context(self, channel, target):
        entries = []
        async for message in channel.history(limit=None, before=discord.Object(id=target.id + 1)):
            if self.relevant(message) or (self.user and message.author.id == self.user.id):
                if message.content.strip():
                    entries.append({'author': f'{message.author.display_name} ({message.author.id})',
                                    'text': message.content})
            if len(entries) == 20:
                break
        return list(reversed(entries))

    async def scan(self):
        async with self.lock:
            channel = await self.channel()
            if self.state.last_seen_message_id is None or not self.state.enabled:
                await self.baseline(channel, self.state.enabled)
                return
            newest = [m async for m in channel.history(limit=1)]
            if not newest or newest[0].id <= self.state.last_seen_message_id:
                return
            upper = newest[0].id
            messages = [message async for message in channel.history(
                limit=None, after=discord.Object(id=self.state.last_seen_message_id),
                before=discord.Object(id=upper + 1), oldest_first=True,
            )]
            candidates = [m for m in messages if self.relevant(m) and m.content.strip()
                          and not (self.config.skip_low_content and low_content(m.content))]
            for message in candidates:
                self.pacing.observe(message.id, message.created_at.timestamp())
            # Prefer the latest substantive messages; earlier ones remain in context.
            selected = {m.id for m in candidates[-self.config.max_responses:]}
            for message in messages:
                if message.id not in selected:
                    self.state.save(self.state.enabled, message.id)
                    continue
                # Read-only failures must not claim the message: retry next heartbeat.
                try:
                    context = await self.context(channel, message)
                except Exception as error:
                    log.error('Context for message %s failed (%s, status=%s); retry next heartbeat',
                              message.id, type(error).__name__, getattr(error, 'status', None))
                    return
                # At-most-once: persist BEFORE generation/send. Failed replies are skipped.
                self.state.save(self.state.enabled, message.id)
                stage = 'Gemini generation'
                try:
                    log.info('Generating reply to message %s', message.id)
                    answer = await self.gemini.answer(context)
                    if answer is None:
                        log.info('No reply needed for message %s', message.id)
                        continue
                    stage = 'Discord send'
                    for index, part in enumerate(split_message(answer)):
                        await channel.send(part, reference=message if index == 0 else None,
                                           mention_author=False,
                                           allowed_mentions=discord.AllowedMentions.none())
                except Exception as error:
                    log.error('Reply to message %s failed at %s (%s, status=%s, code=%s); skipped',
                              message.id, stage, type(error).__name__,
                              getattr(error, 'status', None), getattr(error, 'code', None))

    async def heartbeat(self):
        await self.wait_until_ready()
        # Establish initial baseline immediately, without waiting five minutes.
        while not self.is_closed():
            try:
                if self.is_ready():
                    await self.scan()
            except Exception as error:
                log.error('Heartbeat failed (%s); will retry next interval', type(error).__name__)
            delay = self.pacing.delay() if self.state.enabled else self.config.max_interval
            log.info('Next heartbeat in %.1fs', delay)
            await asyncio.sleep(delay)

    async def control(self, interaction, action):
        if (interaction.user.id != self.config.owner_id
                or interaction.guild_id != self.config.guild_id
                or interaction.channel_id != self.config.channel_id):
            await interaction.response.send_message('Not authorized here.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock:
                if action in ('pause', 'resume'):
                    await self.baseline(await self.channel(), action == 'resume')
                delay = self.pacing.delay() if self.state.enabled else self.config.max_interval
                status = (f'enabled={self.state.enabled}; last_seen_message_id={self.state.last_seen_message_id}; '
                          f'heartbeat_seconds={delay:.1f}; '
                          f'bounds={self.config.min_interval}–{self.config.max_interval}')
            await interaction.followup.send(status, ephemeral=True)
        except Exception as error:
            log.error('Control failed (%s)', type(error).__name__)
            await interaction.followup.send('Operation failed; check process logs.', ephemeral=True)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.gemini.client.aio.aclose()
        self.gemini.client.close()
        await super().close()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    config = Config.from_env()
    bot = ChatBot(config, State(ROOT / 'state.json', config.enabled), Gemini(config))
    bot.run(config.token, log_handler=None, reconnect=True)


if __name__ == '__main__':
    main()
