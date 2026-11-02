"""Explicit local reminders persisted on the PC, delivered by the main loop."""
from dataclasses import dataclass, asdict
import json
import math
from pathlib import Path
import secrets
import time
import copy

from local_tools import UtilityResult, duration, duration_words


@dataclass
class Reminder:
    id: str
    label: str
    kind: str
    due: float | None
    remaining: float | None = None


class Reminders:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.items = []
        self.retry_after = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data['items'], list) or len(data['items']) > 10:
                    raise ValueError('Invalid reminder file')
                for row in data['items']:
                    reminder = Reminder(**row)
                    numbers = [v for v in (reminder.due, reminder.remaining) if v is not None]
                    if (not isinstance(reminder.id, str) or not reminder.id
                            or not isinstance(reminder.label, str) or not 1 <= len(reminder.label) <= 32
                            or reminder.kind not in {'timer', 'reminder'}
                            or (reminder.due is None) == (reminder.remaining is None)
                            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in numbers)):
                        raise ValueError('Invalid reminder entry')
                    self.items.append(reminder)
                if len({item.id for item in self.items}) != len(self.items):
                    raise ValueError('Duplicate reminder IDs')
            except (ValueError, TypeError, KeyError) as exc:
                # Preserve the original file; never overwrite corrupt saved tasks.
                raise RuntimeError('提醒文件无法读取，请检查 runtime/reminders.json；原文件已保留。') from exc

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'items': [asdict(i) for i in self.items]},
                                        ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def apply(self, command):
        before = copy.deepcopy(self.items)
        retries = self.retry_after.copy()
        try:
            return self._apply(command)
        except OSError:
            self.items = before
            self.retry_after = retries
            raise

    def _apply(self, command):
        if command.operation == 'start':
            seconds = duration(command.argument)
            label = command.label.strip()
            if not label or len(label) > 32 or not all(c.isprintable() for c in label):
                raise ValueError('提醒内容请控制在一到三十二个字。')
            kind = 'timer' if label in {'计时', '番茄钟'} else 'reminder'
            if len(self.items) >= 10:
                raise ValueError('最多保存十项计时和提醒，请先取消一些。')
            if kind == 'timer' and any(i.kind == 'timer' for i in self.items):
                raise ValueError('已经有一个计时，请先取消计时或取消番茄钟。')
            item = Reminder(secrets.token_hex(4), label, kind, self.clock() + seconds)
            self.items.append(item)
            self.save()
            text = f'{duration_words(seconds)}计时已开始。' if kind == 'timer' else f'将在{duration_words(seconds)}后提醒你{label}。'
            if label == '番茄钟':
                text = '二十五分钟番茄钟已开始，到点提醒你休息。'
            return UtilityResult(text, f'{label[:8]} {duration_words(seconds)}')
        if command.operation == 'list':
            items = self.items
            if command.label == '计时':
                items = [i for i in items if i.kind == 'timer']
            elif command.label:
                items = [i for i in items if i.label == command.label]
            if not items:
                return UtilityResult('目前没有计时或提醒。', '没有计时或提醒')
            sentences = []
            for item in items[:4]:
                remaining = item.remaining if item.due is None else max(0, item.due - self.clock())
                phase = '已暂停' if item.due is None else ('已到期，等待播报' if remaining == 0 else '还剩')
                sentences.append(f'{item.label}{phase}' + (duration_words(remaining) if remaining else ''))
            if len(items) > 4:
                sentences.append(f'共有{len(items)}项')
            caption = sentences[0] if len(items) == 1 else f'共有{len(items)}项提醒'
            return UtilityResult('；'.join(sentences) + '。', caption)
        if command.label == '*':
            selected = self.items[:]
        elif command.label == '提醒':
            selected = [i for i in self.items if i.kind == 'reminder']
        elif command.label == '计时':
            selected = [i for i in self.items if i.kind == 'timer']
        else:
            selected = [i for i in self.items if i.label == command.label]
        if not selected:
            return UtilityResult('没有找到对应的计时或提醒。', '没有对应提醒')
        if len(selected) > 1 and command.label != '*':
            raise ValueError('有多个提醒符合，请用提醒内容指定，或说取消全部提醒。')
        now = self.clock()
        changed = 0
        for item in selected:
            if command.operation == 'pause' and item.due is not None:
                item.remaining = max(0, item.due - now)
                item.due = None
                changed += 1
            elif command.operation == 'resume' and item.due is None:
                item.due = now + item.remaining
                item.remaining = None
                changed += 1
            elif command.operation == 'cancel':
                self.items.remove(item)
                self.retry_after.pop(item.id, None)
                changed += 1
        self.save()
        verb = {'pause': '暂停', 'resume': '继续', 'cancel': '取消'}[command.operation]
        return UtilityResult(f'已{verb}{changed}项计时或提醒。' if changed else '该提醒已经处于这个状态。', f'已{verb}' if changed else '状态未改变')

    def next_due(self):
        now = self.clock()
        due = [i for i in self.items if i.due is not None and i.due <= now and self.retry_after.get(i.id, 0) <= now]
        return min(due, key=lambda item: item.due) if due else None

    def delivered(self, item):
        before = self.items
        retries = self.retry_after.copy()
        self.items = [i for i in self.items if i.id != item.id]
        self.retry_after.pop(item.id, None)
        try:
            self.save()
        except OSError:
            self.items = before
            self.retry_after = retries
            raise

    def defer(self, item):
        # Local notification retries are bounded and never invoke the LLM.
        self.retry_after[item.id] = self.clock() + 60

    def announcement(self, item):
        if item.label == '番茄钟':
            text = '番茄钟时间到了，休息一下吧。'
        elif item.kind == 'timer':
            text = '计时时间到了。'
        else:
            text = '提醒时间到了，该' + item.label + '啦。'
        return UtilityResult(text, '到点提醒 ' + item.label[:8])
