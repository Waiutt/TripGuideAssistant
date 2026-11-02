# -*- coding: utf-8 -*-
"""中文自然语言规则解析（规则兜底）。

用途：
1) 离线模式下替代 LLM 完成意图抽取（MockLLMAdapter 调用）；
2) 在线模式下 LLM 输出不合格时的兜底（提升鲁棒性）。
输出结构与 LLM 约定的 JSON schema 一致。
"""
from __future__ import annotations

import re
from datetime import date, timedelta

CN_NUM = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
          '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

WEEK_CN = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}

CATEGORY_RULES = [
    ('医疗', ('医院', '看病', '复诊', '体检', '取报告', '挂号', '牙科', '门诊', '药')),
    ('餐饮', ('吃饭', '午餐', '晚餐', '早餐', '用餐', '聚餐', '餐厅', '咖啡', '奶茶', '吃')),
    ('展览', ('看展', '展览', '博物', '美术馆', '演出', '话剧', '音乐会', '电影', '展')),
    ('办事', ('办事', '银行', '邮局', '快递', '政务', '社保', '签合同', '开会', '面试', '取件')),
]


def _cn2int(s: str):
    """数字/中文数字 -> int（支持 1.5 这类小数与 0~99 中文数字）"""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', s):
        return float(s) if '.' in s else int(s)
    if s == '十':
        return 10
    if '十' in s:
        left, _, right = s.partition('十')
        tens = CN_NUM.get(left, 1) if left else 1
        ones = CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(s) == 1 and s in CN_NUM:
        return CN_NUM[s]
    return None


def parse_date(text: str, today: date) -> date | None:
    """解析日期：相对日 / 周几 / 月日 / 显式日期"""
    if re.search(r'今天|今日|今儿', text):
        return today
    if re.search(r'大后天', text):
        return today + timedelta(days=3)
    if re.search(r'后天', text):
        return today + timedelta(days=2)
    if re.search(r'明天|明日|明儿', text):
        return today + timedelta(days=1)

    m = re.search(r'(下)?(?:周|星期|礼拜)([一二三四五六日天])', text)
    if m:
        target = WEEK_CN[m.group(2)]
        if m.group(1):
            # 下周X：以「下周一」为基准再加偏移
            next_monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
            return next_monday + timedelta(days=target)
        delta = (target - today.weekday()) % 7
        if delta == 0:            # 本周X 且今天就是周X → 视为下周同一天
            delta = 7
        return today + timedelta(days=delta)

    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = re.search(r'(\d{1,2})月(\d{1,2})[日号]?', text)
    if m:
        d = date(today.year, int(m.group(1)), int(m.group(2)))
        return d if d >= today else date(today.year + 1, int(m.group(1)), int(m.group(2)))

    m = re.search(r'(\d{1,2})[-/](\d{1,2})(?!\d)', text)
    if m:
        mo, dd = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            d = date(today.year, mo, dd)
            return d if d >= today else date(today.year + 1, mo, dd)
    return None


def parse_time(text: str) -> tuple[int, int] | None:
    """解析时刻，返回 (hour, minute)"""
    period = ''
    if re.search(r'凌晨', text):
        period = 'dawn'
    elif re.search(r'早上|早晨|上午|清早', text):
        period = 'am'
    elif re.search(r'中午|正午', text):
        period = 'noon'
    elif re.search(r'下午|午后', text):
        period = 'pm'
    elif re.search(r'傍晚|晚上|夜里', text):
        period = 'night'

    # 9:30 / 09：30
    m = re.search(r'(\d{1,2})[:：](\d{1,2})', text)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 九点 / 九点半 / 九点三十分 / 10点
    m = re.search(r'(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点\s*'
                  r'(半|一刻|三刻|\d{1,2}|[一二三四五六七八九十]{1,3})?', text)
    if m:
        h = _cn2int(m.group(1))
        if h is None:
            return None
        mm = 0
        tail = m.group(2)
        if tail and not tail.isdigit() and tail not in ('半', '一刻', '三刻'):
            _v = _cn2int(tail)
            if _v is not None:
                tail = str(_v)
        if tail == '半':
            mm = 30
        elif tail == '一刻':
            mm = 15
        elif tail == '三刻':
            mm = 45
        elif tail and tail.isdigit():
            mm = int(tail)
        if period in ('pm', 'night') and h < 12:
            h += 12
        elif period == 'noon' and h < 11:
            h += 12
        if 0 <= h <= 23:
            return h, mm

    if period == 'noon':
        return 12, 0
    return None


