import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent
from prompt_cache import PromptCache, cacheable_prompt


class PromptCacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cache = PromptCache(self.root)
        self.text = '换成正常表情啦。'
        self.audio = b'ID3' + b'x' * 300

    def test_only_fixed_local_prompts_are_eligible(self):
        for text in [self.text, '音量已设为百分之100。', '屏幕亮度已设为百分之10。', '计时时间到了。']:
            self.assertTrue(cacheable_prompt(text), text)
        for text in ['你好', '我叫小明', '结果是13。', '今天是2026年10月29日',
                     '提醒时间到了，该喝水啦。', '合肥今天晴', '音量已设为百分之101。']:
            self.assertFalse(cacheable_prompt(text), text)
            self.assertFalse(self.cache.put(text, 'voice', self.audio))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_voice_key_and_restart_preserve_correct_audio(self):
        self.assertTrue(self.cache.put(self.text, 'voice1', self.audio))
        restored = PromptCache(self.root)
        self.assertEqual(restored.get(self.text, 'voice1'), self.audio)
        self.assertIsNone(restored.get(self.text, 'voice2'))

    def test_corrupt_audio_or_metadata_is_a_miss(self):
        self.cache.put(self.text, 'voice', self.audio)
        key = self.cache.key(self.text, 'voice')
        (self.root / (key + '.mp3')).write_bytes(b'z' * len(self.audio))
        self.assertIsNone(self.cache.get(self.text, 'voice'))
        self.cache.put(self.text, 'voice', self.audio)
        (self.root / (key + '.json')).write_text('{broken', encoding='utf-8')
        self.assertIsNone(self.cache.get(self.text, 'voice'))

    def test_read_only_access_time_failure_does_not_discard_valid_audio(self):
        self.cache.put(self.text, 'voice', self.audio)
        with patch.object(Path, 'touch', side_effect=OSError('read-only')):
            self.assertEqual(self.cache.get(self.text, 'voice'), self.audio)

    def test_entry_and_byte_budgets_evict_old_audio(self):
        cache = PromptCache(self.root, max_entries=2, max_bytes=700)
        for i, text in enumerate(['计时时间到了。', self.text, '已切换到故事模式。']):
            cache.put(text, 'voice', self.audio)
            os.utime(self.root / (cache.key(text, 'voice') + '.mp3'), (100 + i, 100 + i))
        self.assertIsNone(cache.get('计时时间到了。', 'voice'))
        self.assertEqual(len(list(self.root.glob('*.mp3'))), 2)
        self.assertEqual(cache.get(self.text, 'voice'), self.audio)

    def test_io_failure_and_oversized_audio_are_nonfatal(self):
        with patch.object(Path, 'mkdir', side_effect=OSError('disk full')):
            self.assertFalse(self.cache.put(self.text, 'voice', self.audio))
        self.assertFalse(self.cache.put(self.text, 'voice', b'x' * (512 * 1024 + 1)))

    def test_byte_budget_applies_even_with_free_entry_slots(self):
        cache = PromptCache(self.root, max_entries=10, max_bytes=500)
        cache.put(self.text, 'voice', self.audio)
        os.utime(self.root / (cache.key(self.text, 'voice') + '.mp3'), (100, 100))
        cache.put('计时时间到了。', 'voice', self.audio)
        self.assertIsNone(cache.get(self.text, 'voice'))
        self.assertEqual(cache.get('计时时间到了。', 'voice'), self.audio)


class StatusTests(unittest.IsolatedAsyncioTestCase):
    def item(self, *responses):
        return SimpleNamespace(command=AsyncMock(side_effect=responses), poll_supported=True)

    def valid(self, state='IDLE', button=7):
        return f'LOCAL_AGENT_{state} BUTTON={button} CLOUD=DISABLED PET=1 CONTROL=1 PHASE=7'

    async def test_compact_poll_ignores_unrelated_log_lines(self):
        instance = self.item('driver info\r\n' + self.valid() + '\r\nother log\n')
        self.assertEqual(await agent.device_status(instance), self.valid())
        instance.command.assert_awaited_once_with('local_agent poll')

    async def test_partial_read_recovers_without_reinitializing_device(self):
        instance = self.item('LOCAL_AGENT_IDLE BUTTON=7\0', self.valid())
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            self.assertEqual(await agent.device_status(instance), self.valid())
        self.assertEqual([call.args[0] for call in instance.command.await_args_list], ['local_agent poll'] * 2)

    async def test_missing_phase_and_persistent_failure_remain_connection_loss(self):
        instance = self.item(*['LOCAL_AGENT_IDLE BUTTON=7 CLOUD=DISABLED PET=1 CONTROL=1'] * 3)
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.device_status(instance)
        self.assertEqual(instance.command.await_count, 3)

    async def test_transport_loss_is_not_retried(self):
        instance = self.item(agent.DeviceDisconnected('USB removed'))
        with self.assertRaises(agent.DeviceDisconnected):
            await agent.device_status(instance)
        self.assertEqual(instance.command.await_count, 1)

    async def test_large_counter_and_done_state_survive(self):
        instance = self.item(self.valid('DONE', 4294967295))
        self.assertEqual(agent.button_sequence(await agent.device_status(instance)), 4294967295)

    def test_compact_protocol_fits_firmware_shell_buffer(self):
        self.assertLess(len(self.valid('PLAYING', 4294967295).encode()) + 1, 128)


class CachedSpeechTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.instance = agent.Agent.__new__(agent.Agent)
        self.instance.prompt_cache = PromptCache(self.folder.name)
        self.instance.args = SimpleNamespace(voice='voice', host_ip='127.0.0.1', port=8765)
        self.instance.audio = {}
        self.instance.ui = AsyncMock()
        self.instance.wait_playback = AsyncMock()
        self.instance.command = AsyncMock()
        self.audio = b'ID3' + b'x' * 300
        self.text = '换成正常表情啦。'

    def prepare(self):
        self.instance.command.side_effect = ['LOCAL_AGENT_IDLE BUTTON=7', 'LOCAL_AGENT_QUEUED']

    def synthesizer(self):
        async def stream():
            yield {'type': 'audio', 'data': self.audio}
        return SimpleNamespace(stream=stream)

    async def test_second_completed_prompt_bypasses_online_synthesis(self):
        self.prepare()
        with patch.object(agent.edge_tts, 'Communicate', return_value=self.synthesizer()) as tts:
            await self.instance.speak(self.text)
        tts.assert_called_once()
        self.prepare()
        with patch.object(agent.edge_tts, 'Communicate', side_effect=AssertionError('network forbidden')):
            await self.instance.speak(self.text)
        self.assertEqual(self.instance.wait_playback.await_count, 2)
        self.assertEqual(self.instance.audio, {})

    async def test_interrupted_prompt_is_not_committed_to_cache(self):
        self.prepare()
        self.instance.wait_playback.side_effect = agent.PlaybackInterrupted()
        with patch.object(agent.edge_tts, 'Communicate', return_value=self.synthesizer()):
            with self.assertRaises(agent.PlaybackInterrupted):
                await self.instance.speak(self.text)
        self.assertIsNone(self.instance.prompt_cache.get(self.text, 'voice'))
        self.assertEqual(self.instance.audio, {})

    async def test_ordinary_answer_is_not_persisted(self):
        self.prepare()
        with patch.object(agent.edge_tts, 'Communicate', return_value=self.synthesizer()):
            await self.instance.speak('我叫小明，住在合肥。')
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
