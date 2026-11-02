import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import agent
from local_tools import (BEIJING, UtilityCommand, UtilityResult, parse_utility,
                         numeric, duration, arithmetic, tool_answer)
from reminders import Reminders
from utilities import Utilities
from weather import Weather, WeatherUnavailable


class ParsingTests(unittest.TestCase):
    def test_supported_timer_and_reminder_requests(self):
        for text, op, arg, label in [
            ('计时十分钟', 'start', '十分钟', '计时'),
            ('小聆，请开始一个番茄钟。', 'start', '25分钟', '番茄钟'),
            ('半小时后提醒我喝水', 'start', '半小时', '喝水'),
            ('一个小时三十分钟以后提醒我取快递', 'start', '一个小时三十分钟', '取快递'),
            ('暂停计时', 'pause', '', '计时'), ('继续番茄钟', 'resume', '', '番茄钟'),
            ('取消喝水提醒', 'cancel', '', '喝水'), ('取消全部提醒', 'cancel', '', '*'),
            ('计时还剩多久', 'list', '', '计时'), ('查看提醒', 'list', '', ''),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_utility(text), UtilityCommand('reminder', op, arg, label))

    def test_weather_city_and_day_are_distinct(self):
        for text, day, city, topic in [
            ('今天会下雨吗', '0', '', 'rain'), ('明天多少度', '1', '', 'summary'),
            ('今天天气怎么样', '0', '', 'summary'), ('北京明天天气怎么样', '1', '北京', 'summary'),
            ('合肥后天的天气', '2', '合肥', 'summary'), ('杭州天气如何', '0', '杭州', 'summary'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_utility(text), UtilityCommand('weather', day, city, topic))
        self.assertEqual(parse_utility('把天气城市设为合肥'), UtilityCommand('weather', 'city', '合肥'))

    def test_local_tool_requests(self):
        for text, operation in [('现在几点', 'time'), ('今天星期几', 'date'),
                                ('计算三加五乘二', 'calculate'), ('一加一等于几', 'calculate'),
                                ('一公里等于多少米', 'convert'), ('负五摄氏度换算成华氏度', 'convert')]:
            self.assertEqual(parse_utility(text).operation, operation)

    def test_negation_discussion_and_quotes_do_not_create_tasks(self):
        for text in ['不要计时十分钟', '不要取消提醒', '“计时十分钟”', '计时十分钟是什么意思',
                     '如果半小时后提醒我喝水会怎么样', '明天天气不好怎么办',
                     '我想知道天气模块怎么实现', '计算三加五的代码怎么写', '他说半小时后提醒我喝水']:
            self.assertIsNone(parse_utility(text), text)

    def test_chinese_numbers_and_composite_durations(self):
        for text, seconds in [('十分钟', 600), ('半小时', 1800), ('一个半小时', 5400),
                              ('一小时三十分钟', 5400), ('三分钟半', 210), ('零点五分钟', 30),
                              ('1.5分钟', 90), ('七天', 604800), ('一百二十秒', 120)]:
            self.assertEqual(duration(text), seconds)
        self.assertEqual(numeric('两百零五'), 205)
        self.assertEqual(numeric('三点零五'), 3.05)
        for text in ['零分钟', '八天', '一百年', '十分钟十分钟', '-1分钟', '0.1秒']:
            with self.assertRaises(ValueError):
                duration(text)

    def test_calculation_precedence_decimals_and_negatives(self):
        for expression, answer in [('三加五乘二', 13), ('（三加五）乘二', 16), ('3点五加二', 5.5),
                                   ('负五加三', -2), ('1除以4', .25), ('2+-3', -1)]:
            self.assertEqual(arithmetic(expression), answer)
        for request, answer in [('计算一下三加五乘二', 13), ('请回答一加1等于几？', 2),
                                ('负五加三等于多少', -2), ('计算三加五乘二等于多少', 13)]:
            with self.subTest(request=request):
                self.assertEqual(tool_answer(parse_utility(request)).text, f'结果是{answer}。')

    def test_calculator_rejects_code_and_unbounded_operations(self):
        for expression in ['__import__("os")', 'open(1)', '9**999999', '1//2', '1/0',
                           '1e999', '1+' * 80 + '1', 'nan', '10%3', '9' * 20]:
            with self.assertRaises(ValueError):
                arithmetic(expression)

    def test_unit_dimensions_and_temperature(self):
        for text, expected in [('一公里等于多少米', '1000米'), ('一公斤等于多少斤', '2斤'),
                               ('500毫升换算成升', '0.5升'),
                               ('零摄氏度换成华氏度', '32华氏度'), ('负五摄氏度换算成华氏度', '23华氏度')]:
            self.assertIn(expected, tool_answer(parse_utility(text)).text)
        with self.assertRaises(ValueError):
            tool_answer(parse_utility('一公里换算成公斤'))

    def test_time_and_date_use_beijing_time(self):
        moment = datetime(2026, 10, 11, 9, 5, tzinfo=BEIJING)
        self.assertIn('9点5分', tool_answer(parse_utility('现在几点'), moment).text)
        self.assertIn('星期日', tool_answer(parse_utility('今天几号'), moment).text)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'reminders.json'
        self.now = 1000.0
        self.reminders = Reminders(self.path, clock=lambda: self.now)

    def apply(self, text):
        return self.reminders.apply(parse_utility(text))

    def test_create_due_deliver_and_reload(self):
        self.apply('十秒后提醒我喝水')
        self.assertIsNone(self.reminders.next_due())
        restored = Reminders(self.path, clock=lambda: self.now)
        self.assertEqual(restored.items[0].label, '喝水')
        self.now += 10
        item = restored.next_due()
        self.assertIn('喝水', restored.announcement(item).text)
        restored.delivered(item)
        self.assertEqual(Reminders(self.path).items, [])

    def test_paused_timer_survives_restart_and_resumes_remaining_time(self):
        self.apply('计时十分钟')
        self.now += 100
        self.apply('暂停计时')
        self.now += 10000
        restored = Reminders(self.path, clock=lambda: self.now)
        self.assertIsNone(restored.next_due())
        self.assertEqual(restored.items[0].remaining, 500)
        restored.apply(parse_utility('继续计时'))
        self.now += 499
        self.assertIsNone(restored.next_due())
        self.now += 1
        self.assertIsNotNone(restored.next_due())

    def test_pomodoro_and_generic_timer_controls(self):
        self.apply('开始一个番茄钟')
        self.assertEqual(self.reminders.items[0].due, 2500)
        self.apply('暂停计时')
        self.assertIsNone(self.reminders.items[0].due)
        self.apply('继续番茄钟')
        self.assertEqual(self.reminders.items[0].due, 2500)
        self.apply('取消计时')
        self.assertEqual(self.reminders.items, [])

    def test_active_timer_is_not_silently_replaced(self):
        self.apply('计时十分钟')
        with self.assertRaises(ValueError):
            self.apply('开始一个番茄钟')
        self.assertEqual(self.reminders.items[0].label, '计时')

    def test_multiple_reminders_require_explicit_target(self):
        self.apply('一分钟后提醒我喝水')
        self.apply('两分钟后提醒我取快递')
        with self.assertRaises(ValueError):
            self.apply('取消提醒')
        self.assertEqual(len(self.reminders.items), 2)
        self.apply('取消喝水提醒')
        self.assertEqual(self.reminders.items[0].label, '取快递')
        self.apply('取消全部提醒')
        self.assertEqual(self.reminders.items, [])

    def test_timer_remaining_query_selects_timer_after_multiple_reminders(self):
        for label in ['喝水', '散步', '取快递', '开会']:
            self.apply('一分钟后提醒我' + label)
        self.apply('计时十分钟')
        self.now += 100
        result = self.apply('计时还剩多久')
        self.assertEqual(result.text, '计时还剩8分钟20秒。')
        self.assertIn('8分钟20秒', result.caption)
        self.assertNotIn('喝水', result.text)

    def test_failed_persistence_rolls_back_creation(self):
        with patch.object(self.reminders, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply('计时十分钟')
        self.assertEqual(self.reminders.items, [])

    def test_failed_completion_keeps_saved_task(self):
        self.reminders.apply(parse_utility('一秒后提醒我喝水'))
        item = self.reminders.items[0]
        with patch.object(self.reminders, 'save', side_effect=OSError('read-only disk')):
            with self.assertRaises(OSError):
                self.reminders.delivered(item)
        self.assertEqual(self.reminders.items, [item])
        self.assertEqual(Reminders(self.reminders.path).items, [item])

    def test_retry_backoff_and_due_order(self):
        self.apply('十秒后提醒我喝水')
        self.apply('五秒后提醒我伸展')
        self.now += 10
        item = self.reminders.next_due()
        self.assertEqual(item.label, '伸展')
        self.reminders.defer(item)
        self.assertEqual(self.reminders.next_due().label, '喝水')
        self.now += 60
        self.assertEqual(self.reminders.next_due().label, '伸展')

    def test_corrupt_store_is_preserved(self):
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            Reminders(self.path)
        self.assertEqual(self.path.read_text(), '{broken')


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = datetime(2026, 10, 11, 10, 20, tzinfo=BEIJING).timestamp()
        self.weather = Weather(Path(self.folder.name) / 'weather.json', clock=lambda: self.now)
        self.location = {'name': '合肥', 'country_code': 'CN', 'latitude': 31.86, 'longitude': 117.28, 'population': 100}
        self.data = {'daily': {'time': ['2026-10-11', '2026-10-12', '2026-10-13'],
            'weather_code': [2, 61, 0], 'temperature_2m_max': [25, 23, 24],
            'temperature_2m_min': [18, 17, 16], 'precipitation_probability_max': [20, 80, 0]}}

    async def test_default_hefei_forecast_and_screen_timestamp(self):
        self.weather.fetch = AsyncMock(side_effect=[{'results': [self.location]}, self.data])
        result = await self.weather.apply(parse_utility('明天多少度'))
        self.assertIn('合肥明天有雨', result.text)
        self.assertIn('23度', result.text)
        self.assertIn('10:20', result.caption)
        self.assertLessEqual(len(result.caption.encode('utf-8')), 42)
        self.assertEqual(self.weather.fetch.await_count, 2)

    async def test_fresh_cache_avoids_repeat_network_call(self):
        self.weather.fetch = AsyncMock(side_effect=[{'results': [self.location]}, self.data])
        await self.weather.apply(parse_utility('今天天气怎么样'))
        self.now += 60
        await self.weather.apply(parse_utility('今天会下雨吗'))
        self.assertEqual(self.weather.fetch.await_count, 2)

    async def test_network_failure_labels_cache_and_expiry(self):
        self.weather.fetch = AsyncMock(side_effect=[{'results': [self.location]}, self.data,
            WeatherUnavailable('offline'), WeatherUnavailable('offline')])
        await self.weather.apply(parse_utility('天气怎么样'))
        self.now += 601
        cached = await self.weather.apply(parse_utility('天气怎么样'))
        self.assertIn('缓存预报', cached.text)
        self.assertIn('10:20', cached.text)
        self.now += 3600
        with self.assertRaises(WeatherUnavailable):
            await self.weather.apply(parse_utility('天气怎么样'))

    async def test_midnight_cache_does_not_shift_day_index(self):
        self.weather.fetch = AsyncMock(side_effect=[{'results': [self.location]}, self.data])
        await self.weather.apply(parse_utility('天气怎么样'))
        self.now = datetime(2026, 10, 12, 0, 1, tzinfo=BEIJING).timestamp()
        self.weather.cache[(31.86, 117.28)] = (self.data, self.now - 120)
        result = await self.weather.apply(parse_utility('今天天气怎么样'))
        self.assertIn('有雨', result.text)
        self.assertIn('23度', result.text)

    async def test_city_change_is_validated_then_persisted(self):
        self.weather.fetch = AsyncMock(return_value={'results': [dict(self.location, name='北京')]})
        await self.weather.apply(parse_utility('把天气城市设为北京'))
        self.assertEqual(Weather(self.weather.path).city, '北京')
        self.weather.fetch = AsyncMock(return_value={'results': []})
        with self.assertRaises(WeatherUnavailable):
            await self.weather.apply(parse_utility('把天气城市设为不存在的城市'))
        self.assertEqual(Weather(self.weather.path).city, '北京')

    async def test_invalid_location_shape_returns_local_error(self):
        for rows in ['unexpected', [None], [dict(self.location, population=None)]]:
            self.weather.fetch = AsyncMock(return_value={'results': rows})
            with self.assertRaises(WeatherUnavailable):
                await self.weather.location('合肥')

    def test_missing_values_and_invalid_ranges_fail_closed(self):
        for mutate in [lambda d: d['daily']['temperature_2m_max'].__setitem__(0, None),
                       lambda d: d['daily']['precipitation_probability_max'].__setitem__(0, 101),
                       lambda d: d['daily']['temperature_2m_min'].__setitem__(0, 50)]:
            data = json.loads(json.dumps(self.data))
            mutate(data)
            with self.assertRaises(WeatherUnavailable):
                Weather.validate(data)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.item = agent.Agent.__new__(agent.Agent)
        self.item.utilities = Utilities(self.folder.name)
        self.item.history = []
        self.item.last_answer_at = None
        self.item.listen_mode = 'continuous'
        self.item.ui = AsyncMock()
        self.item.speak = AsyncMock()
        self.item.reply = AsyncMock(side_effect=AssertionError('No paid request allowed'))
        self.item.command = AsyncMock(return_value='LOCAL_AGENT_IDLE BUTTON=7')
        self.item.args = SimpleNamespace(mode='continuous')

    async def test_local_modules_and_errors_bypass_paid_api(self):
        for text in ['计时十分钟', '计时零分钟', '暂停计时', '继续计时', '计时还剩多久',
                     '取消计时', '半小时后提醒我喝水', '取消全部提醒', '现在几点',
                     '今天星期几', '计算1除以0', '计算三加五乘二', '计算一下三加五乘二',
                     '请回答一加1等于几？', '一公里等于多少米']:
            self.assertEqual(await self.item.respond(text), 'replied', text)
        self.item.reply.assert_not_awaited()
        self.assertEqual(self.item.history, [])

    async def test_weather_outage_does_not_fall_back_to_chat(self):
        self.item.utilities.weather.fetch = AsyncMock(side_effect=WeatherUnavailable('天气服务不可用'))
        self.assertEqual(await self.item.respond('今天会下雨吗'), 'replied')
        self.assertIn('不可用', self.item.speak.await_args.args[0])
        self.item.reply.assert_not_awaited()

    async def test_due_notification_preserves_rest_and_is_delivered_once(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        state = agent.ConversationState(60)
        state.resting = True
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            await agent.announce_reminder(self.item, state)
            await agent.announce_reminder(self.item, state)
        self.item.speak.assert_awaited_once()
        self.item.ui.assert_awaited_once_with('sleeping')
        self.assertTrue(state.resting)
        self.assertEqual(reminders.items, [])
        self.item.reply.assert_not_awaited()

    async def test_button_dismisses_notification_without_replay(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.speak.side_effect = agent.PlaybackInterrupted()
        with self.assertRaises(agent.PlaybackInterrupted):
            await agent.announce_reminder(self.item, agent.ConversationState(60))
        self.assertEqual(reminders.items, [])

    async def test_failed_notification_keeps_job_and_backs_off(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.speak.side_effect = RuntimeError('TTS failed')
        with self.assertRaises(RuntimeError):
            await agent.announce_reminder(self.item, agent.ConversationState(60))
        self.assertEqual(len(reminders.items), 1)
        self.assertIsNone(reminders.next_due())

    async def test_failed_completion_save_does_not_exit_with_oserror(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        with patch.object(reminders, 'save', side_effect=OSError('read-only disk')):
            with self.assertRaises(RuntimeError):
                await agent.announce_reminder(self.item, agent.ConversationState(60))
        self.assertEqual(len(reminders.items), 1)
        self.assertIsNone(reminders.next_due())

    async def test_due_reminder_runs_in_rest_without_opening_microphone(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.command.side_effect = ['LOCAL_AGENT_IDLE BUTTON=7', 'LOCAL_AGENT_DONE BUTTON=7',
                                         asyncio.CancelledError()]
        state = agent.ConversationState(60)
        state.resting = True
        with patch.object(agent, 'current_mode', return_value='continuous'), \
             patch.object(agent, 'capture', new=AsyncMock()) as capture, \
             patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await agent.connected_loop(self.item, state, 7, 7)
        capture.assert_not_awaited()
        self.item.speak.assert_awaited_once()
        self.assertTrue(state.resting)
        self.assertEqual(reminders.items, [])

    async def test_button_request_is_kept_when_reminder_is_due(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=8'
        state = agent.ConversationState(60)
        state.resting = True
        with patch.object(agent, 'current_mode', return_value='button'), \
             patch.object(agent, 'capture', new=AsyncMock(side_effect=asyncio.CancelledError())) as capture, \
             patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await agent.connected_loop(self.item, state, 7, 7)
        self.item.speak.assert_awaited_once()
        capture.assert_awaited_once()
        self.assertFalse(state.resting)

    async def test_button_capture_resumes_after_quiet_wait_yields_to_reminder(self):
        reminders = self.item.utilities.reminders
        now = 1000
        reminders.clock = lambda: now
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        self.item.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=8'
        captures = 0

        async def capture_then_yield(*args):
            nonlocal now, captures
            captures += 1
            if captures == 1:
                now = 1002
                raise agent.ReminderPending()
            raise asyncio.CancelledError()

        with patch.object(agent, 'current_mode', return_value='button'), \
             patch.object(agent, 'capture', new=AsyncMock(side_effect=capture_then_yield)), \
             patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await agent.connected_loop(self.item, agent.ConversationState(60), 7, 7)
        self.assertEqual(captures, 2)
        self.item.speak.assert_awaited_once()

    async def test_quiet_capture_yields_and_closes_for_notification(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.args = SimpleNamespace(seconds=15, silence=.9, wait_seconds=6)
        stream = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(agent.sd, 'InputStream', return_value=stream), \
             patch.object(agent, 'SpeechEndpoint', return_value=Mock(started=False)):
            with self.assertRaises(agent.ReminderPending):
                await agent.capture(self.item, 7, 'continuous')
        stream.__exit__.assert_called_once()

    async def test_active_speech_is_not_cut_by_due_notification(self):
        reminders = self.item.utilities.reminders
        reminders.clock = lambda: 1000
        reminders.apply(parse_utility('一秒后提醒我喝水'))
        reminders.clock = lambda: 1002
        self.item.args = SimpleNamespace(seconds=15, silence=.9, wait_seconds=6)
        endpoint = Mock(started=True)
        endpoint.feed.return_value = np.ones(480)
        stream = Mock()

        def open_stream(**kwargs):
            stream.__enter__ = Mock(side_effect=lambda: kwargs['callback'](np.ones((480, 4), dtype=np.int16), 480, None, False))
            stream.__exit__ = Mock(return_value=False)
            return stream

        with patch.object(agent.sd, 'InputStream', side_effect=open_stream), \
             patch.object(agent, 'SpeechEndpoint', return_value=endpoint):
            self.assertEqual(len(await agent.capture(self.item, 7, 'continuous')), 480)
        self.item.speak.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
