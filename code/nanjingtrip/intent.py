# -*- coding: utf-8 -*-
"""意图层：**先判断这句话想干什么**，再把"去/吃/找/接/回"切成结构化片段。

为什么要单独一层
----------------
以前这些判断散在 `consult.absorb()` 的六处正则里，各自去猜"X 是不是地名、
动词是哪个、时长归谁"。真实翻车例：

    我11月3号要从南京邮电大学去新街口吃日料，给我推荐一家，
    然后吃完去南京理工找朋友，大约耗时1.5小时，然后再返校

叠在一处的正则互相抢片段，结果变成「去新街口找人 + 吃饭（日料）」，
真正的南京理工反而丢了，1.5 小时也没落到任何一件事上。

一句话里本来就有多个**动作**，所以正确做法是先切成片段，每段只有一件事：

    [{verb: origin, place: 南京邮电大学},
     {verb: eat,    place: 新街口, need: 日料},
     {verb: meet,   place: 南京理工大学, duration_min: 90},
     {verb: return}]

本模块**只做语言结构**，不碰数据库、不取数、不排程：
地名解析通过 `resolve` 回调交给上层（上层可能是城市包，也可能是高德）。
这样它既能被顾问用，也能被"整段话批量录入"复用。

对外只有两个函数：
  · `classify(raw)`       → 这句话想干什么（plan/entry/modify/prune/ask/clarify/vague）
  · `split_segments(raw)` → 切出结构化动作片段
  · `analyze(raw)`        → 上面两件一起，给调用方一个完整判断
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# ---------------------------------------------------------------- 意图分类
#: 判断"在提问"的说法（问句优先于指令：先回答，别急着再排一版）
ASK_MARKS = ('为什么', '怎么样', '多少', '多少钱', '门票', '几点', '什么时候', '多久', '多远',
             '吗', '呢', '？', '?', '合理吗', '值不值', '要不要', '有没有', '能不能')

#: 判断"要方案"的说法
PLAN_MARKS = ('规划', '帮我排', '推荐几个', '推荐一下', '出一版', '给我一版',
              '行程', '计划', '玩几天', '怎么玩', '带我', '安排一下', '帮我安排')

#: 判断"在改已有安排"
MODIFY_MARKS = ('改成', '改到', '变成', '调整', '换成', '挪到', '提前', '推后', '晚点',
                '太满', '太赶', '太松', '多点', '少一点', '少排', '多加一天', '减一天',
                '多加一天', '加一天', '少一天')

#: 判断"删除/排除"
PRUNE_MARKS = ('去掉', '删掉', '删除', '取消', '拿掉', '移掉', '不要了', '换掉',
               '不想去', '不去', '别去', '不要去', '清空', '都删')

#: 判断"含糊/指代不清"
VAGUE_MARKS = ('那边', '那个', '那个什么', '老地方', '随便一个', '记不清', '你懂的',
               '就那样', '还没想好', '不知道去哪')

#: 判断"纯应答"（回答上一轮的追问，本身没有新动作）
ACK_MARKS = ('好的', '好', '行', '可以', '嗯', '对', '是的', '没错', '就这样', 'ok', 'OK')

#: 条件句触发器
CONDITION_MARKS = ('如果', '要是', '假如', '假使', '万一', '下雨就', '下雪就')

#: 片段边界词（按出现顺序切开；长的写在前面，先匹配长的）
_BOUNDARY = ('然后', '接着', '之后', '随后', '再然后', '最后', '另外', '顺便',
             '吃完饭', '吃完', '饭后', '吃过', '再回', '再去', '再吃')

#: 归到"这一件事"的动词（顺序即优先级：越具体的越先认）
_VERBS = (
    ('return', ('返校', '回校', '回学校', '回宿舍', '回酒店', '回到酒店', '返回', '回去',
                '回住处', '回宾馆')),
    ('pickup', ('接人', '接朋友', '接她', '接他', '接一下')),
    ('meet', ('找朋友', '见朋友', '找人', '见面', '碰面', '找同学', '见同学', '拜访')),
    ('eat', ('吃饭', '吃日料', '吃火锅', '吃小吃', '吃面', '吃烧烤', '吃西餐', '吃午饭',
             '吃晚饭', '吃早餐', '用餐', '喝咖啡', '喝一杯')),
    ('play', ('游玩', '逛逛', '游览', '参观', '看展', '散步', '逛街')),
    ('go', ('出发', '前往', '抵达', '到达', '过去', '到', '去')),
)

#: 「去X吃Y」里的动词：吃/喝 → 这顿饭的片区；找/见/接 → 去那儿的正事
_EAT_VERB = ('吃', '喝')
_VISIT_VERB = ('找', '见', '接')

#: 菜系词（"吃日料"要的是一类店，不是一家叫"日料"的店）
CUISINE_WORDS = ('日料', '日本料理', '火锅', '小吃', '烧烤', '西餐', '川菜', '韩餐',
                 '淮扬菜', '面', '素食', '清真')

#: 这些词出现在动词和地名之间时是噪声（"给我推荐一家""大约耗时"）
_NOISE = ('给我', '帮我', '推荐', '一家', '一下', '大约', '大概', '耗时', '用时',
          '顺便', '我们', '我想', '我想去', '想去', '要', '再', '先', '就', '都',
          '把', '给', '今天', '明天', '后天', '大后天', '上午', '下午', '中午',
          '有一天', '某天', '那天', '这回', '这次',
          '晚上', '傍晚', '早上', '今晚', '明早')

_DUR_RE = re.compile(r'(\d+)\s*个\s*半\s*(小时|钟头)'
                     r'|(\d+(?:\.\d+)?)\s*(?:个)?\s*(小时|钟头|h|H)'
                     r'|(\d+)\s*(分钟|分|min)|(半)\s*(?:个)?\s*小时'
                     r'|(一|两|二|三|四|五|六|七|八|九|十)\s*(?:个)?\s*小时')


def parse_duration_min(text: str) -> int:
    """从文字里取"大约耗时"（1.5 小时 → 90；半小时 → 30）。取不到返回 0"""
    m = _DUR_RE.search(str(text or ''))
    if not m:
        return 0
    if m.group(1):                      # "3个半小时" = 210
        return int(m.group(1)) * 60 + 30
    if m.group(3):
        try:
            return max(1, int(round(float(m.group(3)) * 60)))
        except ValueError:
            return 0
    if m.group(5):
        return int(m.group(5))
    if m.group(7):
        return 30
    if m.group(8):
        return {'一': 60, '两': 120, '二': 120, '三': 180, '四': 240,
                '五': 300, '六': 360, '七': 420, '八': 480, '九': 540,
                '十': 600}.get(m.group(8), 0)
    return 0


def _hits(text: str, words) -> int:
    return sum(1 for w in words if w and w in text)


def classify(raw: str) -> dict:
    """这句话想干什么。返回 {kind, confidence, why}

    口径：**问句优先于指令**（先回答，别又排一版）；含糊要追问而不是猜；
    有删除词就按删除处理（用户说"去掉"不是"再来一版"）。
    """
    text = str(raw or '').strip()
    if not text:
        return {'kind': 'clarify', 'confidence': 0.9, 'why': '空输入'}
    if len(text) <= 3 and any(text.startswith(w) for w in ACK_MARKS):
        return {'kind': 'clarify', 'confidence': 0.6, 'why': '纯应答，没有新动作'}

    prune = _hits(text, PRUNE_MARKS)
    modify = _hits(text, MODIFY_MARKS)
    ask = _hits(text, ASK_MARKS)
    plan = _hits(text, PLAN_MARKS)
    vague = _hits(text, VAGUE_MARKS)
    segs = split_segments(text)

    # "既问时刻又要方案"（帮我规划…推荐什么时候出发）：按要方案走
    # 但"这样安排合理吗"里的"安排"是名词，句尾有问号/吗就是问句
    soft_question = bool(re.search(r'(吗|呢|？|\?)\s*$', text))
    if ask and not plan and not prune and not modify:
        return {'kind': 'ask', 'confidence': 0.8, 'why': '问句'}
    if soft_question and not plan and not prune and not modify:
        return {'kind': 'ask', 'confidence': 0.7, 'why': '句尾是问法'}
    if prune and not plan:
        return {'kind': 'prune', 'confidence': 0.8, 'why': '删除/排除'}
    if modify and not plan:
        return {'kind': 'modify', 'confidence': 0.75, 'why': '改已有安排'}
    if plan or len(segs) >= 2:
        return {'kind': 'plan', 'confidence': 0.7 if plan else 0.55,
                'why': '要方案' if plan else '一句话里有多件事'}
    if vague and not segs:
        return {'kind': 'vague', 'confidence': 0.7, 'why': '指代不清'}
    if segs:
        return {'kind': 'entry', 'confidence': 0.7, 'why': '在报地点/办事'}
    return {'kind': 'clarify', 'confidence': 0.5, 'why': '没看出要做什么'}


def _clean(piece: str) -> str:
    text = str(piece or '')
    # 噪声只在**开头**剥：全局替换会把"先锋书店"里的"先"、"就近"里的"就"也吃掉
    changed = True
    while changed:
        changed = False
        text = re.sub(r'^(给我|帮我|我们|我想去|我想|想去|要|就|都|先|再|把|给|另外|顺便)', '',
                      text.strip())
        for w in _NOISE:
            if text.startswith(w):
                text = text[len(w):]
                changed = True
    # 去掉日期/时刻（"11月3号""9点半""40分钟"）——它们是时长与日期，不是地名
    text = re.sub(r'\d{1,2}\s*月\s*\d{1,2}\s*[日号]?', '', text)
    text = re.sub(r'\d{1,2}\s*[点时](\s*半|\s*\d{1,2}\s*分)?', '', text)
    text = re.sub(r'\d+(\.\d+)?\s*(小时|钟头|分钟|分)', '', text)
    text = re.sub(r'^[我在我们]+', '', text.strip())
    # 动词残留：'去/到/在/从' 是介词，不是地名的一部分
    text = re.sub(r'^[去到在从]+', '', text.strip())
    text = re.sub(r'(去|到|在|从|掉|了)$', '', text.strip())
    return text.strip(' ，,。.；;、\t\n')


def _looks_like_duration(text: str) -> bool:
    """'1.5小时''半小时''11月3号' 这类不是地名"""
    t = str(text or '')
    if re.search(r'\d', t) and re.search(r'(小时|钟头|分钟|分|h|H|天|月|号|日)', t):
        return True
    return bool(re.fullmatch(r'[半一两二三四五六七八九十]+\s*(个)?\s*(小时|钟头)', t))


def _split_raw(raw: str) -> list[str]:
    """先按标点切句，再按连接词切；返回候选片段（顺序保留）"""
    text = str(raw or '')
    parts: list[str] = []
    for chunk in re.split(r'[，,。；;！!？?\n]+', text):
        chunk = chunk.strip()
        if not chunk:
            continue
        cut = chunk
        for word in _BOUNDARY:
            cut = cut.replace(word, '\x00')
        parts.extend(p.strip() for p in cut.split('\x00') if p.strip())
    return parts


def _verb_of(piece: str) -> tuple[str, str, str]:
    """返回 (verb, 动词**之前**的文字, 动词之后的文字)

    地名到底在动词哪一边，取决于动词：
      · "去新街口吃日料" / "南京理工找朋友" → 地名在**前面**
      · "去先锋书店"                      → 地名在**后面**
    一起取两半，由调用方按动词挑。
    """
    for verb, words in _VERBS:
        for word in words:
            at = piece.find(word)
            if at >= 0:
                return verb, piece[:at], piece[at + len(word):]
    return '', piece, ''


#: 这些动词的地名在动词**后面**（"去X""到X""前往X"）
_PLACE_AFTER = ('go',)


#: 明显不是地名的词（要求方案、客套话）
_NOT_PLACE_WORDS = ('规划', '安排', '行程', '方案', '建议', '推荐', '随便', '附近',
                    '那边', '这里', '那里', '怎么样', '合理', '可以', '就近随便',
                    '吃', '喝', '玩', '逛', '吃饭')

def split_segments(raw: str, resolve: Optional[Callable[[str], object]] = None,
                   is_place: Optional[Callable[[str], bool]] = None) -> list[dict]:
    """把一句话切成动作片段：``[{verb, place, near, need, duration_min, text}]``

    `resolve` 负责"这段文字对应哪个地点"（返回带 `name`/`kind` 的对象或 None）；
    `is_place` 判断一个词像不像地名（默认只做长度与停用词粗筛）。
    两者都可以不传——那样 `place` 只会是文字本身，由调用方再解析。
    """
    is_place = is_place or (lambda w: 2 <= len(w) <= 16)
    segments: list[dict] = []
    for piece in _split_raw(raw):
        verb, head, tail = _verb_of(piece)
        duration = parse_duration_min(piece)
        # 地名在动词后（去X）还是前（X吃/找/玩），按动词挑那一边
        rest = tail if verb in _PLACE_AFTER else head
        cleaned = _clean(rest)
        # 「从X」「我在X」是出发地，不是一件事。截到动词前为止，
        # 否则会把"南京邮电大学去新街口吃日料"整段当出发地。
        origin = None
        m_from = re.search(r'(?:从|我在|我们住在)\s*([\u4e00-\u9fa5A-Za-z0-9]{2,16}?)'
                           r'(?=出发|去|到|前往|吃|喝|找|见|接|返回|返校|回|，|,|。|$)', piece)
        if m_from:
            cand = _clean(m_from.group(1))
            if is_place(cand) and not _looks_like_duration(cand):
                origin = cand
                # "从南邮去新街口吃日料"：把出发那一半切掉，剩下的才是这顿饭的片区
                rest = rest.replace(m_from.group(0), '')
                cleaned = _clean(rest)
        # 地名：先问 resolve，再退到"去掉动词后剩下的那个词"
        place = None
        poi = None
        if resolve is not None and cleaned and not _looks_like_duration(cleaned):
            for cand in _place_candidates(cleaned, origin or ''):
                try:
                    got = resolve(cand)
                except Exception:             # noqa: BLE001 取不到就跳过
                    got = None
                if got is not None:
                    poi = got
                    place = getattr(got, 'name', None) or cand
                    break
        if (place is None and cleaned and is_place(cleaned)
                and not _looks_like_duration(cleaned)
                and not any(cleaned.startswith(w) for w in _NOT_PLACE_WORDS)):
            place = cleaned
        # 「吃日料」：动词后面跟着的是菜系，不是地名
        need = ''
        for word in CUISINE_WORDS:
            if word in piece:
                need = word
                break
        if verb == '' and need and any(w in piece for w in ('吃', '喝')):
            verb = 'eat'
        if verb == '' and origin is None and not place:
            # 只有时长、没有动作（"大约耗时1.5小时"）：算到上一件事头上
            if duration and segments:
                segments[-1]['duration_min'] = (segments[-1].get('duration_min') or 0) + duration
                segments[-1]['text'] += '；' + piece
                continue
        if verb in ('', 'go') and not place and duration and segments:
            # "去南京理工找朋友，大约耗时1.5小时" 被标点切开了：时长补给上一件正事
            segments[-1]['duration_min'] = (segments[-1].get('duration_min') or 0) + duration
            segments[-1]['text'] += '；' + piece
            continue
        seg = {'verb': verb or ('origin' if origin else 'go'),
               'place': place or '', 'poi': poi, 'need': need,
               'duration_min': duration, 'text': piece}
        if origin:
            seg['origin'] = origin
        if any(w in piece for w in CONDITION_MARKS):
            seg['condition'] = True
        segments.append(seg)
    return segments


def _place_candidates(cleaned: str, origin: str) -> list[str]:
    """从一段文字里给出可能的"地名"候选（长的先试，且**不短于 3 个字**）

    为什么不试 2 字前缀：`_poi_named('南京')` 会被模糊命中到"南京站"，
    于是"南京邮电大学"这种词里就凭空多出一个火车站（真实事故）。
    """
    text = cleaned or ''
    if origin and text.startswith(origin):
        text = text[len(origin):]
    text = re.sub(r'^[去到在从]+', '', text).strip()
    text = re.sub(r'(附近|一带|那边|这儿|这里|集合|吃饭|碰头)+$', '', text).strip()
    out = []
    if len(text) >= 3:
        out.append(text)
        # 「有一天去南邮」这类地名在**结尾**：按介词切开，取后半段当候选
        for piece in re.split(r'[去到在从]+', text):
            piece = piece.strip()
            if len(piece) >= 2 and piece != text:
                out.append(piece)
        for cut in range(len(text) - 1, 2, -1):
            out.append(text[:cut])
            out.append(text[len(text) - cut:])
    return out


def analyze(raw: str, resolve: Optional[Callable[[str], object]] = None,
            is_place: Optional[Callable[[str], bool]] = None) -> dict:
    """分类 + 切片一次给出"""
    judgement = classify(raw)
    segments = split_segments(raw, resolve=resolve, is_place=is_place)
    judgement['segments'] = segments
    return judgement