_CN = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6,
       '七': 7, '八': 8, '九': 9, '十': 10}
_HR_RANGE = re.compile(r'([一二两三四五六七八九])\s*[、到~-]?\s*([一二两三四五六七八九])'
                       r'\s*个?\s*(?:半)?\s*小时')
_HR_CN = re.compile(r'([一二两三四五六七八九]|十[一二三四五六七八九]?)\s*个?\s*钟头')
_EN_DUR = re.compile(r'(\d+(?:\.\d+)?)\s*h(?:ours?)?\s*(?:(\d{1,2})\s*m(?:in)?s?)?'
                     r'|(\d{1,3})\s*m(?:in)?s?(?![a-z])')


def _extra_duration(text: str):
    """补充几种常见写法：两三个小时、两个钟头、1h30m、90min"""
    s = text or ''
    m = _HR_RANGE.search(s)
    if m:
        a, b = _CN.get(m.group(1), 0), _CN.get(m.group(2), 0)
        if a and b:
            return int(round((a + b) / 2 * 60))
    m = _HR_CN.search(s)
    if m:
        v = _CN.get(m.group(1))
        if v:
            return v * 60
    m = _EN_DUR.search(s)
    if m:
        if m.group(1):
            return int(round(float(m.group(1)) * 60)) + int(m.group(2) or 0)
        if m.group(3):
            return int(m.group(3))
    return None


def parse_duration(text: str) -> int | None:
    """解析耗时，返回分钟"""
    # 1个半小时 / 一个半小时
    m = re.search(r'(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*个?\s*半\s*小时', text)
    if m:
        h = _cn2int(m.group(1))
        if h is not None:
            return h * 60 + 30

    # 半小时 / 半个小时
    if re.search(r'半\s*个?\s*小时', text):
        return 30

    # X小时Y分钟（Y 可省略）
    m = re.search(r'(\d{1,2}(?:\.\d)?|[一二两三四五六七八九十]{1,2})\s*个?\s*小时'
                  r'(?:\s*(\d{1,3})\s*分钟?|([一二三四五六七八九十]{1,3})\s*分)?', text)
    if m:
        h = _cn2int(m.group(1))
        mins = _cn2int(m.group(2) or m.group(3)) if (m.group(2) or m.group(3)) else 0
        if h is not None:
            return int(round(float(h) * 60)) + (mins or 0)

    # X分钟 / X min
    m = re.search(r'(\d{1,3}|[一二三四五六七八九十]{1,3})\s*(?:分钟|min|分(?!钟))', text)
    if m:
        v = _cn2int(m.group(1))
        if v is not None:
            return v

    # 2h / 1.5h
    m = re.search(r'(\d{1,2}(?:\.\d)?)\s*h(?:ours?)?\b', text, re.I)
    if m:
        return int(round(float(m.group(1)) * 60))

    # 半天
    if re.search(r'半天', text):
        return 240
    extra = _extra_duration(text)
    if extra:
        return extra
    return None


def parse_mode(text: str) -> str | None:
    if re.search(r'步行|走路|走过去', text):
        return 'walking'
    if re.search(r'地铁|公交|坐车|公共交通', text):
        return 'transit'
    if re.search(r'打车|驾车|开车|自驾|出租', text):
        return 'driving'
    return None


def match_poi(text: str, poi_names: list[str], aliases: dict[str, str] | None = None
              ) -> str | None:
    """在文本中匹配地点名（最长匹配优先，支持别名）"""
    aliases = aliases or {}
    # 别名优先
    for alias in sorted(aliases, key=len, reverse=True):
        if alias in text:
            return aliases[alias]
    hits = [n for n in poi_names if n in text]
    if hits:
        return max(hits, key=len)

    # "去/到/在 + 短语 + 做某事" 结构
    m = re.search(r'(?:去|到|在|前往)([\u4e00-\u9fa5A-Za-z0-9]{2,12}?)'
                  r'(?:看|吃|办|取|见|参加|开|买|做|逛|听|体检|复诊|开会|吃饭)', text)
    if m:
        return m.group(1)
    return None


def guess_category(text: str) -> str:
    for cat, keys in CATEGORY_RULES:
        if any(k in text for k in keys):
            return cat
    return '其他'


