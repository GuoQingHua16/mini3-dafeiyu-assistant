import time
import asyncio
import threading
import subprocess
import unittest
from unittest.mock import patch, Mock, AsyncMock
from types import SimpleNamespace
import numpy as np
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import agent


class EndpointTests(unittest.TestCase):
    def segment(self, samples, **kwargs):
        endpoint = agent.SpeechEndpoint(**kwargs)
        results = []
        for offset in range(0, len(samples) - 479, 480):
            result = endpoint.feed(samples[offset:offset+480].astype('<i2').tobytes())
            if result is not None:
                results.append(result)
                break
        return endpoint, results

    def test_noise_and_impulses_never_trigger(self):
        rng = np.random.default_rng(42)
        noise = rng.normal(0, 150, 16000 * 6)
        noise[16000:16080] += 15000
        endpoint, results = self.segment(noise)
        self.assertFalse(endpoint.started)
        self.assertEqual(results, [])

    def test_real_speech_and_quieter_speech_detected(self):
        speech, rate = agent.sf.read(agent.MODEL / 'test_wavs/zh.wav', dtype='int16')
        self.assertEqual(rate, 16000)
        for scale in (1, 0.2):
            audio = np.concatenate([np.zeros(4800), speech * scale, np.zeros(32000)])
            _, results = self.segment(audio)
            self.assertEqual(len(results), 1)
            self.assertGreater(len(results[0]), 16000)
            self.assertLessEqual(len(results[0]), 15 * 16000)

    def test_long_speech_has_hard_length_limit(self):
        speech, _ = agent.sf.read(agent.MODEL / 'test_wavs/zh.wav', dtype='int16')
        _, results = self.segment(np.tile(speech, 5), seconds=2)
        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(results[0]), 32000)

    def test_missing_neural_model_fails_without_weaker_fallback(self):
        with patch.object(agent, 'VAD_MODEL') as model:
            model.is_file.return_value = False
            with self.assertRaises(RuntimeError):
                agent.SpeechEndpoint()

    def test_volume_gate_rejects_low_level_speech_and_dc(self):
        speech, _ = agent.sf.read(agent.MODEL / 'test_wavs/zh.wav', dtype='int16')
        quiet = speech.astype(np.float32) * (20 / max(abs(speech.astype(np.float32)))) + 1500
        _, results = self.segment(np.concatenate([quiet, np.zeros(32000)]), min_rms=0.003)
        self.assertEqual(results, [])

    def test_volume_gate_preserves_normal_speech(self):
        speech, _ = agent.sf.read(agent.MODEL / 'test_wavs/zh.wav', dtype='int16')
        _, results = self.segment(np.concatenate([speech, np.zeros(32000)]), min_rms=0.003)
        self.assertEqual(len(results), 1)

    def test_real_vad_rejects_digital_silence(self):
        endpoint = agent.SpeechEndpoint()
        for _ in range(500):
            self.assertIsNone(endpoint.feed(bytes(960)))
        self.assertFalse(endpoint.started)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_timeout_closes_microphone(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        stream = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(agent.sd, 'InputStream', return_value=stream), \
             patch.object(agent, 'current_mode', return_value='continuous'), \
             patch.object(agent.time, 'monotonic', side_effect=[0, 61]):
            with self.assertRaises(agent.ListeningExpired):
                await agent.capture(instance, 7, 'continuous', deadline=60)
        stream.__exit__.assert_called_once()

    async def test_mode_switch_discards_partial_utterance(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        stream = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(agent.sd, 'InputStream', return_value=stream), \
             patch.object(agent, 'current_mode', return_value='button'), \
             patch.object(agent.time, 'monotonic', side_effect=[0, 1]):
            self.assertIsNone(await agent.capture(instance, 7, 'continuous'))
        stream.__exit__.assert_called_once()

    async def test_button_speech_timeout_returns_no_audio(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        stream = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(agent.sd, 'InputStream', return_value=stream), \
             patch.object(agent, 'current_mode', return_value='button'), \
             patch.object(agent.time, 'monotonic', side_effect=[0, 7]):
            self.assertIsNone(await agent.capture(instance, 7, 'button'))
        stream.__exit__.assert_called_once()

    async def test_capture_uses_mic_channel_and_closes_before_return(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        block = np.tile(np.array([100, 200, 300, 400], dtype=np.int16), (480, 1))
        stream = Mock()
        def open_stream(**kwargs):
            stream.__enter__ = Mock(side_effect=lambda: kwargs['callback'](block, 480, None, False))
            stream.__exit__ = Mock(return_value=False)
            return stream
        endpoint = Mock(started=False)
        endpoint.feed.return_value = np.ones(480)
        with patch.object(agent.sd, 'InputStream', side_effect=open_stream), \
             patch.object(agent, 'SpeechEndpoint', return_value=endpoint), \
             patch.object(agent, 'current_mode', return_value='continuous'):
            result = await agent.capture(instance, 7, 'continuous')
        self.assertEqual(len(result), 480)
        self.assertTrue(np.all(np.frombuffer(endpoint.feed.call_args.args[0], dtype='<i2') == 100))
        stream.__exit__.assert_called_once()

    async def test_idle_deadline_does_not_cut_active_speech(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        stream = Mock()
        def open_stream(**kwargs):
            stream.__enter__ = Mock(side_effect=lambda: kwargs['callback'](
                np.ones((480, 4), dtype=np.int16), 480, None, False))
            stream.__exit__ = Mock(return_value=False)
            return stream
        endpoint = Mock(started=True)
        endpoint.feed.return_value = np.ones(480)
        with patch.object(agent.sd, 'InputStream', side_effect=open_stream), \
             patch.object(agent, 'SpeechEndpoint', return_value=endpoint), \
             patch.object(agent, 'current_mode', return_value='continuous'):
            result = await agent.capture(instance, 7, 'continuous', deadline=0)
        self.assertEqual(len(result), 480)
        stream.__exit__.assert_called_once()

    async def test_cancellation_closes_microphone(self):
        instance = SimpleNamespace(args=SimpleNamespace(seconds=15, silence=0.9, wait_seconds=6),
                                   ui=AsyncMock())
        stream = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        with patch.object(agent.sd, 'InputStream', return_value=stream):
            task = asyncio.create_task(agent.capture(instance, 7, 'continuous'))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        stream.__exit__.assert_called_once()


class AudioTests(unittest.TestCase):
    def test_pet_caption_is_utf8_safe_and_shell_bounded(self):
        text = '你好，小聆！' * 20 + '\n\"; reboot'
        command = agent.pet_command('synthesizing', text)
        self.assertLess(len(command), 128)
        caption = bytes.fromhex(command.split()[-1]).decode('utf-8')
        self.assertTrue(text.startswith(caption))
        self.assertNotIn(';', command)

    def test_pet_command_rejects_unknown_state(self):
        with self.assertRaises(ValueError):
            agent.pet_command('reboot')
    def test_silence_never_enters_recognizer(self):
        recognizer = Mock()
        self.assertEqual(agent.recognize(recognizer, np.zeros(16000)), '')
        recognizer.create_stream.assert_not_called()

    def test_punctuation_only_asr_is_not_a_query(self):
        recognizer = Mock()
        recognizer.create_stream.return_value.result.text = ' 。！？… '
        self.assertEqual(agent.recognize(recognizer, np.ones(16000)), '')
        recognizer.create_stream.return_value.result.text = '你好。'
        self.assertEqual(agent.recognize(recognizer, np.ones(16000)), '你好。')

    def test_audio_limits(self):
        for audio in [np.zeros(1), np.zeros(320001), np.full(16000, np.nan)]:
            with self.assertRaises(ValueError):
                agent.recognize(Mock(), audio)

    def test_no_computer_microphone_fallback(self):
        with patch.object(agent.sd, 'query_devices', return_value=[
                {'name': 'Realtek microphone', 'max_input_channels': 4}]):
            with self.assertRaises(RuntimeError):
                agent.input_device()

    def test_physical_mic_channel_only(self):
        data = np.array([[32767, -32768, 123, 456]], dtype=np.int16)
        with patch.object(agent.sd, 'rec', return_value=data):
            result = agent.record(1, 1)
        self.assertAlmostEqual(float(result[0]), 32767 / 32768)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rest_command_does_not_call_paid_api(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.asr = Mock()
        instance.reply = AsyncMock()
        instance.ui = AsyncMock()
        with patch.object(agent, 'recognize', return_value='小聆，先不聊了。'):
            self.assertEqual(await instance.turn(np.ones(16000)), 'rest')
        instance.reply.assert_not_awaited()

    async def test_rest_and_resume_reset_listening_window(self):
        instance = SimpleNamespace(ui=AsyncMock())
        with patch.object(agent.time, 'monotonic', return_value=10):
            state = agent.ConversationState(60)
        await state.rest(instance, 'test')
        self.assertTrue(state.resting)
        with patch.object(agent.time, 'monotonic', return_value=100):
            state.resume()
        self.assertFalse(state.resting)
        self.assertEqual(state.deadline, 160)

    def test_rest_phrase_does_not_match_negation_or_discussion(self):
        for text in ['先不聊了。', '小林，休息吧！', '停止聆听', '休息一下',
                     '小聆，请休息一会儿吧。', '别听了', '先别听了', '不要再听了',
                     '你先休息一下吧', '暂停监听', '休息一下？', '小林，别听了?']:
            self.assertTrue(agent.wants_rest(text))
        for text in ['不要停止聆听', '先不聊了是什么意思', '我不想休息吧', '你好',
                     '不要休息一下', '我想休息一下', '你是不是想休息一下？',
                     '“休息一下”', '别听了是什么意思', '继续听不要休息', '休息一下再讲故事',
                     '休息@一下', '请不要再听了是什么意思']:
            self.assertFalse(agent.wants_rest(text))

    async def test_cancelled_adb_call_keeps_lock_until_thread_finishes(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.adb_lock = asyncio.Lock()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def fake_adb(command):
            calls.append(command)
            if command == 'first':
                entered.set()
                release.wait(2)
            return command
        with patch.object(agent, 'adb', side_effect=fake_adb):
            first = asyncio.create_task(instance.command('first'))
            await asyncio.to_thread(entered.wait, 1)
            first.cancel()
            second = asyncio.create_task(instance.command('second'))
            await asyncio.sleep(0.05)
            self.assertEqual(calls, ['first'])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertEqual(await second, 'second')

    async def test_audio_token_expiration_and_unknown_url(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.audio = {'valid.mp3': (b'ID3audio', time.monotonic() + 60),
                          'old.mp3': (b'old', time.monotonic() - 1)}
        app = web.Application()
        app.router.add_get('/audio/{token}', instance.audio_response)
        async with TestClient(TestServer(app)) as client:
            response = await client.get('/audio/valid.mp3')
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), b'ID3audio')
            for path in ['/audio/old.mp3', '/audio/unknown.mp3', '/.env']:
                self.assertEqual((await client.get(path)).status, 404)

    async def test_silent_turn_does_not_call_paid_api(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.asr = Mock()
        instance.reply = AsyncMock()
        instance.ui = AsyncMock()
        await instance.turn(np.zeros(16000))
        instance.reply.assert_not_awaited()

    async def test_failed_playback_does_not_commit_history(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.asr = Mock()
        instance.history = []
        instance.ui = AsyncMock()
        instance.reply = AsyncMock(return_value=('answer', {'role': 'user', 'content': 'hello'}))
        instance.speak = AsyncMock(side_effect=RuntimeError('playback'))
        with patch.object(agent, 'recognize', return_value='hello'):
            with self.assertRaises(RuntimeError):
                await instance.turn(np.ones(16000))
        self.assertEqual(instance.history, [])


class BufferedAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_stop_waits_for_reader_release(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.buffered_audio = True
        stopped = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def command(text):
            calls.append(text)
            if text == 'local_agent status':
                return 'LOCAL_AGENT_PLAYING BUTTON=11'
            if text == 'local_agent stop':
                stopped.set()
                return 'LOCAL_AGENT_STOPPING'
            await release.wait()
            return 'LOCAL_AGENT_AUDIO BUFFER=1 BUSY=0'

        instance.command = command
        work = asyncio.create_task(instance.wait_playback(10))
        try:
            await asyncio.wait_for(stopped.wait(), 1)
            await asyncio.sleep(.01)
            self.assertFalse(work.done(), 'capture may resume only after the reader releases its source')
            release.set()
            with self.assertRaises(agent.PlaybackInterrupted):
                await work
            self.assertEqual(calls, ['local_agent status', 'local_agent stop', 'local_agent audio'])
        finally:
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work

    async def test_unreleased_reader_times_out(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.buffered_audio = True
        instance.command = AsyncMock(return_value='LOCAL_AGENT_AUDIO BUFFER=1 BUSY=1')
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 8]))
        with patch.object(agent, 'time', clock), patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'cleanup timeout'):
                await instance.wait_audio_idle()

    async def test_missing_cleanup_ack_cannot_resume_capture(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.buffered_audio = True
        instance.command = AsyncMock(return_value='')
        with self.assertRaises(agent.DeviceDisconnected):
            await instance.wait_audio_idle()

    async def test_legacy_firmware_needs_no_cleanup_query(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.buffered_audio = False
        instance.command = AsyncMock()
        await instance.wait_audio_idle()
        instance.command.assert_not_awaited()

    async def test_oversized_speech_is_not_published_or_queued(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.buffered_audio = True
        instance.args = SimpleNamespace(voice='test')
        instance.ui = AsyncMock()
        instance.command = AsyncMock()
        instance.audio = {}

        async def stream():
            yield {'type': 'audio', 'data': b'x' * (512 * 1024 + 1)}

        with patch.object(agent.edge_tts, 'Communicate', return_value=SimpleNamespace(stream=stream)):
            with self.assertRaisesRegex(RuntimeError, '语音过长'):
                await instance.speak('long answer')
        self.assertEqual(instance.audio, {})
        instance.command.assert_not_awaited()


class OptimizationTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        instance = agent.Agent.__new__(agent.Agent)
        instance.args = SimpleNamespace(mode='continuous', idle_seconds=60)
        instance.profile = 'companion'
        instance.asr = Mock()
        instance.history = []
        instance.last_answer_at = None
        instance.listen_mode = 'continuous'
        instance.audio = {}
        instance.usage = None
        instance.command = AsyncMock()
        instance.ui = AsyncMock()
        instance.local_action = AsyncMock()
        instance.reply = AsyncMock(return_value=('好的。', {'role': 'user', 'content': '嗯'}))
        instance.speak = AsyncMock()
        return instance

    async def test_new_rest_commands_bypass_model_and_tts(self):
        instance = self.instance()
        for text in ['休息一下', '别听了', '小聆，请休息一下吧']:
            with patch.object(agent, 'recognize', return_value=text):
                self.assertEqual(await instance.turn(np.ones(16000)), 'rest')
        instance.reply.assert_not_awaited()
        instance.speak.assert_not_awaited()

    async def test_isolated_ack_does_not_call_model_or_extend_window(self):
        instance = self.instance()
        for text in ['嗯。', 'うん。', '好的', 'ok']:
            self.assertEqual(await instance.respond(text), 'ignored')
        instance.reply.assert_not_awaited()
        instance.speak.assert_not_awaited()
        self.assertIsNone(instance.last_answer_at)
        self.assertEqual(instance.history, [])

    async def test_ack_answers_recent_question(self):
        instance = self.instance()
        instance.history = [{'role': 'assistant', 'content': '要继续讲故事吗？'}]
        instance.last_answer_at = time.monotonic()
        self.assertEqual(await instance.respond('嗯。'), 'replied')
        instance.reply.assert_awaited_once_with('嗯。')
        self.assertEqual(instance.history[-2]['role'], 'user')

    async def test_explicit_button_ack_is_allowed(self):
        instance = self.instance()
        instance.listen_mode = 'button'
        self.assertEqual(await instance.respond('好的'), 'replied')
        instance.reply.assert_awaited_once_with('好的')

    def test_short_input_context_boundaries(self):
        question = [{'role': 'assistant', 'content': 'Would you like another story?'}]
        for text in ['嗯', 'yes', 'no', 'うん']:
            self.assertFalse(agent.skip_short_input(text, question, 5, 'continuous'))
            self.assertTrue(agent.skip_short_input(text, question, 61, 'continuous'))
        for answer in ['一加一等于二。', '好问题！今天很开心。', '你说过“要继续吗？”但我先讲完。',
                       '小狐狸问：“你想听故事吗？”']:
            self.assertTrue(agent.skip_short_input('嗯', [{'role': 'assistant', 'content': answer}], 5, 'continuous'))
        for answer in ['请跟读 hello。', 'Your turn: say yes.']:
            self.assertFalse(agent.skip_short_input('yes', [{'role': 'assistant', 'content': answer}], 5, 'continuous'))
        for text in ['嗯，我想听故事', '啊为什么', '2', '猫', '你好', '继续']:
            self.assertFalse(agent.skip_short_input(text, [], 100, 'continuous'))

    async def test_playback_button_stops_even_at_done_boundary(self):
        instance = self.instance()
        instance.command.side_effect = ['LOCAL_AGENT_DONE BUTTON=11', 'LOCAL_AGENT_STOPPING']
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(agent.PlaybackInterrupted):
                await agent.Agent.wait_playback(instance, 10)
        self.assertEqual([c.args[0] for c in instance.command.await_args_list],
                         ['local_agent status', 'local_agent stop'])

    async def test_playback_completion_without_click_does_not_stop(self):
        instance = self.instance()
        instance.command.return_value = 'LOCAL_AGENT_DONE BUTTON=10'
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            await agent.Agent.wait_playback(instance, 10)
        instance.command.assert_awaited_once_with('local_agent status')

    async def test_stop_without_ack_does_not_start_new_recording(self):
        instance = self.instance()
        instance.command.side_effect = ['LOCAL_AGENT_PLAYING BUTTON=11', 'unexpected']
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await agent.Agent.wait_playback(instance, 10)

    async def test_interrupted_answer_not_committed_to_history(self):
        instance = self.instance()
        instance.speak.side_effect = agent.PlaybackInterrupted()
        with self.assertRaises(agent.PlaybackInterrupted):
            await instance.respond('讲个故事')
        self.assertEqual(instance.history, [])
        self.assertIsNone(instance.last_answer_at)

    async def test_button_stop_opens_next_capture_in_button_mode(self):
        instance = self.instance()
        instance.args.mode = 'button'
        instance.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=8'
        instance.turn = AsyncMock(side_effect=agent.PlaybackInterrupted())
        capture = AsyncMock(side_effect=[np.ones(16000), asyncio.CancelledError()])
        with patch.object(agent, 'current_mode', return_value='button'), \
             patch.object(agent, 'capture', new=capture), \
             patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await agent.connected_loop(instance, agent.ConversationState(60), 7, 7)
        self.assertEqual(capture.await_count, 2)
        self.assertEqual(capture.await_args_list[-1].args[2], 'button')

    async def test_connect_reinitializes_uac_and_preserves_rest(self):
        instance = self.instance()
        instance.command.side_effect = ['LOCAL_AGENT_IDLE BUTTON=0 CLOUD=DISABLED PET=1 CONTROL=1',
            'LOCAL_AGENT_STOPPING', 'LOCAL_AGENT_SCENE_OK SCENE=companion', 'on',
            'LOCAL_AGENT_IDLE BUTTON=3 CLOUD=DISABLED PET=1 CONTROL=1']
        conversation = agent.ConversationState(60)
        conversation.resting = True
        with patch.object(agent, 'refresh_input_device', return_value=9):
            with patch.object(agent, 'current_mode', return_value='continuous'):
                self.assertEqual(await agent.connect_device(instance, conversation), (9, 3))
        self.assertTrue(conversation.resting)
        instance.ui.assert_awaited_once_with('sleeping')
        self.assertIn('wakeup_uac_record on', [c.args[0] for c in instance.command.await_args_list])

    async def test_absent_at_start_waits_then_recovers(self):
        instance = self.instance()
        connect = AsyncMock(side_effect=[agent.DeviceDisconnected('offline'),
                                         agent.sd.PortAudioError('missing'), (9, 0)])
        with patch.object(agent, 'connect_device', new=connect), \
             patch.object(agent.asyncio, 'sleep', new=AsyncMock()) as sleep:
            self.assertEqual(await agent.wait_connection(instance, agent.ConversationState(60)), (9, 0))
        self.assertEqual(connect.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        instance.reply.assert_not_awaited()

    async def test_disconnect_resumes_next_turn_without_retrying_paid_turn(self):
        instance = self.instance()
        instance.audio = {'old.mp3': (b'ID3', 100)}
        instance.last_answer_at = 100
        wait = AsyncMock(return_value=(9, 0))
        connected = AsyncMock(side_effect=[agent.DeviceDisconnected('lost USB'), asyncio.CancelledError()])
        with patch.object(agent, 'wait_connection', new=wait), \
             patch.object(agent, 'connected_loop', new=connected):
            with self.assertRaises(asyncio.CancelledError):
                await agent.conversation_loop(instance)
        self.assertEqual(wait.await_count, 2)
        self.assertEqual(instance.audio, {})
        self.assertIsNone(instance.last_answer_at)
        instance.reply.assert_not_awaited()

    async def test_incompatible_firmware_does_not_retry_forever(self):
        instance = self.instance()
        instance.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=0 CLOUD=DISABLED PET=1'
        with self.assertRaises(RuntimeError) as caught:
            await agent.wait_connection(instance, agent.ConversationState(60))
        self.assertNotIsInstance(caught.exception, agent.DeviceDisconnected)

    def test_adb_timeouts_are_reconnectable(self):
        with patch.object(agent.USB_LINK, 'execute', side_effect=agent.ADBLinkError('USB response timed out')):
            with self.assertRaises(agent.DeviceDisconnected):
                agent.adb('local_agent status')

    def test_portaudio_refresh_precedes_device_discovery(self):
        calls = []
        with patch.object(agent.sd, '_terminate', side_effect=lambda: calls.append('terminate')), \
             patch.object(agent.sd, '_initialize', side_effect=lambda: calls.append('initialize')), \
             patch.object(agent, 'input_device', side_effect=lambda: calls.append('discover') or 7):
            self.assertEqual(agent.refresh_input_device(), 7)
        self.assertEqual(calls, ['terminate', 'initialize', 'discover'])

    def test_refresh_after_failed_initialize_does_not_terminate_again(self):
        with patch.object(agent.sd, '_initialized', 0), \
             patch.object(agent.sd, '_terminate') as terminate, \
             patch.object(agent.sd, '_initialize'), \
             patch.object(agent, 'input_device', return_value=7):
            self.assertEqual(agent.refresh_input_device(), 7)
        terminate.assert_not_called()


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def instance(self):
        return OptimizationTests.instance(self)

    async def test_control_confirmation_does_not_reuse_previous_question(self):
        item = self.instance()
        item.history = [{'role': 'assistant', 'content': '想听故事吗？'}]
        item.last_answer_at = time.monotonic()
        self.assertEqual(await item.respond('小声一点'), 'replied')
        self.assertEqual(await item.respond('嗯'), 'ignored')
        item.reply.assert_not_awaited()
        self.assertEqual(item.history[-1]['content'], '想听故事吗？')

    async def test_rest_consumes_old_confirmation_context(self):
        item = self.instance()
        item.history = [{'role': 'assistant', 'content': '继续吗？'}]
        item.last_answer_at = time.monotonic()
        self.assertEqual(await item.respond('休息一下？'), 'rest')
        self.assertEqual(await item.respond('嗯'), 'ignored')
        item.reply.assert_not_awaited()

    async def test_failed_new_answer_consumes_old_question(self):
        item = self.instance()
        item.history = [{'role': 'assistant', 'content': '继续吗？'}]
        item.last_answer_at = time.monotonic()
        item.reply.side_effect = RuntimeError('model request failed')
        with self.assertRaises(RuntimeError):
            await item.respond('讲个新的故事')
        self.assertEqual(await item.respond('嗯'), 'ignored')
        self.assertEqual(item.reply.await_count, 1)

    async def test_known_disconnect_prevents_paid_request(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        item.connection_lost.set()
        with patch.object(agent.aiohttp, 'ClientSession') as session:
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.Agent.reply(item, '你好')
        session.assert_not_called()

    async def test_known_disconnect_does_not_start_operation(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        item.connection_lost.set()
        operation = AsyncMock()
        with self.assertRaises(agent.DeviceDisconnected):
            await agent.while_connected(item, operation())
        operation.assert_not_awaited()

    async def test_link_loss_cancels_and_drains_active_operation(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        entered, closed = asyncio.Event(), asyncio.Event()

        async def active():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        task = asyncio.create_task(agent.while_connected(item, active()))
        await asyncio.wait_for(entered.wait(), 1)
        item.connection_lost.set()
        with self.assertRaises(agent.DeviceDisconnected):
            await asyncio.wait_for(task, 1)
        self.assertTrue(closed.is_set())

    async def test_connected_work_retains_result_or_error(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        self.assertEqual(await agent.while_connected(item, AsyncMock(return_value=7)()), 7)
        with self.assertRaisesRegex(ValueError, 'original error'):
            await agent.while_connected(item, AsyncMock(side_effect=ValueError('original error'))())

    async def test_heartbeat_reports_loss_once_and_waits_for_restore(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        item.command.side_effect = agent.DeviceDisconnected('gone')
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await agent.Agent.heartbeat(item)
        self.assertTrue(item.connection_lost.is_set())
        item.command.assert_awaited_once_with('local_agent status')

    async def test_cancelled_native_asr_finishes_before_recognizer_reuse(self):
        item = self.instance()
        entered, release = threading.Event(), threading.Event()

        def decode(*_):
            entered.set()
            release.wait(2)
            return '你好'

        with patch.object(agent, 'recognize', side_effect=decode):
            task = asyncio.create_task(item.turn(np.ones(16000)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        item.reply.assert_not_awaited()

    def test_counter_wrap_and_reset_are_distinct(self):
        self.assertFalse(agent.button_changed(7, 7))
        self.assertTrue(agent.button_changed(7, 9))
        self.assertTrue(agent.button_changed(0xFFFFFFFF, 0))
        with self.assertRaises(agent.DeviceDisconnected):
            agent.button_changed(9, 0)
        with self.assertRaises(agent.DeviceDisconnected):
            agent.button_sequence('LOCAL_AGENT_IDLE BUTTON=4294967296')

    async def test_counter_reset_during_playback_is_not_a_button_press(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_PLAYING BUTTON=0'
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.Agent.wait_playback(item, 9)
        item.command.assert_awaited_once_with('local_agent status')

    async def test_idle_during_pending_playback_restores_connection(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=0'
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.Agent.wait_playback(item, 0)

    async def test_counter_reset_at_rest_does_not_start_recording(self):
        item = self.instance()
        item.command.return_value = 'LOCAL_AGENT_IDLE BUTTON=0'
        conversation = agent.ConversationState(60)
        conversation.resting = True
        with patch.object(agent.asyncio, 'sleep', new=AsyncMock()), \
             patch.object(agent, 'current_mode', return_value='continuous'), \
             patch.object(agent, 'capture', new=AsyncMock()) as capture:
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.connected_loop(item, conversation, 7, 9)
        capture.assert_not_awaited()
        self.assertTrue(conversation.resting)

    async def test_mode_change_offline_takes_effect_when_restoring(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        item.connection_lost.set()
        status = 'LOCAL_AGENT_IDLE BUTTON=0 CLOUD=DISABLED PET=1 CONTROL=1'
        item.command.side_effect = [status, 'LOCAL_AGENT_STOPPING',
                                   'LOCAL_AGENT_SCENE_OK SCENE=companion', 'on', status]
        conversation = agent.ConversationState(60)
        conversation.mode = 'button'
        conversation.resting = True
        with patch.object(agent, 'current_mode', return_value='continuous'), \
             patch.object(agent, 'refresh_input_device', return_value=7):
            self.assertEqual(await agent.connect_device(item, conversation), (7, 0))
        self.assertEqual(conversation.mode, 'continuous')
        self.assertFalse(conversation.resting)
        self.assertFalse(item.connection_lost.is_set())
        item.ui.assert_awaited_once_with('idle')

    async def test_partial_restore_does_not_release_connection_loss(self):
        item = self.instance()
        item.connection_lost = asyncio.Event()
        item.connection_lost.set()
        status = 'LOCAL_AGENT_IDLE BUTTON=0 CLOUD=DISABLED PET=1 CONTROL=1'
        item.command.side_effect = [status, 'LOCAL_AGENT_STOPPING',
                                   'LOCAL_AGENT_SCENE_OK SCENE=companion', 'on', 'incomplete', 'incomplete', 'incomplete']
        with patch.object(agent, 'refresh_input_device', return_value=7):
            with self.assertRaises(agent.DeviceDisconnected):
                await agent.connect_device(item, agent.ConversationState(60))
        self.assertTrue(item.connection_lost.is_set())

    def test_missing_ack_is_loss_but_explicit_rejection_is_error(self):
        with self.assertRaises(agent.DeviceDisconnected):
            agent.require_ack('', 'LOCAL_AGENT_STOPPING', 'stop')
        with self.assertRaises(RuntimeError) as caught:
            agent.require_ack('LOCAL_AGENT_ERROR', 'LOCAL_AGENT_QUEUED', 'play')
        self.assertNotIsInstance(caught.exception, agent.DeviceDisconnected)


if __name__ == '__main__':
    unittest.main()
