"""Mini3 USB microphone -> local SenseVoice -> DeepSeek -> edge-tts -> Mini3.

No ListenAI cloud requests. Requires the local_agent firmware playback command.
"""
import argparse
import atexit
import sys
import asyncio
import contextlib
import getpass
import json
import os
import re
import ipaddress
from pathlib import Path
import secrets
import subprocess
import time
import math

import aiohttp
from aiohttp import web
import edge_tts
import numpy as np
import sherpa_onnx
import sounddevice as sd
import soundfile as sf
from commands import BASE_PROMPT, PROFILES, parse_command
from utilities import Utilities, parse_utility
from networking import resolve_host_ip
from display_info import hud_command
from prompt_cache import PromptCache
from adb_link import Mini3Link, ADBLinkError
from demo_events import emit_event

ROOT = Path(__file__).resolve().parent
MODEL = ROOT.parent / 'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
ADB = ROOT.parent / 'tools/platform-tools/adb.exe'
USB_LINK = Mini3Link(ADB, ROOT / 'runtime/usb-adb.lock')
atexit.register(USB_LINK.close)
MODE_FILE = ROOT / 'runtime/mode.txt'
STOP_FILE = ROOT / 'runtime/stop.request'
VAD_MODEL = ROOT.parent / 'models/vad/silero_vad.onnx'


class ListeningExpired(Exception):
    """The continuous conversation window ended without another valid turn."""


class DeviceDisconnected(RuntimeError):
    """A transient USB/ADB/audio loss; never retry a conversation request."""


class PlaybackInterrupted(Exception):
    """Playback was stopped by a fresh main-button click."""


class ReminderPending(Exception):
    """Yield quiet capture so the main loop can deliver a due reminder."""


def reminder_due(agent):
    utilities = getattr(agent, 'utilities', None)
    return utilities is not None and utilities.reminders.next_due() is not None


def wants_rest(text):
    # Match a complete instruction, not quoted/negated words inside a question.
    if any(c in text for c in '\"“”‘’「」'):
        return False
    clean = re.sub(r'[\s，。！？、,.!?]', '', text)
    return re.fullmatch(
        r'(?:(?:小聆|小林)[啊呀]?)?(?:请你|请)?(?:'
        r'先不聊了|不聊了|今天先聊到这里|先聊到这里|'
        r'(?:你)?(?:先)?休息(?:一下|一会儿|一会)?(?:吧|啦)?|'
        r'(?:先)?别听了|不要再听了|不用再听了|停止(?:聆听|监听|听我说话)|'
        r'暂停(?:聆听|监听))', clean) is not None


SHORT_ACKS = {'嗯', '嗯嗯', '嗯哼', '啊', '哦', '喔', '噢', '唔', '呃', '额', 'うん',
              '好', '好的', '好啊', '好吧', '是', '是的', '对', '对的', '没错',
              '不', '不是', 'ok', 'okay', 'yes', 'yeah', 'yep', 'sure', 'no'}


def skip_short_input(text, history, context_age, mode):
    """Only suppress isolated acknowledgements during unattended listening."""
    clean = re.sub(r'[\s，。！？、,.!?]', '', text).casefold()
    if clean not in SHORT_ACKS or mode == 'button':
        return False
    if not history or context_age > 60 or history[-1]['role'] != 'assistant':
        return True
    answer = history[-1]['content'].strip()
    # A character's quoted question in a story is not a question to the user.
    answer = re.sub(r'“[^”]*”|「[^」]*」|\"[^\"]*\"', '', answer)
    # A question or speaking exercise at the end creates a pending user turn.
    tail = re.split(r'[。！!.\n]', answer.rstrip('。！!.\n '))[-1].strip()
    expecting = bool(re.search(r'[?？][\s\"”’]*$', tail) or re.search(
        r'^(?:请|试着|轮到你).*(?:说|回答|跟读|翻译)|'
        r'^(?:please\s+)?(?:try|repeat|say|translate|tell me|your turn)\b', tail, re.I))
    return not expecting


def button_sequence(status):
    match = re.search(r'BUTTON=(\d+)\b', status)
    if not match or 'LOCAL_AGENT_' not in status or int(match[1]) > 0xFFFFFFFF:
        raise DeviceDisconnected('Mini3 status response incomplete')
    return int(match[1])


def button_changed(previous, current):
    # Firmware counter is uint32. Wraparound is a click; a backwards jump is
    # usually a board restart, and must not masquerade as a request to record.
    delta = (current - previous) & 0xFFFFFFFF
    if delta >= 0x80000000:
        raise DeviceDisconnected('Mini3 button counter reset; restore connection')
    return delta != 0


def complete_status(output, compact=False):
    for line in reversed(re.split(r'[\r\n]+', output)):
        if '\0' in line or not re.match(r'^LOCAL_AGENT_(?:IDLE|QUEUED|PLAYING|DONE|ERROR)\b', line):
            continue
        if compact and not re.fullmatch(
                r'LOCAL_AGENT_(?:IDLE|QUEUED|PLAYING|DONE|ERROR) BUTTON=\d+ '
                r'CLOUD=DISABLED PET=1 CONTROL=1 PHASE=[0-9]', line):
            continue
        button_sequence(line)
        return line
    raise DeviceDisconnected('Mini3 status response incomplete')


