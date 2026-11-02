"""Local filming observations; never sends commands to the board."""
import json
import os
from pathlib import Path
import time

EVENTS = Path(__file__).resolve().parent / 'runtime/demo-events.jsonl'


def emit_event(owner, event_type, **details):
    if not getattr(owner, 'events_enabled', False):
        return
    event = {'time': time.time(), 'pid': os.getpid(), 'event': event_type,
             'turn_id': getattr(owner, 'active_turn_id', None), **details}
    try:
        EVENTS.parent.mkdir(exist_ok=True)
        with EVENTS.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    except OSError:
        pass  # Filming observations must not interrupt speech or device control.
