"""Complete utility requests and bounded local arithmetic; no model calls."""
import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import operator
import re

BEIJING = timezone(timedelta(hours=8))
NUM = r'[0-9零〇一二两三四五六七八九十百千万点.]+'


@dataclass(frozen=True)
class UtilityCommand:
    kind: str
    operation: str
    argument: str = ''
    label: str = ''


@dataclass(frozen=True)
class UtilityResult:
    text: str
    caption: str = ''


def numeric(text):
    if text.startswith('负'):
        return -numeric(text[1:])
    if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text):
        value = float(text)
    elif '点' in text:
        whole, fraction = text.split('点', 1)
        digits = dict(zip('零〇一二两三四五六七八九', '001223456789'))
        if not fraction or any(c not in digits for c in fraction):
            raise ValueError('无法解析小数，请说零点五这样的数值。')
        value = numeric(whole) + float('0.' + ''.join(digits[c] for c in fraction))
    else:
        digits = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
                  '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
        units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
        if not text or any(c not in digits and c not in units for c in text):
            raise ValueError('无法解析数值。')
        if all(c in digits for c in text):
            value = int(''.join(str(digits[c]) for c in text))
        else:
            total = section = digit = 0
            last_unit = 10000
            for c in text:
                if c in digits:
                    digit = digits[c]
                elif c == '万':
                    total += (section + digit) * 10000
                    section = digit = 0
                    last_unit = 10000
                else:
                    unit = units[c]
                    if unit >= last_unit:
                        raise ValueError('数值格式不清楚。')
                    section += (digit or 1) * unit
                    digit = 0
                    last_unit = unit
            value = total + section + digit
    if not math.isfinite(value) or abs(value) > 1e12:
        raise ValueError('数值过大。')
    return value


def duration(text):
    # Also accept "一个半小时" without sending an unsupported request to a model.
    text = re.sub(r'(' + NUM + r')个半(?=小时)', lambda m: str(numeric(m[1]) + 0.5), text)
    factors = {'天': 86400, '小时': 3600, '分钟': 60, '分': 60, '秒钟': 1, '秒': 1}
    pieces = list(re.finditer(r'(' + NUM + r'|半)(?:个)?(天|小时|分钟|分|秒钟|秒)', text))
    cursor = 0
    seconds = 0
    seen = set()
    for part in pieces:
        if part.start() != cursor or factors[part[2]] in seen:
            raise ValueError('时间格式不清楚，可以说十分钟或一小时三十分钟。')
        factor = factors[part[2]]
        seen.add(factor)
        seconds += (0.5 if part[1] == '半' else numeric(part[1])) * factor
        cursor = part.end()
    if pieces and text[cursor:] == '半':
        seconds += factors[pieces[-1][2]] / 2
        cursor += 1
    if not pieces or cursor != len(text) or seconds != int(seconds) or not 1 <= seconds <= 7 * 86400:
        raise ValueError('请设置一秒到七天之间的时间，例如计时十分钟。')
    return int(seconds)


def duration_words(seconds):
    seconds = max(0, math.ceil(seconds))
    parts = []
    for factor, name in ((86400, '天'), (3600, '小时'), (60, '分钟'), (1, '秒')):
        count, seconds = divmod(seconds, factor)
        if count:
            parts.append(f'{count}{name}')
    return ''.join(parts) or '零秒'


def parse_utility(text):
    if any(c in text for c in '\"“”‘’「」'):
        return None
    clean = re.sub(r'\s+', '', text).strip('，。！？,.!?')
    clean = re.sub(r'^(?:小聆|小林)[啊呀]?[，,]?', '', clean)
    clean = re.sub(r'^(?:请你|请|帮我|给我)', '', clean)
    clean = re.sub(r'(?:谢谢|好吗|吧)[。！？!?]*$', '', clean)
    if re.search(r'(?:是什么意思|怎么实现|如何实现|怎么写|如果|假如)', clean):
        return None
    if clean in {'现在几点', '现在几点了', '几点了', '报时', '现在的时间', '当前时间', '告诉我现在几点'}:
        return UtilityCommand('tool', 'time')
    if clean in {'今天几号', '今天是几号', '今天几月几号', '今天的日期', '今天星期几', '今天是星期几'}:
        return UtilityCommand('tool', 'date')
    if clean in {'开始一个番茄钟', '开始番茄钟', '开启番茄钟', '开始番茄计时'}:
        return UtilityCommand('reminder', 'start', '25分钟', '番茄钟')
    match = re.fullmatch(r'(?:开始)?(?:计时|倒计时)(.+)', clean)
    if match and not match[1].startswith(('还剩', '剩余')):
        return UtilityCommand('reminder', 'start', match[1], '计时')
    match = re.fullmatch(r'([0-9零〇一二两三四五六七八九十百千万点.半个天小时分秒钟]+)(?:后|以后)提醒我(.+)', clean)
    if match:
        return UtilityCommand('reminder', 'start', match[1], match[2])
    if clean in {'查看计时', '计时还剩多久', '倒计时还剩多久', '计时剩余时间'}:
        return UtilityCommand('reminder', 'list', label='计时')
    if clean == '番茄钟还剩多久':
        return UtilityCommand('reminder', 'list', label='番茄钟')
    if clean in {'查看提醒', '查看所有提醒', '有哪些提醒', '查看剩余时间'}:
        return UtilityCommand('reminder', 'list')
    match = re.fullmatch(r'(暂停|继续|恢复|取消|停止)(全部提醒|所有提醒|提醒|计时|倒计时|番茄钟|.+提醒)', clean)
    if match:
        op = {'暂停': 'pause', '继续': 'resume', '恢复': 'resume', '取消': 'cancel', '停止': 'cancel'}[match[1]]
        target = match[2]
        if target in {'全部提醒', '所有提醒'}:
            target = '*'
        elif target == '倒计时':
            target = '计时'
        elif target.endswith('提醒') and target != '提醒':
            target = target[:-2]
        return UtilityCommand('reminder', op, label=target)
    match = re.fullmatch(r'(?:把)?(?:天气城市|默认天气城市)(?:设为|设置为|设成|改为|设置成)([\u4e00-\u9fffA-Za-z·]{2,20})', clean)
    if match:
        return UtilityCommand('weather', 'city', match[1])
    if clean in {'天气城市', '查看天气城市', '默认天气城市是哪里'}:
        return UtilityCommand('weather', 'location')
    # City and day must precede a complete supported weather question.
    match = re.fullmatch(r'([\u4e00-\u9fffA-Za-z·]{2,20}?)?(今天|明天|后天|现在)?(?:的)?'
                         r'(天气(?:怎么样|如何)?|会下雨吗|下雨吗|多少度|气温多少|温度多少)', clean)
    if match:
        city, day, topic = match.groups()
        # Greedy optional city can swallow a day prefix; repair only exact days.
        for name in ('今天', '明天', '后天', '现在'):
            if city and city.endswith(name) and day is None:
                city, day = city[:-len(name)], name
        offset = {'明天': '1', '后天': '2'}.get(day, '0')
        return UtilityCommand('weather', offset, city or '', 'rain' if '雨' in topic else 'summary')
    match = re.fullmatch(r'(?:计算一下|算一下|计算|算算)(.+)', clean)
    if match:
        argument = re.sub(r'(?:等于多少|等于几|是多少)$', '', match[1])
        return UtilityCommand('tool', 'calculate', argument)
    match = re.fullmatch(r'(?:回答)?(.+?)(?:等于多少|等于几|是多少)', clean)
    if match and re.fullmatch(r'[0-9零〇一二两三四五六七八九十百千万点负加减乘除以去上.+\-*/×÷()（）]+', match[1]):
        return UtilityCommand('tool', 'calculate', match[1])
    units = '|'.join(sorted(UNIT_FACTORS, key=len, reverse=True))
    match = re.fullmatch(r'([-负]?' + NUM + r')(' + units + r')(?:等于多少|是多少|换算成|转换成|换成|转成|等于几)(' + units + r')', clean)
    if match:
        return UtilityCommand('tool', 'convert', '|'.join(match.groups()))
    return None