def extract_item(text: str, mention: str | None) -> str:
    """抽取事项名称：剔除时间/地点/耗时等修饰后保留核心动宾短语"""
    core = text
    if mention:
        core = core.replace(mention, '')
    # 逗号分隔时优先取含动宾短语的那一段（如"鼓楼医院取报告，半小时" → "取报告"）
    segs = [s for s in re.split(r'[，,。；;！!？?]', core) if s.strip()]
    if len(segs) > 1:
        scored = sorted(segs, key=lambda s: (-len(re.findall(
            r'看|吃|取|办|见|参加|开|买|做|逛|听|散步|体检|复诊|开会|签到|拜访', s)), -len(s)))
        core = scored[0] if scored else core
    core = re.sub(r'(今天|今日|明天|明日|后天|大后天|下周|周[一二三四五六日天]|星期[一二三四五六日天])', '', core)
    core = re.sub(r'(早上|早晨|上午|中午|下午|傍晚|晚上|凌晨)', '', core)
    core = re.sub(r'(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点\s*(半|一刻|三刻|\d{1,2})?', '', core)
    core = re.sub(r'\d{1,2}[:：]\d{1,2}', '', core)
    # 耗时表达（含"半小时""2小时""1个半小时""30分钟"等）
    core = re.sub(r'(大概|大约|约|差不多|需要|用时|花费?|时长|耗时)?\s*'
                  r'((\d{1,2}(?:\.\d)?|[一二两三四五六七八九十]{1,3})\s*个?\s*)?'
                  r'(半\s*)?(个)?\s*(小时|分钟|min|h)\s*'
                  r'((\d{1,3}|[一二三四五六七八九十]{1,3})\s*分钟?)?', '', core)
    core = re.sub(r'(去|到|前往|在|要|想|准备|打算)', ' ', core)
    core = re.sub(r'[，,。、；;！!？?]', ' ', core)
    core = re.sub(r'\s+', ' ', core).strip()
    return core[:24] or '未命名事项'


def rule_based_parse(text: str, today: date, poi_names: list[str],
                     aliases: dict[str, str] | None = None) -> dict:
    """规则解析主入口，返回与 LLM 约定一致的 dict"""
    d = parse_date(text, today)
    t = parse_time(text)
    dur = parse_duration(text)
    mode = parse_mode(text)
    mention = match_poi(text, poi_names, aliases) or ''
    item = extract_item(text, mention)
    category = guess_category(text)

    missing = []
    if dur is None:
        missing.append('耗时')
    if not mention:
        missing.append('地点')

    found = sum(x is not None for x in (d, t, dur, mode)) + (1 if mention else 0)
    return {
        'date': d.isoformat() if d else None,
        'start_hint': f'{t[0]:02d}:{t[1]:02d}' if t else None,
        'mention': mention,
        'item': item,
        'duration_min': dur or 0,
        'mode_hint': mode,
        'category': category,
        'confidence': round(min(0.95, 0.45 + 0.1 * found), 2),
        'missing': missing,
        'raw_text': text,
    }


# ======================================================================
# 批量多目的地：把"我要依次去 A、B、C"切成多条单点意图
# ======================================================================
#: 句末的"帮我规划…"这类指令尾巴，切分前先去掉
_TAIL_CRUTCH = re.compile(
    r'(帮我|请|麻烦)?(规划|安排|生成|计算|自动计算|自动匹配|排出).{0,40}?(行程表|路线|行程|路线图|方案).*$')
#: "我现在在 X" / "从 X 出发" / "起点是 X" —— 起点声明单独提取
_ORIGIN = re.compile(
    r'(?:我现在在|我目前在|我现在位于|目前位于|我在|人在|我从|从|出发地?是|起点(?:是|在)?)\s*'
    r'([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,20}?)(?:出发|开始|，|,|。|$|；|;)')
#: 强分隔符（出现在这些位置几乎一定是"下一件事"）。
#: 句号也算：整段里常把"…1小时。全程尽量少走路"连在一起，不按句号断，
#: 后半句的偏好说明会被当成前半句的标题（实测出现过标题变成"全程尽量少走路"）。
_STRONG_SPLIT = re.compile(r'[、；;。！!？?]|(?<!\d)[,/→>|｜](?!\d)|然后|接着|之后|再去|再去一趟|最后去|最后一个|其次|再有|另外')
#: 绝对截止："10:30 前必须抵达" / "九点一刻之前到"
_DEADLINE = re.compile(
    r'(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[点:：]\s*'
    r'(半|一刻|三刻|\d{1,2})?\s*分?\s*(?:之前|以前|前)\s*'
    r'(?:必须|需要|要|得)?\s*(?:到达|抵达|赶到|赶|到)')


