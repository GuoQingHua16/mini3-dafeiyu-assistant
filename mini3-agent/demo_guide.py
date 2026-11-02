"""Interactive filming prompts; observes local files, never opens an ADB shell."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from adb_link import Lease, ADBInUse
from demo_plan import CHAPTERS, markdown

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
BEIJING = timezone(timedelta(hours=8))
PHASES = {0: '等待连接', 1: '待机', 2: '我在听', 3: '思考', 4: '准备说话',
          5: '播放中', 6: '本轮未成功', 7: '休息', 8: '持续聆听'}


def normalize(text):
    return re.sub(r'[\s，。！？、,.!?]', '', text).lower()


def usb_status():
    try:
        status = json.loads((RUNTIME / 'usb-link-status.json').read_text(encoding='utf-8'))
        if not 0 <= time.time() - status.get('updated', 0) < 7:
            status['connected'] = False
        return status
    except (OSError, ValueError):
        return {'connected': False}


def set_mode(mode):
    if mode not in {'button', 'continuous'}: raise ValueError('Invalid mode')
    path = RUNTIME / 'mode.txt'
    temporary = path.with_suffix('.guide.tmp')
    temporary.write_text(mode, encoding='utf-8')
    temporary.replace(path)


class EventTail:
    def __init__(self, path, at_end=True):
        self.path = Path(path)
        self.offset = self.path.stat().st_size if at_end and self.path.exists() else 0
        self.pending = b''

    def read(self):
        try:
            if self.path.stat().st_size < self.offset:
                self.offset = 0; self.pending = b''
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                chunk = stream.read(65536)
                self.offset = stream.tell()
        except OSError:
            return []
        lines = (self.pending + chunk).split(b'\n')
        self.pending = lines.pop()
        result = []
        for line in lines:
            try: result.append(json.loads(line.decode('utf-8')))
            except (ValueError, UnicodeError): pass
        return result


class Tracker:
    def __init__(self, action, context):
        self.action, self.context = action, context
        self.turn = None
        self.recognized = ''
        self.verified = action.get('expected') is None
        self.done = self.playing = self.finished_rest = False
        self.failure = ''

    def feed(self, event):
        kind = self.action['kind']
        if kind == 'wait_reminder':
            target = self.context.get('reminder_' + self.action['reminder_kind'])
            if event.get('event') == 'reminder_delivered' and event.get('reminder_id') == target and target:
                if event.get('interrupted'):
                    self.failure = '提醒被按键停止，未完整播完。'
                else: self.done = True
            return
        if event.get('event') == 'turn_started':
            if re.search(self.action['pattern'], normalize(event.get('text', '')), re.I):
                self.turn = event.get('turn_id'); self.recognized = event.get('text', '')
                self.verified = self.action.get('expected') is None
                self.failure = ''; self.finished_rest = False
            return
        if not self.turn or event.get('turn_id') != self.turn:
            return
        expected = self.action.get('expected')
        if expected and all(str(event.get(k)) == str(v) for k, v in expected.items() if k != 'text_pattern'):
            if not expected.get('text_pattern') or re.search(expected['text_pattern'], event.get('text', '')):
                self.verified = True
                for item in event.get('created', []):
                    self.context['reminder_' + item['kind']] = item['id']
        if event.get('event') == 'turn_failed':
            self.failure = event.get('reason', '本轮没有完成')
        elif event.get('event') == 'playback_started':
            self.playing = True
        elif event.get('event') == 'playback_stopped' and kind == 'interrupt':
            self.done = True; self.context['after_stop'] = True
        elif event.get('event') == 'turn_finished':
            if kind == 'interrupt':
                self.failure = '故事已经播完，尚未展示按键停止；可重拍或跳过。'
            elif kind == 'rest':
                self.finished_rest = event.get('outcome') == 'rest'
            elif event.get('outcome') == 'replied' and self.verified:
                self.done = True
            elif event.get('outcome') == 'replied':
                self.failure = '已收到回答，但没有观察到本步预期的执行确认；请检查识别结果。'

    def observe(self, status):
        if self.finished_rest and status.get('connected') and status.get('phase') == 7:
            self.done = True


def key():
    if os.name == 'nt':
        import msvcrt
        if not msvcrt.kbhit(): return None
        char = msvcrt.getwch()
        if char in ('\x00', '\xe0'):
            if msvcrt.kbhit(): msvcrt.getwch()
            return None
    else:
        import select
        if not select.select([sys.stdin], [], [], 0)[0]: return None
        char = sys.stdin.read(1)
    if char == '\x03': raise KeyboardInterrupt
    return char.lower()


def clear():
    os.system('cls' if os.name == 'nt' else 'clear')


def wait_enter(message):
    print(message, flush=True)
    while True:
        choice = key()
        if choice in ('\r', '\n', ' '): return 'next'
        if choice in ('q', 'f', 's', 'r'): return choice
        time.sleep(.1)


def preflight():
    print('正在等候后台与 Mini3 建立连接……', flush=True)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = usb_status()
        if state.get('connected') and state.get('mode') == 'button' and state.get('phase') in (1, 7):
            print('设备在线，按键模式就绪。', flush=True)
            return
        if key() == 'q': raise KeyboardInterrupt
        time.sleep(.25)
    raise RuntimeError('设备未进入可拍摄状态。请检查USB连接和后台日志，暂不开始录像。')


def main(session_dir):
    if not sys.stdin.isatty():
        raise RuntimeError('请从“开始拍摄.cmd”打开交互提示窗口。预览可使用 --preview。')
    lease = Lease(RUNTIME / 'demo-guide.lock')
    lease.acquire()
    session_dir.mkdir(parents=True, exist_ok=True)
    report = {'guide_started_at': datetime.now(BEIJING).isoformat(), 'target_seconds': 475,
              'records_video': False, 'finished': False, 'actions': [], 'chapters': []}
    def save():
        (session_dir / '拍摄记录.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        set_mode('button')
        preflight()
        clear()
        print('Mini3 拍摄助手｜约8～9分钟\n')
        print('电脑回车：推进提词。开发板主键短按：录音或停止播放。演示不使用RST。')
        print('旁白时保持按键模式；连续对话仅展示一轮。你本人说话、操作开发板，程序只观察反馈。')
        print('请先固定镜头并开始你自己的录像。程序不会替你录制视频。\n')
        if wait_enter('准备好后按回车开始；Q退出。') == 'q': return
        began = time.monotonic(); context = {}; chapter_index = 0
        while chapter_index < len(CHAPTERS):
            chapter = CHAPTERS[chapter_index]
            set_mode('button')
            clear()
            elapsed = int(time.monotonic() - began)
            print(f'第{chapter_index+1}/{len(CHAPTERS)}章：{chapter["title"]}｜建议{chapter["seconds"]}秒｜已用{elapsed//60}:{elapsed%60:02d}\n')
            print('【可照读的讲解词】\n' + chapter['narration'] + '\n')
            print('讲解时不要按开发板主键，避免把旁白当作提问。')
            choice = wait_enter('讲解完按回车开始操作；S跳过本章，F直接收尾，Q结束。')
            if choice == 'q': break
            if choice == 'f' and chapter['id'] != 'outro': chapter_index = len(CHAPTERS)-1; continue
            if choice == 's':
                report['chapters'].append({'id': chapter['id'], 'skipped': True}); save(); chapter_index += 1; continue
            tail = EventTail(RUNTIME / 'demo-events.jsonl')
            if chapter.get('mode') == 'continuous': set_mode('continuous')
            end_chapter = False
            for index, action in enumerate(chapter['actions']):
                tracker = Tracker(action, context)
                started = time.time(); last_phase = -999; last_failure = ''; stop_prompted = False
                continuous_deadline = time.monotonic() + 45
                continuous_returned = False
                clear()
                print(f'【{chapter["title"]}】操作 {index+1}/{len(chapter["actions"])}\n')
                if action['kind'] == 'wait_reminder':
                    print(action['say'])
                    if not context.get('reminder_' + action['reminder_kind']):
                        print('前一步未确认创建对应任务，本项标记为跳过。')
                        report['actions'].append({'chapter': chapter['id'], 'say': action['say'], 'status': 'skipped_no_task'})
                        save(); continue
                else:
                    if action['press'] == 'after_stop':
                        print('刚停播后，等“我在听”亮起直接说；如果已回到待机，再短按一次主键。')
                    elif action['press'] is False:
                        print('不用按开发板按键，等“我在听”后直接说。')
                    else:
                        print('短按一次开发板主功能键，等“我在听”后说：')
                    print('\n  ' + action['say'] + '\n')
                print('R重新提示本步（由你重说），S跳过，F收尾，Q结束。')
                skipped = False
                while not tracker.done:
                    state = usb_status()
                    for event in tail.read():
                        if event.get('pid') != state.get('pid'): continue
                        if event.get('event') == 'turn_started': print('识别到：' + event.get('text', ''), flush=True)
                        tracker.feed(event)
                        if event.get('event') == 'operation_failed' and not tracker.done:
                            tracker.failure = event.get('reason', '本轮未成功')
                        # One turn only: close continuous capture even if ASR
                        # heard an unexpected sentence. Never auto-repeat it.
                        if chapter.get('mode') == 'continuous' and not continuous_returned and event.get('event') in ('turn_finished', 'turn_failed', 'turn_interrupted'):
                            set_mode('button')
                            continuous_returned = True
                    if chapter.get('mode') == 'continuous' and not continuous_returned and time.monotonic() > continuous_deadline:
                        set_mode('button'); continuous_returned = True
                        tracker.failure = '本轮等待较久，已切回按键模式；可以按R再试一次或S跳过。'
                    tracker.observe(state)
                    phase = state.get('phase') if state.get('connected') else None
                    if phase != last_phase:
                        print('设备：' + PHASES.get(phase, '连接暂不可用，先不要重复说指令'), flush=True)
                        last_phase = phase
                    if tracker.playing and action['kind'] == 'interrupt' and not stop_prompted:
                        print('\n>>> 故事已开始：现在短按一次主功能键停止。随后直接说“切换到陪伴聊天”。\n', flush=True)
                        stop_prompted = True
                    if tracker.failure and tracker.failure != last_failure:
                        print('本步未确认完成：' + tracker.failure + '\n请按R重拍，或S跳过。', flush=True)
                        last_failure = tracker.failure
                        set_mode('button')
                    choice = key()
                    if choice == 'r':
                        tracker = Tracker(action, context); last_failure = ''; stop_prompted = False
                        print('请重新操作并说：' + action['say'], flush=True)
                        if chapter.get('mode') == 'continuous':
                            set_mode('continuous'); continuous_returned = False
                            continuous_deadline = time.monotonic() + 45
                    elif choice in ('s', 'f', 'q'):
                        skipped = True; end_chapter = choice in ('f', 'q'); break
                    if time.monotonic() - began > 560 and not context.get('time_notice'):
                        print('已接近十分钟，可以按F进入收尾。', flush=True); context['time_notice'] = True
                    time.sleep(.1)
                report['actions'].append({'chapter': chapter['id'], 'say': action['say'],
                    'status': 'skipped' if skipped else 'observed', 'recognized': tracker.recognized,
                    'failure': tracker.failure, 'seconds': round(time.time()-started, 1)})
                save()
                if end_chapter: break
                print('已观察到完成。', flush=True)
                if action['kind'] != 'interrupt': time.sleep(.5)
            set_mode('button')
            if end_chapter:
                if choice == 'q': break
                chapter_index = len(CHAPTERS)-1; continue
            confirmation = wait_enter('\n确认本章画面和声音后按回车；S表示未确认，F直接收尾，Q结束。')
            report['chapters'].append({'id': chapter['id'], 'human_confirmed': confirmation == 'next'})
            save()
            if confirmation == 'q': break
            if confirmation == 'f' and chapter['id'] != 'outro': chapter_index = len(CHAPTERS)-1
            else: chapter_index += 1
        report['finished'] = chapter_index >= len(CHAPTERS)
        report['elapsed_seconds'] = round(time.monotonic()-began, 1)
    finally:
        set_mode('button')
        save(); lease.close()
        print('\n提词结束，后台保留按键模式。拍摄记录：' + str(session_dir / '拍摄记录.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--export', type=Path)
    parser.add_argument('--session-dir', type=Path)
    args = parser.parse_args()
    if args.export:
        args.export.write_text(markdown(), encoding='utf-8')
    elif args.preview:
        print(markdown())
    else:
        folder = args.session_dir or RUNTIME / 'filming' / datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S')
        try: main(folder)
        except KeyboardInterrupt: print('\n已退出提词。')
        except (RuntimeError, OSError, ADBInUse) as exc:
            raise SystemExit('拍摄助手未启动或已停止：' + str(exc)) from None
