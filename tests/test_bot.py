import asyncio
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord
from google.genai import types

from bot import AdaptiveHeartbeat, Config, ChatBot, Gemini, State, low_content, sources_from, split_message


CFG = Config('test-token', 10, 20, frozenset({30, 40}), 30,
             'test-key', 'gemini-flash-latest', 300, True, max_responses=10)


def message(mid, uid=30, text='argument', bot=False, guild=10, channel=20):
    return NS(id=mid, content=text, created_at=discord.utils.snowflake_time(mid), author=NS(id=uid, bot=bot, display_name=str(uid)),
              guild=NS(id=guild) if guild else None, channel=NS(id=channel))


class Channel:
    def __init__(self, messages):
        self.messages = messages
        self.send = AsyncMock()

    async def history(self, limit=None, after=None, before=None, oldest_first=False):
        items = [m for m in self.messages if (after is None or m.id > after.id)
                 and (before is None or m.id < before.id)]
        items.sort(key=lambda m: m.id, reverse=not oldest_first)
        for item in items if limit is None else items[:limit]:
            yield item


class ConfigTests(unittest.TestCase):
    def config(self, ids, **extra):
        values = {'DISCORD_BOT_TOKEN': 'test', 'DISCORD_GUILD_ID': '10',
                  'DISCORD_CHANNEL_ID': '20', 'OWNER_USER_ID': '30',
                  'GEMINI_API_KEY': 'test', 'ALLOWED_USER_IDS': ids}
        values.update(extra)
        with patch.dict('os.environ', values, clear=True), patch('bot.load_dotenv'):
            return Config.from_env()

    def test_any_positive_number_of_allowed_ids(self):
        for count in (1, 2, 7, 100):
            ids = range(30, 30 + count)
            self.assertEqual(self.config(','.join(map(str, ids))).allowed_ids, frozenset(ids))
        self.assertEqual(self.config(' 30, 40,30 ').allowed_ids, frozenset({30, 40}))

    def test_heartbeat_bounds_validation(self):
        for minimum, maximum in [('0', '10'), ('20', '10'), ('10', '-1'), ('bad', '30')]:
            with self.assertRaises(ValueError):
                self.config('30', MIN_SCAN_INTERVAL_SECONDS=minimum, MAX_SCAN_INTERVAL_SECONDS=maximum)
        cfg = self.config('30', SCAN_INTERVAL_SECONDS='300', MIN_SCAN_INTERVAL_SECONDS='10',
                          MAX_SCAN_INTERVAL_SECONDS='90')
        self.assertEqual(AdaptiveHeartbeat(cfg).delay(), 90)

    def test_invalid_allowlists_fail_closed(self):
        for ids in ('', ' ', '30,', ',30', '30,,40', 'nico', '0', '-30', '30,0', '30.5'):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.config(ids)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('bot.time.monotonic', return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.pacing = AdaptiveHeartbeat(replace(CFG, interval=30, min_interval=10, max_interval=90))

    def test_fallback_learning_and_smoothing(self):
        self.assertEqual(self.pacing.delay(), 30)
        self.pacing.observe(1, 100)
        self.assertEqual(self.pacing.delay(), 30)
        self.pacing.observe(2, 112)
        self.assertEqual(self.pacing.delay(), 12)
        self.pacing.observe(3, 152)
        self.assertAlmostEqual(self.pacing.delay(), .35 * 40 + .65 * 12)

    def test_minimum_maximum_idle_and_reset(self):
        self.pacing.observe(1, 100)
        self.pacing.observe(2, 101)
        self.assertEqual(self.pacing.delay(), 10)
        self.now.return_value = 1100
        self.assertEqual(self.pacing.delay(), 50)
        self.now.return_value = 2000
        self.assertEqual(self.pacing.delay(), 90)
        self.pacing.reset()
        self.assertEqual(self.pacing.delay(), 30)
        self.pacing.observe(10, 100)
        self.pacing.observe(11, 10000)
        self.assertEqual(self.pacing.delay(), 90)

    def test_reprocessed_message_does_not_change_rhythm(self):
        self.pacing.observe(1, 100)
        self.pacing.observe(2, 120)
        self.pacing.observe(2, 120)
        self.pacing.observe(1, 100)
        self.assertEqual(self.pacing.delay(), 20)


class StateTests(unittest.TestCase):
    def test_missing_malformed_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            self.assertIsNone(State(path, False).last_seen_message_id)
            for value in ('broken', '[]', '{"enabled":true}',
                          '{"last_seen_message_id":false}', '{"last_seen_message_id":-1}'):
                path.write_text(value)
                self.assertIsNone(State(path, False).last_seen_message_id)
            path.write_text('{"enabled":false,"last_seen_message_id":123}')
            state = State(path, True)
            self.assertFalse(state.enabled)
            self.assertEqual(state.last_seen_message_id, 123)
            state.save(True, 124)
            self.assertEqual(json.loads(path.read_text())['last_seen_message_id'], 124)

    def test_failed_save_does_not_advance(self):
        state = State('/nonexistent-directory/state.json', True)
        with self.assertRaises(OSError):
            state.save(False, 123)
        self.assertTrue(state.enabled)
        self.assertIsNone(state.last_seen_message_id)

    def test_low_content_filter_preserves_substantive_messages(self):
        for text in ('kkkkk', 'kkkk rsrs', 'HAHAHAHA!', 'lol', 'asdfghjkl', 'qwertyuiop', '😂😂', 'ok'):
            self.assertTrue(low_content(text), text)
        for text in ('kkkk mas qual o custo?', 'vc joga lol?', 'oi bot', 'pq?', 'hello', 'sim, mas discordo', 'priority', 'pretty'):
            self.assertFalse(low_content(text), text)

    def test_split_limits_and_exact_content(self):
        text = ('😀 argumento\n\n' * 600) + 'x' * 2500
        parts = list(split_message(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 2000 for p in parts))

    def test_real_grounding_metadata_sources(self):
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            grounding_metadata=types.GroundingMetadata(grounding_chunks=[
                types.GroundingChunk(web=types.GroundingChunkWeb(uri=url))
                for url in ['https://example.org/a', 'https://example.org/a',
                            'javascript:bad', 'https://example.org/b',
                            'https://example.org/c', 'https://example.org/d']]))])
        self.assertEqual(sources_from(response),
                         ['https://example.org/a', 'https://example.org/b', 'https://example.org/c'])


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.json', True)
        self.gemini = NS(answer=AsyncMock(return_value='resposta'))
        self.bot = ChatBot(CFG, self.state, self.gemini)
        self.channel = Channel([message(100)])
        self.bot.channel = AsyncMock(return_value=self.channel)
        self.bot._connection.user = NS(id=50)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_first_start_skips_old_and_processes_once_in_order(self):
        await self.bot.scan()
        self.gemini.answer.assert_not_awaited()
        self.assertEqual(self.state.last_seen_message_id, 100)
        self.channel.messages.extend([message(103, 40), message(101), message(102, 99),
                                      message(104, 50, bot=True), message(105, 30, bot=True)])
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, 2)
        calls = self.gemini.answer.await_args_list
        self.assertEqual(calls[0].args[0][-1]['author'], '30 (30)')
        self.assertEqual(calls[1].args[0][-1]['author'], '40 (40)')
        self.assertFalse(any('99' in entry['author'] for call in calls for entry in call.args[0]))
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, 2)
        self.assertEqual(self.state.last_seen_message_id, 105)
        # Restart preserves the cursor and cannot repeat these messages.
        self.bot.state = State(self.state.path, True)
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, 2)

    async def test_concurrent_scans_cannot_duplicate(self):
        self.state.save(True, 99)
        await asyncio.gather(self.bot.scan(), self.bot.scan())
        self.gemini.answer.assert_awaited_once()
        self.channel.send.assert_awaited_once()

    async def test_discord_uses_environment_proxy(self):
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://proxy.example:8080'}):
            client = ChatBot(CFG, self.state, self.gemini)
        self.assertEqual(client.http.proxy, 'http://proxy.example:8080')

    async def test_cap_prefers_latest_and_does_not_queue_old_messages(self):
        self.bot.config = replace(CFG, max_responses=1)
        self.state.save(True, 99)
        self.channel.messages = [message(100, text='first question'),
                                 message(101, 40, text='latest question'),
                                 message(102, text='kkkkkkkk')]
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs['reference'].id, 101)
        self.assertEqual(self.state.last_seen_message_id, 102)
        self.assertEqual(self.gemini.answer.await_args.args[0][0]['text'], 'first question')
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()

    async def test_filter_can_be_disabled_and_model_can_decline(self):
        self.state.save(True, 99)
        self.channel.messages = [message(100, text='kkkk')]
        await self.bot.scan()
        self.gemini.answer.assert_not_awaited()
        self.bot.config = replace(CFG, skip_low_content=False)
        self.channel.messages.append(message(101, text='kkkk'))
        self.gemini.answer.return_value = None
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.state.last_seen_message_id, 101)

    async def test_only_substantive_authorized_messages_change_rhythm(self):
        self.state.save(True, 99)
        self.channel.messages = [message(100, text='question'), message(101, 99),
                                 message(102, 50, bot=True), message(103, text='kkkk')]
        await self.bot.scan()
        self.assertEqual(self.bot.pacing.last_id, 100)
        await self.bot.control(self.interaction(), 'pause')
        self.assertIsNone(self.bot.pacing.last_id)
        await self.bot.control(self.interaction(), 'resume')
        self.assertIsNone(self.bot.pacing.average_gap)

    async def test_heartbeat_sleeps_using_adaptive_delay(self):
        self.bot.wait_until_ready = AsyncMock()
        self.bot.is_closed = lambda: False
        self.bot.is_ready = lambda: True
        self.bot.scan = AsyncMock()
        with patch.object(self.bot.pacing, 'delay', return_value=17), patch(
                'bot.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.heartbeat()
            sleep.assert_awaited_once_with(17)
        self.state.enabled = False
        with patch('bot.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await self.bot.heartbeat()
            sleep.assert_awaited_once_with(self.bot.config.max_interval)

    async def test_scope(self):
        for msg in [message(1, 99), message(2, bot=True), message(3, channel=99),
                    message(4, guild=99), message(5, guild=None), message(6, 50, bot=True)]:
            self.assertFalse(self.bot.relevant(msg))
        self.assertTrue(self.bot.relevant(message(7, 40)))

    def interaction(self, uid=30, channel=20):
        return NS(user=NS(id=uid), guild_id=10, channel_id=channel,
                  response=NS(send_message=AsyncMock(), defer=AsyncMock()),
                  followup=NS(send=AsyncMock()))

    async def test_owner_commands_pause_resume_skip_backlog(self):
        await self.bot.scan()
        denied = self.interaction(40)
        await self.bot.control(denied, 'pause')
        self.assertTrue(self.state.enabled)
        denied.response.send_message.assert_awaited_once()
        wrong_channel = self.interaction(channel=99)
        await self.bot.control(wrong_channel, 'pause')
        self.assertTrue(self.state.enabled)
        await self.bot.control(self.interaction(), 'pause')
        self.assertFalse(self.state.enabled)
        self.channel.messages.append(message(101))
        await self.bot.scan()
        self.gemini.answer.assert_not_awaited()
        self.assertEqual(self.state.last_seen_message_id, 101)
        self.channel.messages.append(message(102))
        await self.bot.control(self.interaction(), 'resume')
        self.assertTrue(self.state.enabled)
        self.assertEqual(self.state.last_seen_message_id, 102)
        await self.bot.scan()
        self.gemini.answer.assert_not_awaited()
        self.channel.messages.append(message(103))
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()
        await self.bot.control(self.interaction(), 'status')
        self.assertEqual({c.name for c in self.bot.tree.get_commands(guild=discord.Object(id=10))},
                         {'bot_pause', 'bot_resume', 'bot_status'})

    async def test_failure_is_not_retried_and_next_message_runs(self):
        self.state.save(True, 100)
        self.channel.messages.extend([message(101), message(102)])
        self.gemini.answer.side_effect = [TimeoutError(), 'ok']
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, 2)
        self.channel.send.assert_awaited_once()
        await self.bot.scan()
        self.assertEqual(self.gemini.answer.await_count, 2)

    async def test_send_failure_not_repeated_and_mentions_disabled(self):
        self.state.save(True, 99)
        self.channel.send.side_effect = OSError('connection')
        await self.bot.scan()
        await self.bot.scan()
        self.assertEqual(self.channel.send.await_count, 1)
        kwargs = self.channel.send.await_args.kwargs
        self.assertFalse(kwargs['mention_author'])
        self.assertFalse(kwargs['allowed_mentions'].everyone)

    async def test_context_last_twenty_relevant_chronological(self):
        self.channel.messages = [message(i, 30 if i % 2 else 99) for i in range(1, 61)]
        self.channel.messages.append(message(61, 50, bot=True))
        context = await self.bot.context(self.channel, self.channel.messages[-1])
        self.assertEqual(len(context), 20)
        self.assertEqual(context[0]['author'], '30 (30)')
        self.assertEqual(context[-1]['author'], '50 (50)')
        self.assertTrue(all('99' not in entry['author'] for entry in context))

    async def test_missing_state_paused_and_empty_channel(self):
        self.state.enabled = False
        self.channel.messages = []
        await self.bot.scan()
        self.assertIsInstance(self.state.last_seen_message_id, int)
        self.gemini.answer.assert_not_awaited()

    async def test_context_failure_retries_without_claiming_message(self):
        self.state.save(True, 99)
        original = self.bot.context
        self.bot.context = AsyncMock(side_effect=OSError('transient read error'))
        await self.bot.scan()
        self.assertEqual(self.state.last_seen_message_id, 99)
        self.gemini.answer.assert_not_awaited()
        self.bot.context = original
        await self.bot.scan()
        self.gemini.answer.assert_awaited_once()
        self.assertEqual(self.state.last_seen_message_id, 100)

    async def test_write_failure_prevents_generation(self):
        self.state.save(True, 99)
        with patch.object(self.state, 'save', side_effect=OSError()):
            with self.assertRaises(OSError):
                await self.bot.scan()
        self.gemini.answer.assert_not_awaited()


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_prompt_is_preserved_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'custom.txt'
            content = 'custom prompt\r\nwith unchanged newlines\n'
            prompt.write_bytes(content.encode('utf-8'))
            with patch.dict('os.environ', {'SYSTEM_PROMPT_PATH': str(prompt)}), patch('bot.genai.Client'):
                self.assertEqual(Gemini(CFG).prompt, content)

    async def test_relative_prompt_resolves_from_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / 'custom.txt'
            prompt.write_text('local prompt', encoding='utf-8')
            with patch.dict('os.environ', {'SYSTEM_PROMPT_PATH': 'custom.txt'}), patch('bot.ROOT', Path(directory)), patch('bot.genai.Client'):
                self.assertEqual(Gemini(CFG).prompt, 'local prompt')

    async def test_search_failure_uses_cautious_no_tool_fallback(self):
        fake = NS(aio=NS(models=NS(generate_content=AsyncMock(side_effect=[
            RuntimeError('unsupported search'), types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(parts=[types.Part(text='cautela')]))])]))))
        with patch('bot.genai.Client', return_value=fake):
            gemini = Gemini(CFG)
        self.assertEqual(await gemini.answer([{'author': '30', 'text': 'argument'}]), 'cautela')
        calls = fake.aio.models.generate_content.await_args_list
        self.assertIsNotNone(calls[0].kwargs['config'].tools)
        self.assertIsNone(calls[1].kwargs['config'].tools)
        self.assertIn('Não afirme fatos atuais', calls[1].kwargs['config'].system_instruction)

    async def test_no_reply_marker_is_not_a_response(self):
        fake = NS(aio=NS(models=NS(generate_content=AsyncMock(return_value=
            types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(parts=[types.Part(text='[NO_REPLY]')]))])))))
        with patch('bot.genai.Client', return_value=fake):
            gemini = Gemini(CFG)
        self.assertIsNone(await gemini.answer([]))

    async def test_empty_response_fails(self):
        fake = NS(aio=NS(models=NS(generate_content=AsyncMock(
            return_value=types.GenerateContentResponse()))))
        with patch('bot.genai.Client', return_value=fake):
            gemini = Gemini(CFG)
        with self.assertRaises(ValueError):
            await gemini.answer([])


if __name__ == '__main__':
    unittest.main()