def arithmetic(text):
    if len(text) > 120:
        raise ValueError('算式过长。')
    expression = text
    for source, dest in [('加上', '+'), ('减去', '-'), ('乘以', '*'), ('除以', '/'),
                         ('加', '+'), ('减', '-'), ('乘', '*'), ('除', '/'),
                         ('×', '*'), ('÷', '/'), ('（', '('), ('）', ')'), ('负', '-')]:
        expression = expression.replace(source, dest)
    expression = re.sub(NUM, lambda m: str(numeric(m[0])) if any(c in m[0] for c in '零〇一二两三四五六七八九十百千万点') else m[0], expression)
    if not re.fullmatch(r'[0-9.()+\-*/]+', expression) or '**' in expression or '//' in expression:
        raise ValueError('目前支持数字、括号和加减乘除。')
    try:
        tree = ast.parse(expression, mode='eval')
    except (SyntaxError, RecursionError):
        raise ValueError('算式格式不清楚。') from None
    if len(list(ast.walk(tree))) > 50:
        raise ValueError('算式过长。')
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}

    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            try:
                value = operations[type(node.op)](evaluate(node.left), evaluate(node.right))
            except ZeroDivisionError:
                raise ValueError('不能除以零。') from None
        else:
            raise ValueError('目前支持数字、括号和加减乘除。')
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError('数值过大。')
        return value
    return evaluate(tree.body)


# category, multiplier to category base unit. Temperature uses affine conversion.
UNIT_FACTORS = {
    '毫米': ('length', .001), '厘米': ('length', .01), '米': ('length', 1),
    '公里': ('length', 1000), '千米': ('length', 1000),
    '克': ('mass', .001), '千克': ('mass', 1), '公斤': ('mass', 1), '斤': ('mass', .5),
    '毫升': ('volume', .001), '升': ('volume', 1),
    '秒': ('time', 1), '分钟': ('time', 60), '小时': ('time', 3600),
    '摄氏度': ('temperature', 1), '华氏度': ('temperature', 1),
}


def tool_answer(command, now=None):
    now = now or datetime.now(BEIJING)
    if command.operation == 'time':
        return UtilityResult(f'现在是北京时间{now.hour}点{now.minute}分。', now.strftime('%H:%M 北京时间'))
    if command.operation == 'date':
        weekday = '一二三四五六日'[now.weekday()]
        return UtilityResult(f'今天是{now.year}年{now.month}月{now.day}日，星期{weekday}。',
                             f'{now.month}月{now.day}日 周{weekday}')
    if command.operation == 'calculate':
        answer = format(arithmetic(command.argument), '.10g')
        return UtilityResult(f'结果是{answer}。', f'结果 {answer}')
    quantity, source, target = command.argument.split('|')
    value = numeric(quantity)
    if UNIT_FACTORS[source][0] != UNIT_FACTORS[target][0]:
        raise ValueError('这两个单位不能直接换算。')
    if source == '摄氏度' and target == '华氏度':
        result = value * 9 / 5 + 32
    elif source == '华氏度' and target == '摄氏度':
        result = (value - 32) * 5 / 9
    else:
        result = value * UNIT_FACTORS[source][1] / UNIT_FACTORS[target][1]
    answer = format(result, '.10g')
    return UtilityResult(f'{quantity}{source}等于{answer}{target}。', f'{answer}{target}')