async def device_status(agent):
    compact = getattr(agent, 'poll_supported', False)
    command = 'local_agent poll' if compact else 'local_agent status'
    # Only repeat a successful read with a malformed response. Transport loss
    # and all mutating commands keep their existing no-retry behavior.
    for attempt in range(3):
        response = await agent.command(command)
        try:
            line = complete_status(response, compact)
        except DeviceDisconnected:
            if attempt == 2:
                raise
            await asyncio.sleep(.06 * (attempt + 1))
        else:
            agent.last_status_at = time.monotonic()
            agent.last_device_status = line
            if attempt:
                print('状态回读不完整，重读后恢复；未重发操作或对话。')
            return line


def require_ack(response, marker, description):
    if marker not in response:
        if USB_LINK.connected:
            with contextlib.suppress(OSError):
                (ROOT / 'runtime/usb-ack-failure.json').write_text(json.dumps({
                    'description': description, 'expected': marker, 'response': response,
                    'exchange': USB_LINK.last_exchange, 'tail': USB_LINK.diagnostic_tail()
                }, ensure_ascii=False, indent=2), encoding='utf-8')
        if 'LOCAL_AGENT_' not in response:
            raise DeviceDisconnected('Mini3 response incomplete: ' + description)
        raise RuntimeError('Mini3 did not acknowledge ' + description)


async def while_connected(agent, operation):
    """Cancel active capture/turn when heartbeat reports link loss."""
    lost = getattr(agent, 'connection_lost', None)
    if lost is None:
        return await operation
    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(lost.wait())
    try:
        # Cancel before giving the task a turn if loss is already known.
        if not lost.is_set():
            await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if lost.is_set():
            raise DeviceDisconnected('Mini3 connection lost during active turn')
        return await work
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        for task in (work, watcher):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def validate_firmware(status):
    button_sequence(status)
    if not all(marker in status for marker in ('CLOUD=DISABLED', 'PET=1', 'CONTROL=1')):
        raise RuntimeError('Install the Mini3 local pet and control firmware first')


class ConversationState:
    def __init__(self, timeout):
        self.timeout = timeout
        self.resting = False
        self.mode = None
        self.deadline = time.monotonic() + timeout

    def resume(self):
        self.resting = False
        self.deadline = time.monotonic() + self.timeout

    async def rest(self, agent, reason):
        self.resting = True
        await agent.ui('sleeping')
        emit_event(agent, 'rested', reason=reason)
        print('已休息，停止采音；按主按键继续。原因：', reason)


def current_mode(default):
    try:
        mode = MODE_FILE.read_text(encoding='utf-8-sig').strip()
        return mode if mode in {'button', 'continuous'} else default
    except OSError:
        return default


class SpeechEndpoint:
    """Local neural speech detection; no ASR or cloud request for rejected noise."""
    def __init__(self, seconds=15, silence=0.9, vad=None, threshold=0.65, min_rms=0):
        if vad is None:
            if not VAD_MODEL.is_file():
                raise RuntimeError('Local Silero VAD model missing: ' + str(VAD_MODEL))
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(VAD_MODEL)
            config.silero_vad.threshold = threshold
            config.silero_vad.min_speech_duration = 0.32
            config.silero_vad.min_silence_duration = silence
            config.silero_vad.max_speech_duration = seconds
            config.sample_rate = 16000
            config.num_threads = 1
            vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.vad = vad
        self.limit = int(seconds * 16000)
        self.started = False
        self.active_samples = 0
        self.min_rms = min_rms

    def feed(self, frame):
        samples = np.frombuffer(frame, dtype='<i2').astype(np.float32) / 32768
        # Gate only speech onset: preserve quiet syllables once a sentence starts.
        # Remove DC for the level check, so a microphone offset cannot open it.
        rms = float(np.std(samples))
        detected_samples = np.zeros_like(samples) if not self.started and rms < self.min_rms else samples
        self.vad.accept_waveform(detected_samples)
        if self.vad.is_speech_detected():
            self.started = True
        if self.started:
            self.active_samples += len(samples)
            if self.active_samples >= self.limit:
                self.vad.flush()
        if not self.vad.empty():
            # Copy before pop invalidates the native segment reference.
            result = np.array(self.vad.front.samples, dtype=np.float32)[:self.limit].copy()
            self.vad.pop()
            return result
        return None


