"""Explicit local voice commands and in-memory conversation profiles."""
import re
from dataclasses import dataclass

PROFILES = {
    'companion': ('陪伴聊天', '温和、好奇、自然地陪用户聊天，通常不超过60个汉字。', 256),
    'english': ('英语陪练', '帮助用户练习日常英语。用户用英语时，先用简短自然英语接话；'
                '有明显表达错误时温和给出一句更自然的说法，必要时用中文简短解释。'
                '用户用中文求助时用中文说明并给英文例句。一次只问一个问题，适合口头练习。', 320),
    'story': ('故事模式', '讲有情节、适合口头收听的故事。每次讲约120至200个汉字的一小段，'
              '不要一次讲完整部长篇。用户说继续讲时接着当前故事，不重复开头。'
              '用户指定主题时按主题讲，未指定时选择温暖、有趣的故事。', 512),
}

BASE_PROMPT = ('你是用户的桌面电子宠物小聆，一只圆润、好奇、温和的小机器人，'
               '通过Mini3和用户电脑交流。不使用Markdown，不刻意卖萌。'
               '设备控制只能由程序执行；没有工具执行结果时，不声称已控制设备、'
               '联网搜索、设置闹钟或完成现实操作。你没有长期记忆。')


@dataclass(frozen=True)
class LocalCommand:
    kind: str
    target: str
    operation: str = ''
    value: int = 0


def number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip('零一二三四五六七八九', range(10)))
    if text == '两':
        return 2
    if text == '一百':
        return 100
    if text in digits:
        return digits[text]
    if re.fullmatch('[一二三四五六七八九]?十[一二三四五六七八九]?', text):
        tens, units = text.split('十')
        return digits.get(tens, 1) * 10 + digits.get(units, 0)
    raise ValueError('Unsupported percentage')


def parse_command(text):
    # Only complete, direct requests qualify. Discussion, negation and quoted
    # commands remain ordinary dialogue; arbitrary words never enter the shell.
    if any(c in text for c in '\"“”‘’「」'):
        return None
    clean = re.sub(r'[\s，。！？、,.!?]', '', text).lower()
    clean = re.sub(r'^(?:小聆|小林)[啊呀]?', '', clean)
    clean = re.sub(r'^(?:请你|请|帮我|给我)', '', clean)
    clean = re.sub(r'(?:谢谢|好吗|吧)$', '', clean)
    modes = {'陪伴聊天': 'companion', '聊天模式': 'companion', '陪伴模式': 'companion',
             '英语陪练': 'english', '英语模式': 'english', '英语陪练模式': 'english',
             '故事模式': 'story'}
    match = re.fullmatch(r'(?:切换到|切换成|进入|开启|回到|切换)(.+)', clean)
    if match and match[1] in modes:
        return LocalCommand('profile', modes[match[1]])
    moods = {'开心': 'happy', '高兴': 'happy', '难过': 'sad', '伤心': 'sad',
             '生气': 'angry', '正常': 'neutral', '普通': 'neutral'}
    match = re.fullmatch(r'(?:换成|变成|切换到|切换成|换个|做个)?(开心|高兴|难过|伤心|生气|正常|普通)(?:的)?表情', clean)
    if not match:
        match = re.fullmatch(r'(开心|高兴|难过|伤心|生气)(?:一点)', clean)
    if match:
        return LocalCommand('mood', moods[match[1]])
    if clean in {'恢复表情', '恢复正常表情'}:
        return LocalCommand('mood', 'neutral')
    short = {'小声一点': ('volume', -10), '小声点': ('volume', -10),
             '大声一点': ('volume', 10), '大声点': ('volume', 10),
             '屏幕暗一点': ('brightness', -10), '屏幕暗点': ('brightness', -10),
             '屏幕亮一点': ('brightness', 10), '屏幕亮点': ('brightness', 10)}
    if clean in short:
        target, delta = short[clean]
        return LocalCommand('setting', target, 'adjust', delta)
    labels = r'(屏幕亮度|亮度|音量|声量|声音)'
    match = re.fullmatch(r'(调高|调大|调亮|调低|调小|调暗|降低|提高)' + labels + r'(?:一点|点)?', clean)
    if match:
        target = 'brightness' if '亮度' in match[2] else 'volume'
        delta = -10 if match[1] in {'调低', '调小', '调暗', '降低'} else 10
        return LocalCommand('setting', target, 'adjust', delta)
    match = re.fullmatch(labels + r'(?:调|调节|调整)?(?:高|大|亮|低|小|暗)(?:一点|点)', clean)
    if not match:
        match = re.fullmatch(r'(?:把)?' + labels + r'(?:调高|调大|调亮|调低|调小|调暗|降低|提高)(?:一点|点)?', clean)
    if match:
        target = 'brightness' if '亮度' in match[1] else 'volume'
        delta = -10 if any(word in clean for word in ('低', '小', '暗', '降低')) else 10
        return LocalCommand('setting', target, 'adjust', delta)
    match = re.fullmatch(r'(?:把)?' + labels + r'(?:调到|调成|设置为|设为|调整到|调节到)(?:百分之)?([0-9零一二两三四五六七八九十百]+)(?:%|％)?', clean)
    if match:
        target = 'brightness' if '亮度' in match[1] else 'volume'
        try:
            value = number(match[2])
        except ValueError:
            return LocalCommand('invalid', target)
        # Keep screen readable and spoken confirmations audible.
        if not 10 <= value <= 100:
            return LocalCommand('invalid', target)
        return LocalCommand('setting', target, 'set', value)
    return None
