"""Under-ten-minute filming cards; the user performs every board action."""


def voice(say, pattern, expected=None, press=True, kind='voice'):
    return {'say': say, 'pattern': pattern, 'expected': expected, 'press': press, 'kind': kind}


def control(kind, target, value=None):
    out = {'event': 'control_verified', 'kind': kind, 'target': target}
    if value is not None: out['value'] = value
    return out


def utility(kind, operation, text_pattern=None):
    out = {'event': 'utility_verified', 'kind': kind, 'operation': operation}
    if text_pattern: out['text_pattern'] = text_pattern
    return out


CHAPTERS = [
    {'id': 'intro', 'title': '开场与设备介绍', 'seconds': 35,
     'narration': '这是我的 Mini3 桌面语音伙伴。开发板负责采音、显示、播放和按键交互，电脑上的本地模型负责语音识别，DeepSeek 负责对话，edge-tts 负责合成声音。屏幕上的大肥鱼会随交互改变姿态，还能显示时钟、电量和 Wi-Fi 状态。',
     'shot': '镜头对准开发板，留出主功能键和屏幕；电脑提词窗口可以放在镜头外。', 'actions': []},
    {'id': 'chat', 'title': '按键对话与自动断句', 'seconds': 40,
     'narration': '先演示基本对话。短按一次主功能键，看到“我在听”再说话。说完停顿约一秒，它会自动结束采音，进入思考和回答状态。',
     'actions': [voice('我今天有点累，用一句话鼓励我吧。', '累|鼓励')]},
    {'id': 'continuous', 'title': '短暂展示连续对话', 'seconds': 30, 'mode': 'continuous',
     'narration': '也可以切换成连续对话，等待聆听时不用每次按键。为了方便我边讲解边拍摄，接下来只演示一轮，完成后提示助手会切回按键模式。',
     'actions': [voice('现在几点？', '现在几点|几点了', utility('tool', 'time'), press=False)]},
    {'id': 'controls', 'title': '表情、亮度和音量', 'seconds': 75,
     'narration': '明确的设备控制指令由电脑本地解析，确认设备执行后再播报，不需要让对话模型猜测是否完成。这里展示表情，以及亮度和音量调节。',
     'actions': [voice('换成开心表情。', '开心.*表情', control('mood', 'happy')),
                 voice('屏幕亮度调到百分之五十。', '亮度.*(?:五十|50)', control('setting', 'brightness', 50)),
                 voice('屏幕亮度调到百分之七十。', '亮度.*(?:七十|70)', control('setting', 'brightness', 70)),
                 voice('音量调到百分之五十。', '音量.*(?:五十|50)', control('setting', 'volume', 50)),
                 voice('音量调到百分之七十。', '音量.*(?:七十|70)', control('setting', 'volume', 70)),
                 voice('恢复正常表情。', '正常表情|恢复表情', control('mood', 'neutral'))]},
    {'id': 'english', 'title': '英语陪练', 'seconds': 55,
     'narration': '对话有陪伴、英语陪练和故事三种模式。切换模式会清空上一种模式的最近上下文；英语陪练支持用中英文交流。',
     'actions': [voice('切换到英语陪练。', '英语', control('profile', 'english')),
                 voice('Hello, I want to practise English.', 'hello|english|英语|practice|practise')]},
    {'id': 'story', 'title': '故事模式与按键停止', 'seconds': 60,
     'narration': '故事模式会给出更长的回答。播放过程中可以用主按键停止；停止后，它会自动接收下一句话。这里演示的是按键停止。',
     'actions': [voice('切换到故事模式。', '故事模式', control('profile', 'story')),
                 voice('讲一个小鲸鱼交朋友的故事。', '故事|鲸鱼', kind='interrupt'),
                 voice('切换到陪伴聊天。', '陪伴|聊天模式', control('profile', 'companion'), press='after_stop')]},
    {'id': 'tools', 'title': '计算、单位换算和天气', 'seconds': 55,
     'narration': '除了聊天，它还可以做受限四则计算、单位换算，以及查询日天气预报。天气播报会说明获取时间，屏幕也会显示摘要。',
     'actions': [voice('计算三加五乘二。', '计算|三加五|3.*5', utility('tool', 'calculate', '13|十三')),
                 voice('一公里等于多少米？', '公里', utility('tool', 'convert', '1000|一千')),
                 voice('明天天气怎么样？', '明天.*天气|天气.*明天', utility('weather', '1', '明天'))]},
    {'id': 'timers', 'title': '计时、暂停继续和提醒', 'seconds': 95,
     'narration': '计时和相对提醒也由本地程序管理。屏幕底部能看到倒计时，支持暂停、继续和到点播报；提醒任务保存在电脑端，使用时需要服务和设备保持连接。',
     'actions': [voice('计时二十秒。', '计时.*(?:二十|20)', utility('reminder', 'start', '已开始')),
                 voice('暂停计时。', '暂停计时', utility('reminder', 'pause', '已暂停')),
                 voice('继续计时。', '继续计时', utility('reminder', 'resume', '已继续')),
                 {'kind': 'wait_reminder', 'reminder_kind': 'timer', 'say': '等待计时结束，镜头拍屏幕倒计时，并录下提示音。'},
                 voice('十秒后提醒我喝水。', '提醒.*喝水', utility('reminder', 'start', '提醒')),
                 {'kind': 'wait_reminder', 'reminder_kind': 'reminder', 'say': '等待喝水提醒。可以补充讲解：休息时到点也可以播报提醒。'}]},
    {'id': 'outro', 'title': '休息与结束', 'seconds': 30,
     'narration': '最后演示休息指令。进入休息后会关闭电脑采音，大肥鱼闭眼；需要继续使用时，再按主功能键唤起。这就是目前实现的主要功能。',
     'actions': [voice('休息一下。', '休息|不聊|别听', kind='rest')]},
]


def markdown():
    lines = ['# Mini3 拍摄提词稿（约8～9分钟）', '',
             '由本人操作开发板。每章先读讲解词，再在提示助手中按回车开始操作；普通语音指令先短按主功能键，看到“我在听”后再说。演示不使用RST。', '',
             '程序观察到完成后会提示下一项；你负责判断画面与听感。重拍由你主动选择，不会自动重发对话请求。', '']
    for chapter in CHAPTERS:
        lines += [f'## {chapter["title"]}（建议{chapter["seconds"]}秒）', '', chapter['narration'], '']
        for action in chapter['actions']:
            if action['kind'] == 'wait_reminder': lines += ['- ' + action['say']]
            else:
                prefix = '直接说' if action['press'] is False else '停播后直接说' if action['press'] == 'after_stop' else '短按主键后说'
                lines += [f'- {prefix}：“{action["say"]}”']
                if action['kind'] == 'interrupt': lines += ['- 故事开始播放后，短按主键停止，随即等“我在听”并说下一句切换指令。']
        lines.append('')
    lines += ['## 拍摄要点', '',
              '- 全流程建议约8分钟，预留网络等待和讲解停顿，尽量在10分钟内结束。',
              '- 旁白阶段保持按键模式；连续对话只展示一轮，提示助手自动切回按键模式。',
              '- USB线与同局域网Wi-Fi保持连接，电脑保持服务运行。',
              '- 演示中的天气是当日接口返回，不预写天气数值；对话回答也以实际播报为准。',
              '- 未完成的项目可跳过或重拍，记录会保留真实状态。', '']
    return '\n'.join(lines)