def parse_deadline(text: str) -> tuple[int, int] | None:
    """从"10:30 前必须抵达"这类表述里取绝对截止时刻，返回 (时, 分)。

    与"锁定开始时刻"不同：deadline 允许更早到，只禁止更晚。
    """
    m = _DEADLINE.search(text or '')
    if not m:
        return None
    return parse_time(m.group(0))
#: 带"动作"的词：碎片里有它才算一件独立的事，否则只是时长/说明补充
_ACTION_WORD = re.compile(r'去|到|逛|看|参观|游览|吃|喝|取|办|听|买|见|开|做|参加|签到|散步')
#: 序号前缀："1. " "① " "第一站 "
_NUM_PREFIX = re.compile(r'^\s*(?:\d{1,2}[.、)）]|[①②③④⑤⑥⑦⑧⑨]|第[一二三四五六七八九十\d]{1,2}[站个处])')
#: 编号列表被压成一行时的序号边界（"1. A 2. B 3. C" / "①A②B"）。
#: 数字后必须紧跟序号符号且后面不是数字，避免把 "9.5 公里" 这类小数当序号。
_NUM_SPLIT = re.compile(
    r'(?:^|(?<=[\s，,。；;、）)\u4e00-\u9fff]))\s*\d{1,2}\s*[.、)）](?!\d)\s*'
    r'|[①②③④⑤⑥⑦⑧⑨]')


#: 括号里的**附注**（"（这个必须去，我已经预约了）"）：整段去掉，别让它挤掉地点名。
#: 括号里带时长的（"（停留40分钟）"）不算附注，必须保留。
_NOTE_PAREN = re.compile(
    r'[（(]([^（()）]{0,40}?(?:必须去|必须到|一定要去|已经预约|已预约|需要预约|要预约|'
    r'看情况|人多就算|随便|无所谓|可选|如果|要是|记得|注意)[^（()）]{0,40}?)[）)]')


def strip_note_parens(text: str) -> str:
    """去掉括号里的附注内容（保留括号外的地点与时长）"""
    if not text or ('（' not in text and '(' not in text):
        return text or ''
    out = _NOTE_PAREN.sub('', text)
    return re.sub(r'\s{2,}', ' ', out).strip()


def split_by_pois(text: str, names) -> list[str]:
    """无标点长句的兜底切分：按句中出现的**已知地点名**切开。

    "上午去南京博物院看展两个小时中午在新街口吃个饭下午去先锋书店……"
    这类整段没有任何分隔符的输入，强分隔符切不出东西，只解析出第一处地点、
    其余整句**静默消失**。地点名本身是天然边界，用它切比猜标点可靠。

    只在命中 ≥2 个地点时返回结果，否则返回空列表（交给调用方退化处理）。
    """
    raw = (text or '').strip()
    if not raw or not names:
        return []
    hits: list[tuple[int, str]] = []
    for n in {str(x) for x in names if x}:
        start = 0
        while True:
            i = raw.find(n, start)
            if i < 0:
                break
            hits.append((i, n))
            start = i + len(n)
    if len(hits) < 2:
        return []
    # 同一位置命中多个名字时保留更长的（"南京博物院" 优先于 "博物院"）
    hits.sort(key=lambda x: (x[0], -len(x[1])))
    picked: list[tuple[int, str]] = []
    for i, n in hits:
        if picked and i < picked[-1][0] + len(picked[-1][1]):
            continue
        picked.append((i, n))
    if len(picked) < 2:
        return []
    chunks: list[str] = []
    for k, (i, n) in enumerate(picked):
        end = picked[k + 1][0] if k + 1 < len(picked) else len(raw)
        chunk = raw[i:end].strip(' ，,。.；;、')
        if chunk:
            chunks.append(chunk)
    if not chunks:
        return []
    # 首个地点之前的"明天下上午"这类时间前缀并入第一段，别丢
    head = raw[:picked[0][0]].strip(' ，,。.；;、')
    if head:
        chunks[0] = f'{head}，{chunks[0]}'
    return chunks


def extract_origin(text: str) -> tuple[str | None, str]:
    """抽出起点声明，返回 (起点名|None, 去掉起点声明后的文本)"""
    m = _ORIGIN.search(text or '')
    if not m:
        return None, text or ''
    name = m.group(1).strip()
    # "从今天11月1日白天出行" 这类误抓：至少含 2 个汉字且不是纯数字/日期
    if len(name) < 2 or re.fullmatch(r'[\d\s年月日号:：点分]+', name):
        return None, text
    return name, (text[:m.start()] + text[m.end():])


