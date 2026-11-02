"""Small, independent HUD updates; never change conversation/recording phase."""
from datetime import datetime
import math
from local_tools import BEIJING


def hud_command(reminders, now=None):
    now = reminders.clock() if now is None else now
    clock = datetime.fromtimestamp(now, BEIJING)
    seconds_today = clock.hour * 3600 + clock.minute * 60 + clock.second
    timers = [item for item in reminders.items if item.kind == 'timer']
    running = sorted((item for item in reminders.items if item.due is not None), key=lambda item: item.due)
    paused = [item for item in reminders.items if item.due is None]
    selected = (timers or running or paused)
    if not selected:
        return f'local_agent hud {seconds_today} -1 0'
    item = selected[0]
    remaining = item.remaining if item.due is None else item.due - now
    remaining = min(604800, max(0, math.ceil(remaining)))
    label = ''.join(c if c.isprintable() else ' ' for c in item.label)[:5]
    encoded = label.encode('utf-8')[:18].decode('utf-8', errors='ignore').encode('utf-8').hex()
    return f'local_agent hud {seconds_today} {remaining} {int(item.due is None)}' + (' ' + encoded if encoded else '')
