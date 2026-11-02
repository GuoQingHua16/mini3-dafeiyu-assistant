"""Bounded PC cache of fixed local prompts, never ordinary conversations."""
import hashlib
import json
from pathlib import Path
import re


FIXED_PROMPTS = frozenset({
    '请设置百分之十到百分之一百之间的数值。',
    '已切换到陪伴聊天。', '已切换到英语陪练。', '已切换到故事模式。',
    '换成开心表情啦。', '换成难过表情啦。', '换成生气表情啦。', '换成正常表情啦。',
    '计时时间到了。', '番茄钟时间到了，休息一下吧。',
    '目前没有计时或提醒。', '没有找到对应的计时或提醒。',
    '该提醒已经处于这个状态。', '二十五分钟番茄钟已开始，到点提醒你休息。',
    '本地配置保存失败，请检查电脑存储。',
})


def cacheable_prompt(text):
    if text in FIXED_PROMPTS:
        return True
    match = re.fullmatch(r'(?:屏幕亮度|音量)已设为百分之(\d{2,3})。', text)
    return bool(match and 10 <= int(match[1]) <= 100)


class PromptCache:
    def __init__(self, root, max_entries=64, max_bytes=4 * 1024 * 1024):
        self.root = Path(root)
        self.max_entries = max_entries
        self.max_bytes = max_bytes

    @staticmethod
    def key(text, voice):
        return hashlib.sha256(('v1\0' + voice + '\0' + text).encode('utf-8')).hexdigest()

    def get(self, text, voice):
        if not cacheable_prompt(text):
            return None
        key = self.key(text, voice)
        try:
            metadata = json.loads((self.root / (key + '.json')).read_text(encoding='utf-8'))
            audio_path = self.root / (key + '.mp3')
            if not 128 <= audio_path.stat().st_size <= 512 * 1024:
                return None
            audio = audio_path.read_bytes()
            if (not isinstance(metadata, dict) or metadata.get('version') != 1
                    or metadata.get('bytes') != len(audio)
                    or metadata.get('sha256') != hashlib.sha256(audio).hexdigest()):
                return None
            try:
                audio_path.touch()
            except OSError:
                pass
            return audio
        except (OSError, ValueError):
            return None

    def put(self, text, voice, audio):
        # The caller only stores complete synthesized audio after device DONE.
        if not cacheable_prompt(text) or not 128 <= len(audio) <= 512 * 1024:
            return False
        key = self.key(text, voice)
        temporary_audio = self.root / (key + '.audio.tmp')
        temporary_meta = self.root / (key + '.meta.tmp')
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary_audio.write_bytes(audio)
            temporary_meta.write_text(json.dumps({'version': 1, 'bytes': len(audio),
                'sha256': hashlib.sha256(audio).hexdigest()}), encoding='utf-8')
            temporary_audio.replace(self.root / (key + '.mp3'))
            temporary_meta.replace(self.root / (key + '.json'))
            self.prune()
            return True
        except OSError:
            # Cache storage must not turn a successful spoken answer into failure.
            return False
        finally:
            for path in (temporary_audio, temporary_meta):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def prune(self):
        files = [p for p in self.root.glob('*.mp3') if re.fullmatch(r'[0-9a-f]{64}\.mp3', p.name)]
        files.sort(key=lambda p: p.stat().st_mtime)
        sizes = {p: p.stat().st_size for p in files}
        total = sum(sizes.values())
        while files and (len(files) > self.max_entries or total > self.max_bytes):
            path = files.pop(0)
            total -= sizes[path]
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