def split_batch(text: str) -> list[str]:
    """把一个多目的地句子切成若干条"单点意图"。

    保守策略：先用强分隔符切；切出的碎片若仍含多个"去/到 X"，再用逗号细切。
    返回的碎片保留了各自的地点与时长（如"总统府（停留1小时）"）。
    """
    raw = (text or '').strip()
    if not raw:
        return []
    raw = _TAIL_CRUTCH.sub('', raw)
    # 「需要去的地点：A、B、C」这类标签句式：只保留冒号之后的地点清单
    raw = re.sub(r'^.{0,40}?(?:需要|要|想去|计划)?去的?地[点方]?[:：]?', '', raw, count=1)
    # 冒号前若是"没有固定顺序/帮我自动排序"这类交代表述，直接丢掉。
    # 注意：**时刻里的冒号不是分隔符**——"赶 21:00 的高铁"曾把整段前半句切成废料，
    # 只剩"00 的高铁回上海"一个片段（R1 真实口语模拟暴露）。
    m_colon = re.search(r'(?<![\d])\s*[：:]\s*(?![\d])', raw)
    if m_colon:
        left, right = raw[:m_colon.start()], raw[m_colon.end():]
        if len(right.strip()) >= 4 and not parse_duration(left):
            raw = right
    _, raw = extract_origin(raw)
    raw = raw.replace('\n', '；')
    # 编号列表被压成一行（"1. A 2. B 3. C"）：不按序号断开就只会解析出第一项，
    # 后面的地点**静默丢失**（连"未解析"都不会提示），所以这里必须先断句。
    if len(_NUM_SPLIT.findall(raw)) >= 2:
        raw = _NUM_SPLIT.sub('；', raw)
    parts = [p.strip(' ，,。.') for p in _STRONG_SPLIT.split(raw)]
    parts = [_NUM_PREFIX.sub('', p).strip(' ，,。.') for p in parts if p and p.strip(' ，,。.')]
    # 二次细切：一个碎片里出现两次及以上"去/到"时，按逗号再分
    out: list[str] = []
    for p in parts:
        if len(re.findall(r'(?:去|到|逛|看|参观|游览)', p)) >= 2 and '，' in p:
            merged: list[str] = []
            for bit in (x.strip(' ，,。.') for x in p.split('，')):
                if not bit:
                    continue
                # "大概2小时""需要1小时"这类只是上一件事的时长补充，不是独立的一件事：
                # 若单独成段，时长就丢了（"去X看展，大概2小时" 会被拆散）。
                if merged and not _ACTION_WORD.search(bit):
                    merged[-1] = f'{merged[-1]}，{bit}'
                else:
                    merged.append(bit)
            out.extend(merged)
        else:
            out.append(p)
    # 去掉只剩"我需要""出行"这类无地点无时长的碎片
    keep = []
    for p in out:
        if not p:
            continue
        # 去掉碎片尾部的整体说明（如"。每个地方平均停留50分钟"）
        p = re.sub(r'[。；;]\s*(?:每个|每个地方|每处|平均).*$', '', p).strip(' ，,。.')
        if not p:
            continue
        if re.search(r'[\u4e00-\u9fa5]{2,}', p) and (
                parse_duration(p) is not None or len(p) >= 2):
            keep.append(p)
    return keep

#: "每个地方平均停留50分钟" / "每处1小时" 这类**统一时长**（批量场景）
_GROUP_DURATION = re.compile(
    r'(?:每个|每处|每个地方|每个点|平均)\s*(?:地方|地点|点)?\s*'
    r'[^。；;\d一二三四五六七八九十]{0,6}?'
    r'(\d+(?:\.\d+)?|[一二三四五六七八九十两]{1,3})\s*'
    r'(个半)?\s*(小时|钟头|分钟|分)')


def parse_group_duration(text: str) -> int | None:
    """整句共用的停留时长（分钟）。用于"5 个地方每个停留 50 分钟"这种写法。"""
    m = _GROUP_DURATION.search(text or '')
    if not m:
        return None
    num = _cn2int(m.group(1))
    if num is None:
        return None
    unit = m.group(3)
    minutes = num * 60 if unit in ('小时', '钟头') else num
    if m.group(2):
        minutes += 30
    return int(minutes) if minutes > 0 else None
