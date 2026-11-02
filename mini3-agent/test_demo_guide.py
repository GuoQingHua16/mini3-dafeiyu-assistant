import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import demo_events
from demo_guide import EventTail, Tracker
from demo_plan import CHAPTERS, voice, control
from commands import parse_command
from local_tools import parse_utility


class GuideTests(unittest.TestCase):
    def begin(self, tracker, text, turn='demo:1'):
        tracker.feed({'event': 'turn_started', 'turn_id': turn, 'text': text})

    def test_device_confirmation_and_completion_are_both_required(self):
        tracker = Tracker(voice('换成开心表情', '开心', control('mood', 'happy')), {})
        self.begin(tracker, '换成开心表情')
        tracker.feed({'event': 'turn_finished', 'turn_id': 'demo:1', 'outcome': 'replied'})
        self.assertFalse(tracker.done)
        self.begin(tracker, '换成开心表情', 'demo:2')
        tracker.feed({'event': 'control_verified', 'turn_id': 'demo:1', 'kind': 'mood', 'target': 'happy'})
        tracker.feed({'event': 'turn_finished', 'turn_id': 'demo:2', 'outcome': 'replied'})
        self.assertFalse(tracker.done)
        tracker.feed({'event': 'control_verified', 'turn_id': 'demo:2', 'kind': 'mood', 'target': 'happy'})
        tracker.feed({'event': 'turn_finished', 'turn_id': 'demo:2', 'outcome': 'replied'})
        self.assertTrue(tracker.done)

    def test_unrelated_speech_cannot_finish_a_card(self):
        tracker = Tracker(voice('现在几点', '现在几点'), {})
        self.begin(tracker, '今天天气怎么样')
        tracker.feed({'event': 'turn_finished', 'turn_id': 'demo:1', 'outcome': 'replied'})
        self.assertFalse(tracker.done)

    def test_story_requires_an_observed_stop(self):
        context = {}; tracker = Tracker(voice('讲故事', '故事', kind='interrupt'), context)
        self.begin(tracker, '讲一个故事')
        tracker.feed({'event': 'playback_started', 'turn_id': 'demo:1'})
        self.assertTrue(tracker.playing); self.assertFalse(tracker.done)
        tracker.feed({'event': 'playback_stopped', 'turn_id': 'demo:1'})
        self.assertTrue(tracker.done); self.assertTrue(context['after_stop'])

    def test_reminder_wait_is_bound_to_the_created_task(self):
        tracker = Tracker({'kind': 'wait_reminder', 'reminder_kind': 'timer'}, {'reminder_timer': 'our-id'})
        tracker.feed({'event': 'reminder_delivered', 'reminder_id': 'other-id', 'interrupted': False})
        self.assertFalse(tracker.done)
        tracker.feed({'event': 'reminder_delivered', 'reminder_id': 'our-id', 'interrupted': False})
        self.assertTrue(tracker.done)

    def test_rest_waits_for_the_device_sleep_state(self):
        tracker = Tracker(voice('休息一下', '休息', kind='rest'), {})
        self.begin(tracker, '休息一下')
        tracker.feed({'event': 'turn_finished', 'turn_id': 'demo:1', 'outcome': 'rest'})
        tracker.observe({'connected': True, 'phase': 1}); self.assertFalse(tracker.done)
        tracker.observe({'connected': False, 'phase': 7}); self.assertFalse(tracker.done)
        tracker.observe({'connected': True, 'phase': 7}); self.assertTrue(tracker.done)

    def test_plan_fits_ten_minutes_and_uses_supported_commands(self):
        self.assertLess(sum(chapter['seconds'] for chapter in CHAPTERS), 600)
        for chapter in CHAPTERS:
            for action in chapter['actions']:
                expected = action.get('expected')
                if not expected: continue
                parsed = parse_command(action['say']) if expected['event'] == 'control_verified' else parse_utility(action['say'])
                self.assertIsNotNone(parsed, action['say'])
                self.assertEqual(parsed.kind, expected['kind'], action['say'])
                if 'target' in expected: self.assertEqual(parsed.target, expected['target'])
                if 'value' in expected: self.assertEqual(parsed.value, expected['value'])
                if 'operation' in expected: self.assertEqual(str(parsed.operation), str(expected['operation']))

    def test_event_tail_handles_split_utf8_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'events'
            data = (json.dumps({'text': '开心表情'}, ensure_ascii=False) + '\n').encode()
            path.write_bytes(data[:12]); tail = EventTail(path, at_end=False)
            self.assertEqual(tail.read(), [])
            with path.open('ab') as stream: stream.write(data[12:])
            self.assertEqual(tail.read(), [{'text': '开心表情'}])
            self.assertEqual(tail.read(), [])

    def test_event_sink_is_optional_and_does_not_break_device_work(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(demo_events, 'EVENTS', Path(folder) / 'events'):
            demo_events.emit_event(SimpleNamespace(), 'ignored')
            self.assertFalse(demo_events.EVENTS.exists())
            demo_events.emit_event(SimpleNamespace(events_enabled=True, active_turn_id='one'), 'turn_finished', outcome='replied')
            row = json.loads(demo_events.EVENTS.read_text(encoding='utf-8'))
            self.assertEqual(row['turn_id'], 'one')
            with patch.object(Path, 'open', side_effect=OSError('read only')):
                demo_events.emit_event(SimpleNamespace(events_enabled=True), 'ignored')


if __name__ == '__main__': unittest.main()
