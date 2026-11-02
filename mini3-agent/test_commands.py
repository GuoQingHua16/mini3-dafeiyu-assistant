import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import numpy as np
import agent
from commands import LocalCommand, parse_command, PROFILES


class ParsingTests(unittest.TestCase):
    def test_modes_and_moods(self):
        expected = {'切换到陪伴聊天': ('profile', 'companion'),
                    '小聆，切换到英语陪练。': ('profile', 'english'),
                    '进入故事模式': ('profile', 'story'),
                    '换个开心的表情': ('mood', 'happy'),
                    '请变成难过表情吧': ('mood', 'sad'),
                    '生气一点': ('mood', 'angry'),
                    '恢复正常表情': ('mood', 'neutral')}
        for text, (kind, target) in expected.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text), LocalCommand(kind, target))

    def test_settings_absolute_and_relative(self):
        for text, expected in [
            ('调低屏幕亮度', LocalCommand('setting', 'brightness', 'adjust', -10)),
            ('小声一点', LocalCommand('setting', 'volume', 'adjust', -10)),
            ('把音量调高一点', LocalCommand('setting', 'volume', 'adjust', 10)),
            ('屏幕亮度调到百分之三十', LocalCommand('setting', 'brightness', 'set', 30)),
            ('音量调到50%', LocalCommand('setting', 'volume', 'set', 50)),
            ('声量设置为一百', LocalCommand('setting', 'volume', 'set', 100)),
        ]:
            self.assertEqual(parse_command(text), expected)

    def test_unsafe_or_discussed_commands_do_not_execute(self):
        for text in ['不要调低屏幕亮度', '小声一点是什么意思', '请解释“音量调到50%”',
                     '切换到英语陪练;reboot', '音量调到50然后把屏幕关闭',
                     '我今天很难过', '继续讲', 'turn the volume down']:
            self.assertIsNone(parse_command(text), text)
        for text in ['音量调到101', '亮度调到零', '音量调到五']:
            self.assertEqual(parse_command(text).kind, 'invalid')


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        item = agent.Agent.__new__(agent.Agent)
        item.profile = 'companion'
        item.history = [{'role': 'user', 'content': 'old'}]
        item.command = AsyncMock()
        item.speak = AsyncMock()
        item.ui = AsyncMock()
        item.reply = AsyncMock()
        item.asr = Mock()
        return item

    async def test_mode_ack_clears_history_without_paid_request(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_SCENE_OK SCENE=english'
        with patch.object(agent, 'recognize', return_value='切换到英语陪练'):
            self.assertEqual(await item.turn(np.ones(16000)), 'replied')
        self.assertEqual(item.profile, 'english')
        self.assertEqual(item.history, [])
        item.reply.assert_not_awaited()

    async def test_failed_mode_ack_does_not_change_profile_or_history(self):
        item = self.instance()
        item.command.return_value = 'Command not Found'
        with self.assertRaises(RuntimeError):
            await item.local_action(LocalCommand('profile', 'english'))
        self.assertEqual(item.profile, 'companion')
        self.assertTrue(item.history)
        item.speak.assert_not_awaited()

    async def test_setting_confirmation_uses_actual_device_result(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_CONTROL_OK VOLUME=10'
        await item.local_action(LocalCommand('setting', 'volume', 'adjust', -10))
        item.speak.assert_awaited_once_with('音量已设为百分之10。')
        item.reply.assert_not_awaited()
        item.command.return_value = 'LOCAL_AGENT_CONTROL_ERROR'
        item.speak.reset_mock()
        with self.assertRaises(RuntimeError):
            await item.local_action(LocalCommand('setting', 'volume', 'set', 50))
        item.speak.assert_not_awaited()

    async def test_mood_command_is_local_and_preserves_history(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_MOOD_OK MOOD=sad'
        await item.local_action(LocalCommand('mood', 'sad'))
        item.command.assert_awaited_once_with('local_agent mood sad')
        self.assertTrue(item.history)
        item.reply.assert_not_awaited()

    async def test_profiles_use_distinct_prompts_and_token_budgets(self):
        item = self.instance()
        # Exercise real reply request construction without making network calls.
        item.args = SimpleNamespace(model='deepseek-flash')
        item.key = 'test-only'
        response = AsyncMock()
        response.status = 200
        response.json.return_value = {'choices': [{'message': {'content': 'answer'}}]}
        response.__aenter__.return_value = response
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.post = Mock(return_value=response)
        with patch.object(agent.aiohttp, 'ClientSession', return_value=session):
            for profile in PROFILES:
                item.profile = profile
                await agent.Agent.reply(item, 'hello')
                payload = session.post.call_args.kwargs['json']
                self.assertEqual(payload['max_tokens'], PROFILES[profile][2])
                self.assertIn(PROFILES[profile][1], payload['messages'][0]['content'])


if __name__ == '__main__':
    unittest.main()