async def capture(agent, device, mode, deadline=None):
    """Keep capture in an async scope: cancellation closes the stream immediately."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=100)
    overflow = False

    def deliver(frame, bad):
        nonlocal overflow
        if bad or queue.full():
            overflow = True
        else:
            queue.put_nowait(frame)

    def callback(data, frames, timing, status):
        # Copy only the physical microphone, never the speaker reference channels.
        frame = np.ascontiguousarray(data[:, 0], dtype='<i2').tobytes()
        loop.call_soon_threadsafe(deliver, frame, bool(status))

    endpoint = SpeechEndpoint(agent.args.seconds, agent.args.silence,
                              threshold=getattr(agent.args, 'vad_threshold', 0.65),
                              min_rms=getattr(agent.args, 'min_speech_rms', 0.003) if mode == 'continuous' else 0)
    began = time.monotonic()
    last_mode_check = began
    with sd.InputStream(device=device, samplerate=16000, channels=4,
                        dtype='int16', blocksize=480, callback=callback):
        if mode == 'continuous':
            await agent.ui('ready', '请直接说话')
        else:
            await agent.ui('listening', seconds=math.ceil(agent.args.wait_seconds))
        while True:
            now = time.monotonic()
            if now - last_mode_check >= 0.2:
                last_mode_check = now
                if current_mode(mode) != mode:
                    return None
            if mode == 'button' and not endpoint.started and now - began >= agent.args.wait_seconds:
                return None
            if mode == 'continuous' and deadline is not None and now >= deadline and not endpoint.started:
                raise ListeningExpired()
            if not endpoint.started and reminder_due(agent):
                raise ReminderPending()
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                raise DeviceDisconnected('Mini3 microphone stopped delivering audio') from None
            if overflow:
                raise RuntimeError('Microphone capture overflow; discarded incomplete audio')
            was_started = endpoint.started
            result = endpoint.feed(frame)
            if endpoint.started and not was_started:
                print('检测到讲话，停顿后自动结束录音。')
                await agent.ui('listening', seconds=math.ceil(agent.args.seconds))
            if result is not None:
                return result


def adb(command):
    try:
        output = USB_LINK.execute(command)
    except ADBLinkError as exc:
        raise DeviceDisconnected(str(exc)) from exc
    if 'Command not Found' in output:
        raise RuntimeError('Mini3 ADB command failed: ' + command.split()[0])
    return output


def input_device():
    # Never silently use the computer microphone. MME exposes all four channels.
    for i, d in enumerate(sd.query_devices()):
        if 'CherryUSB UAC1 Audio' in d['name'] and d['max_input_channels'] >= 4:
            if sd.query_hostapis(d['hostapi'])['name'] == 'MME':
                return i
    raise DeviceDisconnected('Mini3 USB microphone not found; connect USB and power on.')


def refresh_input_device():
    # PortAudio/MME caches devices. Refresh only when no InputStream is open.
    if sd._initialized:
        sd._terminate()
    sd._initialize()
    return input_device()


def recognize(recognizer, samples):
    if len(samples) < 1600 or len(samples) > 16000 * 20:
        raise ValueError('Audio must be 0.1 to 20 seconds')
    if not np.isfinite(samples).all():
        raise ValueError('Invalid audio samples')
    if float(np.max(np.abs(samples))) < 0.003:
        return ''
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()
    # SenseVoice can emit punctuation for background sounds. Such fragments
    # carry no query and must not trigger an automatically paid conversation.
    return text if any(char.isalnum() for char in text) else ''


def write_usb_diagnostics(agent):
    if not getattr(agent, 'usb_diagnostics', False):
        return
    last = getattr(agent, 'last_device_status', '')
    match = re.search(r'PHASE=(\d+)', last)
    age = time.monotonic() - agent.last_status_at if getattr(agent, 'last_status_at', None) is not None else None
    payload = {'updated': time.time(), 'pid': os.getpid(),
               'transport': 'persistent-shell-v1',
               'filming_events': bool(getattr(agent, 'events_enabled', False)),
               'connected': USB_LINK.connected and not agent.connection_lost.is_set() and age is not None and age < 6,
               'phase': int(match[1]) if match else None,
               'mode': getattr(agent, 'listen_mode', None), 'tf_enabled': False,
               'last_read_age_seconds': round(age, 2) if age is not None else None,
               'stats': dict(USB_LINK.stats)}
    path = ROOT / 'runtime/usb-link-status.json'
    temporary = path.with_suffix('.' + str(os.getpid()) + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        # A diagnostic reader must never interrupt microphone/playback work.
        with contextlib.suppress(OSError): temporary.unlink()


class Agent:
    def __init__(self, args):
        self.args = args
        self.key = os.environ.get('DEEPSEEK_API_KEY', '')
        self.history = []
        self.profile = getattr(args, 'profile', 'companion')
        self.audio = {}
        self.usage = None
        self.last_answer_at = None
        self.listen_mode = args.mode
        self.adb_lock = asyncio.Lock()
        self.connection_lost = asyncio.Event()
        self.utilities = Utilities(ROOT / 'runtime')
        self.hud_supported = False
        self.buffered_audio = False
        self.poll_supported = False
        self.tf_supported = False
        self.usb_diagnostics = True
        self.events_enabled = True
        self.active_turn_id = None
        self._turn_sequence = 0
        self.last_status_at = None
        self.prompt_cache = PromptCache(ROOT / 'runtime/prompt-audio')
        self.asr = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(MODEL / 'model.int8.onnx'), tokens=str(MODEL / 'tokens.txt'),
            num_threads=2, provider='cpu', language='auto', use_itn=True)

    async def local_action(self, action):
        self.last_answer_at = None
        if action.kind == 'invalid':
            await self.speak('请设置百分之十到百分之一百之间的数值。')
            return
        if action.kind == 'profile':
            response = await self.command('local_agent scene ' + action.target)
            require_ack(response, f'LOCAL_AGENT_SCENE_OK SCENE={action.target}', 'conversation mode')
            self.profile = action.target
            self.history.clear()
            emit_event(self, 'control_verified', kind='profile', target=action.target)
            await self.speak('已切换到' + PROFILES[self.profile][0] + '。')
        elif action.kind == 'mood':
            response = await self.command('local_agent mood ' + action.target)
            require_ack(response, f'LOCAL_AGENT_MOOD_OK MOOD={action.target}', 'expression')
            emit_event(self, 'control_verified', kind='mood', target=action.target)
            names = {'happy': '开心', 'sad': '难过', 'angry': '生气', 'neutral': '正常'}
            await self.speak('换成' + names[action.target] + '表情啦。')
        elif action.kind == 'setting':
            response = await self.command(f'local_agent control {action.target} {action.operation} {action.value}')
            match = re.search(rf'LOCAL_AGENT_CONTROL_OK {action.target.upper()}=(\d+)\b', response)
            if not match or not 10 <= int(match[1]) <= 100:
                require_ack(response, 'LOCAL_AGENT_CONTROL_OK', 'setting')
                raise RuntimeError('Mini3 did not acknowledge setting; no success confirmation')
            name = '屏幕亮度' if action.target == 'brightness' else '音量'
            emit_event(self, 'control_verified', kind='setting', target=action.target, value=int(match[1]))
            await self.speak(f'{name}已设为百分之{int(match[1])}。')

    async def command(self, command):
        # Firmware shell output uses one active session. Never overlap ADB calls.
        async with self.adb_lock:
            operation = asyncio.create_task(asyncio.to_thread(adb, command))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Cancelling a thread await does not stop subprocess.run(). Wait
                # before releasing the lock so shutdown cannot steal its output.
                with contextlib.suppress(Exception):
                    await operation
                raise

    async def ui(self, phase, text='', seconds=0):
        command = pet_command(phase, text, seconds)
        result = await self.command(command)
        require_ack(result, 'LOCAL_AGENT_UI_OK', 'pet UI state')

    async def heartbeat(self):
        while True:
            await asyncio.sleep(2)
            if self.connection_lost.is_set():
                write_usb_diagnostics(self)
                continue
            try:
                last = getattr(self, 'last_status_at', None)
                if last is None or time.monotonic() - last >= 1:
                    await device_status(self)
                if getattr(self, 'hud_supported', False):
                    await self.update_hud()
            except RuntimeError:
                # The device expires its heartbeat and shows offline in 12 seconds.
                self.connection_lost.set()
            finally:
                write_usb_diagnostics(self)

    async def update_hud(self):
        response = await self.command(hud_command(self.utilities.reminders))
        require_ack(response, 'LOCAL_AGENT_HUD_OK', 'clock and countdown')

    async def audio_response(self, request):
        payload = self.audio.get(request.match_info['token'])
        if not payload or time.monotonic() > payload[1]:
            raise web.HTTPNotFound()
        return web.Response(body=payload[0], content_type='audio/mpeg')

    async def reply(self, text):
        lost = getattr(self, 'connection_lost', None)
        if lost is not None and lost.is_set():
            raise DeviceDisconnected('Mini3 disconnected; skip paid request')
        _, instruction, max_tokens = PROFILES[self.profile]
        messages = [{'role': 'system', 'content': BASE_PROMPT + instruction}] + self.history[-6:] + [
            {'role': 'user', 'content': text[:1000]}]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
            async with session.post('https://api.deepseek.com/chat/completions',
                headers={'Authorization': 'Bearer ' + self.key}, json={
                    'model': self.args.model, 'messages': messages,
                    'max_tokens': max_tokens, 'thinking': {'type': 'disabled'}, 'stream': False}) as response:
                if response.status != 200:
                    raise RuntimeError(f'DeepSeek HTTP {response.status}; no automatic retry')
                data = await response.json()
        answer = data['choices'][0]['message']['content']
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError('DeepSeek returned no spoken answer')
        self.usage = data.get('usage')
        return answer[:600], messages[-1]

    async def speak(self, text, caption=None):
        emit_event(self, 'speech_preparing', text=text[:600])
        await self.ui('synthesizing', text if caption is None else caption)
        cache = getattr(self, 'prompt_cache', None)
        mp3 = cache.get(text, self.args.voice) if cache else None
        cached = mp3 is not None
        if cached:
            print('使用电脑本地固定提示音缓存。')
        else:
            chunks = []
            async with asyncio.timeout(45):
                async for part in edge_tts.Communicate(text, self.args.voice).stream():
                    if part['type'] == 'audio':
                        chunks.append(part['data'])
            mp3 = b''.join(chunks)
        if not mp3:
            raise RuntimeError('edge-tts returned no audio')
        if getattr(self, 'buffered_audio', False) and len(mp3) > 512 * 1024:
            raise RuntimeError('这段回答的语音过长，请分成几段再播放。')
        # Baseline immediately before play: clicks during thinking are discarded.
        button = button_sequence(await device_status(self))
        token = secrets.token_hex(16) + '.mp3'
        self.audio.clear()
        self.audio[token] = (mp3, time.monotonic() + 120)
        url = f'http://{self.args.host_ip}:{self.args.port}/audio/{token}'
        try:
            response = await self.command('local_agent play ' + url)
            require_ack(response, 'LOCAL_AGENT_QUEUED', 'playback')
            await self.wait_playback(button)
            emit_event(self, 'playback_finished')
            if cache and not cached:
                cache.put(text, self.args.voice, mp3)
        finally:
            self.audio.clear()

    async def wait_playback(self, button):
        # Keep the microphone closed until stop/completion and acoustic cooldown.
        deadline = time.monotonic() + 90
        reported_playing = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            status = await device_status(self)
            current = button_sequence(status)
            if not reported_playing and 'PHASE=5' in status:
                emit_event(self, 'playback_started')
                reported_playing = True
            if 'LOCAL_AGENT_IDLE' in status:
                raise DeviceDisconnected('Mini3 playback state reset; restore connection')
            if button_changed(button, current):
                result = await self.command('local_agent stop')
                require_ack(result, 'LOCAL_AGENT_STOPPING', 'playback stop')
                await self.wait_audio_idle()
                emit_event(self, 'playback_stopped', button=current)
                raise PlaybackInterrupted()
            if 'LOCAL_AGENT_DONE' in status:
                return
            if 'LOCAL_AGENT_ERROR' in status:
                raise RuntimeError('Mini3 playback failed; check LAN and firewall')
        raise RuntimeError('Mini3 playback timeout')

    async def wait_audio_idle(self):
        # Cancellation can arrive during an HTTP read. Keep capture closed
        # until the worker has stopped its reader and released the MP3 source.
        if not getattr(self, 'buffered_audio', False):
            return
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            response = await self.command('local_agent audio')
            require_ack(response, 'LOCAL_AGENT_AUDIO BUFFER=1', 'buffered audio state')
            if re.search(r'\bBUSY=0\b', response):
                return
            await asyncio.sleep(.1)
        raise RuntimeError('Mini3 audio cleanup timeout')

    async def turn(self, samples):
        started = time.monotonic()
        await self.ui('thinking', '正在识别你的声音')
        # Native ASR continues on its worker after cancellation. Drain it before
        # reuse so reconnecting cannot run two decodes on one recognizer.
        decoding = asyncio.create_task(asyncio.to_thread(recognize, self.asr, samples))
        try:
            text = await asyncio.shield(decoding)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await decoding
            raise
        if not text:
            print('未识别到语音，不调用 API。')
            await self.ui('error', '没有听清，再说一次吧')
            return
        print('你：', text)
        return await self.respond(text, started)

    async def respond(self, text, started=None):
        self._turn_sequence = getattr(self, '_turn_sequence', 0) + 1
        self.active_turn_id = f'{os.getpid()}:{self._turn_sequence}'
        emit_event(self, 'turn_started', text=text[:1000])
        try:
            result = await Agent._respond_impl(self, text, started)
            emit_event(self, 'turn_finished', outcome=result, profile=getattr(self, 'profile', None))
            return result
        except PlaybackInterrupted:
            emit_event(self, 'turn_interrupted')
            raise
        except (Exception, asyncio.CancelledError) as exc:
            emit_event(self, 'turn_failed', reason=type(exc).__name__ + ': ' + str(exc)[:240])
            raise
        finally:
            self.active_turn_id = None

    async def _respond_impl(self, text, started=None):
        started = time.monotonic() if started is None else started
        if wants_rest(text):
            self.last_answer_at = None
            print('识别到休息指令，不调用对话 API。')
            return 'rest'
        action = parse_command(text)
        if action:
            self.last_answer_at = None
            print('本地命令：', action)
            await self.local_action(action)
            return 'replied'
        utility = parse_utility(text)
        if utility:
            self.last_answer_at = None
            print('本地实用模块：', utility.kind, utility.operation)
            observing_reminders = utility.kind == 'reminder' and getattr(self, 'events_enabled', False)
            previous_ids = {item.id for item in self.utilities.reminders.items} if observing_reminders else set()
            result = await self.utilities.apply(utility)
            created = [{'id': item.id, 'kind': item.kind} for item in self.utilities.reminders.items
                       if item.id not in previous_ids] if observing_reminders else []
            emit_event(self, 'utility_verified', kind=utility.kind, operation=utility.operation,
                       text=result.text[:400], created=created)
            if utility.kind == 'reminder' and getattr(self, 'hud_supported', False):
                await self.update_hud()
            print('小聆：', result.text)
            await self.speak(result.text, caption=result.caption)
            return 'replied'
        last_answer = getattr(self, 'last_answer_at', None)
        age = time.monotonic() - last_answer if last_answer is not None else math.inf
        if skip_short_input(text, getattr(self, 'history', []), age,
                            getattr(self, 'listen_mode', 'button')):
            print('孤立确认词/语气词，继续等待；不调用对话 API。')
            return 'ignored'
        # An accepted new turn consumes the old confirmation context, even if
        # the following request or playback fails.
        self.last_answer_at = None
        print('识别耗时：', round(time.monotonic() - started, 2), '秒，正在请求回答。')
        await self.ui('thinking', text)
        answer, user = await self.reply(text)
        emit_event(self, 'assistant_text', text=answer[:600])
        print('小聆：', answer)
        print('开始合成与播放，距录音结束：', round(time.monotonic() - started, 2), '秒')
        await self.speak(answer)
        self.history = (self.history + [user, {'role': 'assistant', 'content': answer}])[-6:]
        self.last_answer_at = time.monotonic()
        print('本轮用量：', json.dumps(self.usage, ensure_ascii=False))
        print('设备报告播放完成，距录音结束：', round(time.monotonic() - started, 2), '秒')
        return 'replied'


async def announce_reminder(agent, conversation):
    reminders = agent.utilities.reminders
    item = reminders.next_due()
    if item is None:
        return
    agent.last_answer_at = None
    result = reminders.announcement(item)
    print('到点提醒：', item.label)
    interrupted = False
    try:
        await agent.speak(result.text, caption=result.caption)
    except PlaybackInterrupted:
        # The button dismisses this notification and starts a fresh interaction.
        interrupted = True
    except (RuntimeError, aiohttp.ClientError, TimeoutError):
        reminders.defer(item)
        raise
    try:
        reminders.delivered(item)
        emit_event(agent, 'reminder_delivered', reminder_id=item.id, kind=item.kind,
                   label=item.label, interrupted=interrupted)
    except OSError as exc:
        # Keep the saved task and the service alive if storage becomes unwritable.
        reminders.defer(item)
        if not interrupted:
            raise RuntimeError('提醒完成状态保存失败，一分钟后重试，请检查电脑存储。') from exc
        print('提醒完成状态保存失败，保留原任务；仍响应按键接收新问题。')
    if interrupted:
        raise PlaybackInterrupted()
    await asyncio.sleep(0.6)
    await agent.ui('sleeping' if conversation.resting else 'idle')


async def connect_device(agent, conversation):
    if hasattr(agent, 'poll_supported'):
        agent.poll_supported = False
    status = await device_status(agent)
    validate_firmware(status)
    if hasattr(agent, 'hud_supported'):
        capabilities = await agent.command('local_agent info')
        if 'local_agent status' in capabilities or 'Command not Found' in capabilities:
            raise RuntimeError('当前 Mini3 固件没有时钟/倒计时接口，请先更新本项目 CP 固件。')
        require_ack(capabilities, 'HUD=1', 'clock/countdown firmware; update Mini3 CP first')
        agent.hud_supported = True
        agent.buffered_audio = 'AUDIO_BUFFER=1' in capabilities
        agent.poll_supported = 'POLL=1' in capabilities
        # TF experiments were stopped by the user, regardless of device flags.
        agent.tf_supported = False
        await agent.update_hud()
    response = await agent.command('local_agent stop')
    require_ack(response, 'LOCAL_AGENT_STOPPING', 'playback stop')
    if getattr(agent, 'buffered_audio', False):
        await agent.wait_audio_idle()
    response = await agent.command('local_agent scene ' + agent.profile)
    require_ack(response, f'LOCAL_AGENT_SCENE_OK SCENE={agent.profile}', 'conversation mode')
    await agent.command('wakeup_uac_record on')
    device = await asyncio.to_thread(refresh_input_device)
    selected = current_mode(agent.args.mode)
    if conversation.mode is not None and conversation.mode != selected:
        conversation.resume()
        print('重连期间已切换模式：', selected)
    conversation.mode = selected
    if not conversation.resting:
        conversation.resume()
    await agent.ui('sleeping' if conversation.resting else 'idle')
    # Final status verifies the link after setup and establishes a fresh button
    # baseline. Only a successful restore releases the heartbeat loss latch.
    status = await device_status(agent)
    validate_firmware(status)
    button = button_sequence(status)
    lost = getattr(agent, 'connection_lost', None)
    if lost is not None:
        lost.clear()
    emit_event(agent, 'connected', mode=selected, resting=conversation.resting)
    return device, button


async def wait_connection(agent, conversation):
    reported = False
    while True:
        try:
            result = await connect_device(agent, conversation)
        except (DeviceDisconnected, sd.PortAudioError):
            if not reported:
                print('Mini3 USB/ADB 暂不可用，停止采音并等待重连；不重发对话请求。')
                reported = True
            await asyncio.sleep(2)
        else:
            print('Mini3 已连接，恢复场景与' + ('休息状态。' if conversation.resting else '交互模式。'))
            return result


async def connected_loop(agent, conversation, device, button):
    mode = conversation.mode or current_mode(agent.args.mode)
    pending_capture = False
    while True:
        await asyncio.sleep(0.2)
        selected = current_mode(agent.args.mode)
        if selected != mode:
            mode = selected
            conversation.mode = mode
            conversation.resume()
            print('已切换模式：', mode)
            emit_event(agent, 'mode_changed', mode=mode)
            await agent.ui('idle')
        status = await device_status(agent)
        current = button_sequence(status)
        changed = button_changed(button, current)
        due = reminder_due(agent)
        if (mode == 'button' or conversation.resting) and not changed and not pending_capture and not due:
            continue
        requested_capture = changed or pending_capture
        if conversation.resting and requested_capture:
            conversation.resume()
            print('按键唤起，恢复', mode, '模式。')
        button = current
        pending_capture = False
        agent.listen_mode = mode
        try:
            if due:
                await while_connected(agent, announce_reminder(agent, conversation))
                pending_capture = requested_capture
            else:
                samples = await while_connected(agent, capture(agent, device, mode, conversation.deadline))
                outcome = await while_connected(agent, agent.turn(samples)) if samples is not None else None
                if outcome == 'replied':
                    await asyncio.sleep(0.6)
                    conversation.resume()
                if outcome == 'rest':
                    await conversation.rest(agent, '语音结束指令')
                else:
                    await agent.ui('idle')
        except ReminderPending:
            # InputStream has closed; the next iteration owns notification audio.
            pending_capture = mode == 'button' or requested_capture
        except PlaybackInterrupted:
            print('主按键停止播放，余音消退后接收新问题。')
            agent.last_answer_at = None
            await asyncio.sleep(0.6)
            conversation.resume()
            pending_capture = True
        except ListeningExpired:
            await conversation.rest(agent, '连续等待超时')
        except (DeviceDisconnected, sd.PortAudioError):
            raise
        except (RuntimeError, ValueError, aiohttp.ClientError, TimeoutError) as exc:
            print('本轮失败：', type(exc).__name__, str(exc))
            emit_event(agent, 'operation_failed', reason=type(exc).__name__ + ': ' + str(exc)[:240])
            agent.last_answer_at = None
            print('不会切换至收费识别，也不会自动重试付费请求。')
            response = await agent.command('local_agent stop')
            require_ack(response, 'LOCAL_AGENT_STOPPING', 'playback stop')
            await agent.ui('error', '暂时没成功，按键重试')
            if mode == 'continuous':
                MODE_FILE.parent.mkdir(exist_ok=True)
                MODE_FILE.write_text('button', encoding='utf-8')
                mode = 'button'
                conversation.mode = mode
                print('连续模式已暂停并切回按键模式，可用 set-mode.ps1 重新开启。')
        # Consume old clicks, but retain the explicit post-stop recording request.
        latest = button_sequence(await device_status(agent))
        button_changed(button, latest)
        button = latest


async def conversation_loop(agent):
    conversation = ConversationState(agent.args.idle_seconds)
    conversation.resting = getattr(agent.args, 'start_resting', False)
    conversation.mode = current_mode(agent.args.mode)
    while True:
        device, button = await wait_connection(agent, conversation)
        try:
            await connected_loop(agent, conversation, device, button)
        except (DeviceDisconnected, sd.PortAudioError) as exc:
            lost = getattr(agent, 'connection_lost', None)
            if lost is not None:
                lost.set()
            agent.audio.clear()
            agent.last_answer_at = None
            print('设备连接中断：', type(exc).__name__, str(exc))
            emit_event(agent, 'disconnected', reason=type(exc).__name__ + ': ' + str(exc)[:180])
            # The current turn is abandoned, including any partial recording or
            # paid answer. Reconnection only restores the next fresh interaction.


async def watch_stop_request(task, requested, token):
    while True:
        await asyncio.sleep(.2)
        try:
            matches = STOP_FILE.read_text(encoding='utf-8-sig').strip() == token
        except OSError:
            matches = False
        if matches:
            requested.set()
            task.cancel()
            return


async def run(args):
    if not args.check and not args.asr_file:
        args.host_ip = await asyncio.to_thread(resolve_host_ip, args.host_ip or 'auto')
        print('电脑音频回传 IP：', args.host_ip)
    agent = Agent(args)
    if args.check:
        SpeechEndpoint(threshold=args.vad_threshold)
        device = input_device()
        print('Mini3 USB input:', device)
        print('Local SenseVoice: loaded (CPU, 2 threads)')
        print('Local Silero VAD: loaded (CPU, threshold', args.vad_threshold, ')')
        print('DeepSeek key:', 'configured' if agent.key else 'not configured')
        print(adb('local_agent status').strip())
        return
    if args.asr_file:
        samples, rate = sf.read(args.asr_file, dtype='float32')
        if rate != 16000 or samples.ndim != 1:
            raise ValueError('ASR test requires mono 16 kHz WAV')
        print(recognize(agent.asr, samples))
        return
    if not args.host_ip:
        raise ValueError('--host-ip must be the PC LAN IPv4 address reachable by Mini3')
    if not agent.key and not args.speak:
        agent.key = getpass.getpass('DeepSeek API Key（隐藏输入，仅本进程使用）: ').strip()
    if not agent.key and not args.speak:
        raise ValueError('DeepSeek key is required')
    app = web.Application()
    app.router.add_get('/audio/{token}', agent.audio_response)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, args.host_ip, args.port).start()
    heartbeat = asyncio.create_task(agent.heartbeat())
    stop_requested = asyncio.Event()
    token = getattr(args, 'control_token', None)
    stopping = asyncio.create_task(watch_stop_request(asyncio.current_task(), stop_requested, token)) if token else None
    try:
        if args.speak or args.text:
            conversation = ConversationState(args.idle_seconds)
            await connect_device(agent, conversation)
            try:
                outcome = None
                if args.speak:
                    await agent.speak(args.speak)
                else:
                    agent.listen_mode = current_mode(args.mode)
                    outcome = await agent.respond(args.text)
                if outcome == 'rest':
                    await conversation.rest(agent, '语音结束指令')
            except PlaybackInterrupted:
                print('测试播放已被主按键停止。')
            return
        print('当前模式：', current_mode(args.mode), '；说完安静', args.silence,
              '秒结束，最长', args.seconds, '秒；Ctrl+C 退出。')
        await conversation_loop(agent)
    except asyncio.CancelledError:
        if not stop_requested.is_set():
            raise
        print('收到停止请求，关闭采音、播放与 HTTP 服务。')
    finally:
        original_failure = sys.exc_info()[1]
        if stopping is not None:
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
        try:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        finally:
            try:
                try:
                    await agent.command('local_agent stop')
                    await agent.ui('offline')
                except (RuntimeError, sd.PortAudioError) as cleanup_error:
                    print('退出时设备清理未完整确认：', str(cleanup_error))
                    if original_failure is None:
                        raise
            finally:
                agent.audio.clear()
                await runner.cleanup()
                USB_LINK.close()
                write_usb_diagnostics(agent)


def record(device, seconds):
    raw = sd.rec(int(16000 * seconds), samplerate=16000, channels=4,
                 dtype='int16', device=device, blocking=True)
    # Channel 0 is the physical microphone; do not mix with speaker reference.
    return raw[:, 0].astype(np.float32) / 32768


def pet_command(phase, text='', seconds=0):
    if phase not in {'offline', 'idle', 'listening', 'thinking', 'synthesizing', 'speaking', 'error', 'sleeping', 'ready'}:
        raise ValueError('Unknown pet phase')
    if not isinstance(seconds, int) or not 0 <= seconds <= 20:
        raise ValueError('Invalid pet recording duration')
    clean = ''.join(c if c.isprintable() else ' ' for c in text)
    clipped = clean.encode('utf-8')[:42].decode('utf-8', errors='ignore').encode('utf-8')
    # Hex keeps arbitrary speech out of shell quoting and the 128-byte shell limit.
    return f'local_agent ui {phase} {seconds}' + (' ' + clipped.hex() if clipped else '')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-ip', default='auto', help='PC LAN IPv4; auto selects one active physical interface')
    parser.add_argument('--control-token', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--mode', choices=['button', 'continuous'], default='button')
    parser.add_argument('--start-resting', action='store_true',
                        help='Start with capture closed; main button resumes the selected mode')
    parser.add_argument('--disable-tf', action='store_true', default=True,
                        help='Compatibility flag; TF experiments are disabled')
    parser.add_argument('--profile', choices=list(PROFILES), default='companion')
    parser.add_argument('--seconds', type=float, default=15, help='Maximum utterance duration')
    parser.add_argument('--silence', type=float, default=0.9, help='End after this many seconds of silence')
    parser.add_argument('--vad-threshold', type=float, default=0.65, help='Local speech probability threshold')
    parser.add_argument('--min-speech-rms', type=float, default=0.003,
                        help='Continuous mode onset volume floor; 0 disables, does not identify speakers')
    parser.add_argument('--wait-seconds', type=float, default=6, help='Button mode speech onset timeout')
    parser.add_argument('--idle-seconds', type=float, default=60, help='Continuous listening inactivity timeout')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--voice', default='zh-CN-XiaoxiaoNeural')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--asr-file')
    parser.add_argument('--speak', help='Test edge-tts and Mini3 playback without DeepSeek')
    parser.add_argument('--text', help='Text input: local command or one DeepSeek playback test')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 20:
        parser.error('--seconds must be 1..20')
    if not 0.3 <= args.silence <= 3 or args.silence >= args.seconds:
        parser.error('--silence must be 0.3..3 and less than --seconds')
    if not 1 <= args.wait_seconds <= 20:
        parser.error('--wait-seconds must be 1..20')
    if not 5 <= args.idle_seconds <= 3600:
        parser.error('--idle-seconds must be 5..3600')
    if not 0.1 <= args.vad_threshold <= 0.95:
        parser.error('--vad-threshold must be 0.1..0.95')
    if not 0 <= args.min_speech_rms <= 0.1:
        parser.error('--min-speech-rms must be 0..0.1')
    if args.host_ip and args.host_ip != 'auto':
        ipaddress.IPv4Address(args.host_ip)
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be 1024..65535')
    return args


if __name__ == '__main__':
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError, aiohttp.ClientError, TimeoutError, sd.PortAudioError) as exc:
        raise SystemExit(f'启动或运行失败：{type(exc).__name__}: {exc}') from None
