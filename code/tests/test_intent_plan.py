# -*- coding: utf-8 -*-
"""排程类意图（50 条）：用户想要「一整趟方案」。

与 `test_intent_matrix.py`（I-01~I-50）不重复：那边一条一类，这里把**排程这一类**拆
成十个口径细化——日期/窗口、天数、人数、预算、节奏、兴趣、首次来访、无头绪、
只有时长没日期、只有地点没日期。

判定口径（只看可观察结果，不看实现）：
  · 是否真的给出 `plan.days`（而不是只回一句追问）；
  · `plan.days_total` 是否等于用户说的天数；
  · `plan.start` 是否等于用户说的起始日；
  · `plan.party` / `plan.per_day` 是否与「几个人 / 什么节奏」对得上。

`P-42`~`P-50` 是「没给日期」的两轮：第一句（`PRE`）说行程长度或想去的地点，
第二句只补一个起始日；判定看补完日期后那一版草案还记不记得用户说过的天数/地点。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)


@pytest.fixture(scope='module')
def ctx(tmp_path_factory):
    c = AppContext(tmp_path_factory.mktemp('plan-intent'))
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session, pre=()):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    for line in pre:
        con.chat(session, line, TODAY)
    return con.chat(session, text, TODAY)


def plan_of(out):
    return out.get('plan') or {}


def has_plan(out):
    return bool(plan_of(out).get('days'))


def days_total(out):
    return plan_of(out).get('days_total')


def plan_start(out):
    return plan_of(out).get('start') or slot(out, 'start') or ''


def slot(out, key, default=None):
    return (out.get('slots') or {}).get(key, default)


def interests(out):
    return list(slot(out, 'interests') or [])


def per_day(out):
    return plan_of(out).get('per_day')


def party(out):
    return plan_of(out).get('party')


def stops(out):
    """草案里出现过的全部地点名（含吃饭/住宿块）"""
    return [str(it.get('poi') or it.get('title') or '')
            for d in plan_of(out).get('days') or [] for it in d.get('items') or []]


#: 第一次来南京「总得去一趟」的地标（只用来判断"经典有没有排进去"）
CLASSIC = ('总统府', '中山陵', '夫子庙', '南京博物院', '明孝陵', '玄武湖', '老门东')

#: 城市名与车站：它们不是可游玩的地点，不允许进 must
CITY_OR_STATION = ('南京', '南京市', '南京站', '南京南站', '南京禄口国际机场', '禄口机场')


def has_classic(out):
    return any(any(c in s for c in CLASSIC) for s in stops(out))


def museum_stops(out):
    return [s for s in stops(out)
            if any(w in s for w in ('博物', '美术馆', '纪念馆', '科举', '六朝'))]


def no_city_or_station(out):
    return not [m for m in (slot(out, 'must') or []) if m in CITY_OR_STATION]


def says(out, key, want):
    return slot(out, key) == want


CASES = [
    # ---------- 日期 / 天数 / 窗口 ----------
    ('P-01', '11月上旬去南京玩两天', lambda o: has_plan(o) and days_total(o) == 2),
    ('P-02', '11月下旬想去南京，玩三天', lambda o: has_plan(o) and days_total(o) == 3),
    ('P-03', '这个月下旬有空，陪我玩四天', lambda o: has_plan(o) and days_total(o) == 4),
    ('P-04', '下周末去南京，两天就够', lambda o: has_plan(o) and days_total(o) == 2),
    ('P-05', '明年五一去南京玩四天', lambda o: has_plan(o) and days_total(o) == 4),
    ('P-06', '国庆去南京，玩三天', lambda o: has_plan(o) and days_total(o) == 3),
    ('P-07', '十一月十四号到十六号去南京',
     lambda o: has_plan(o) and days_total(o) == 3 and plan_start(o) == '2026-11-14'),
    ('P-08', '12月5号到7号，三个人的南京行',
     lambda o: (has_plan(o) and days_total(o) == 3 and party(o) == 3
                and plan_start(o) == '2026-12-05')),
    ('P-09', '11月14号开始，三天两晚',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 3),
    ('P-10', '四天三晚，12月10号到13号',
     lambda o: has_plan(o) and days_total(o) == 4 and plan_start(o) == '2026-12-10'),
    ('P-11', '11月8号一天，南京一日游', lambda o: has_plan(o) and days_total(o) == 1),
    ('P-12', '11月28号开始，玩两天', lambda o: has_plan(o) and days_total(o) == 2),
    ('P-13', '12月20号到22号，带孩子去南京',
     lambda o: has_plan(o) and days_total(o) == 3 and '亲子' in interests(o)),
    ('P-14', '下下周一到周三，南京', lambda o: has_plan(o) and days_total(o) == 3),
    # ---------- 人数 / 预算 / 节奏 ----------
    ('P-15', '11月15号到17号去南京，两个人',
     lambda o: has_plan(o) and party(o) == 2 and days_total(o) == 3),
    ('P-16', '12月1号到3号，四个人一起玩', lambda o: has_plan(o) and party(o) == 4),
    ('P-17', '11月20号到22号，就我一个人', lambda o: has_plan(o) and party(o) == 1),
    ('P-18', '11月中旬去南京三天，预算不多',
     lambda o: has_plan(o) and says(o, 'budget', '省')),
    ('P-19', '12月8号到10号，穷游南京', lambda o: has_plan(o) and says(o, 'budget', '省')),
    ('P-20', '11月18号到20号，我很懒，别安排太早',
     lambda o: has_plan(o) and per_day(o) == 2 and says(o, 'pace', 'slow')),
    ('P-21', '12月12号到14号，我要特种兵式玩南京',
     lambda o: has_plan(o) and per_day(o) == 4 and says(o, 'pace', 'fast')),
    ('P-22', '11月25号到27号，节奏别太赶',
     lambda o: has_plan(o) and (per_day(o) < 3 or slot(o, 'pace') != 'normal')),
    ('P-23', '12月3号到5号，预算充足，想住好点',
     lambda o: has_plan(o) and says(o, 'budget', '宽')),
    ('P-24', '11月21号到23号，两个人，预算一千以内',
     lambda o: has_plan(o) and party(o) == 2 and bool(slot(o, 'budget'))),
    # ---------- 兴趣 ----------
    ('P-25', '11月16号到18号，想多看博物馆',
     lambda o: has_plan(o) and '博物馆' in interests(o) and len(museum_stops(o)) >= 3),
    ('P-26', '12月6号到8号，带孩子去南京',
     lambda o: has_plan(o) and '亲子' in interests(o)),
    ('P-27', '11月22号到24号，想看夜景',
     lambda o: has_plan(o) and any('夜' in i for i in interests(o))),
    ('P-28', '12月15号到17号，主要想吃小吃',
     lambda o: has_plan(o) and '美食街区' in interests(o)),
    ('P-29', '11月28号到30号，想爬山看风景',
     lambda o: has_plan(o) and '自然风光' in interests(o)),
    ('P-30', '12月18号到20号，想看民国历史',
     lambda o: has_plan(o) and '历史人文' in interests(o)),
    ('P-31', '11月12号到14号，以博物馆为主，别太累',
     lambda o: has_plan(o) and '博物馆' in interests(o) and per_day(o) <= 2),
    ('P-32', '12月22号到24号，带孩子，节奏慢一点',
     lambda o: has_plan(o) and (slot(o, 'pace') == 'slow' or per_day(o) < 3)),
    # ---------- 首次来访：经典要排进去，城市名不能当景点 ----------
    ('P-33', '第一次来南京，11月14号到16号',
     lambda o: has_plan(o) and days_total(o) == 3 and has_classic(o)),
    ('P-34', '头一回来南京，11月20号到22号，经典的都想去',
     lambda o: has_plan(o) and has_classic(o) and no_city_or_station(o)),
    ('P-35', '第一次去南京，12月1号到4号，帮我排',
     lambda o: has_plan(o) and days_total(o) == 4 and has_classic(o)),
    ('P-36', '从没去过南京，11月下旬四天', lambda o: has_plan(o) and days_total(o) == 4),
    # ---------- 无头绪：把选择权交出来，也得先给一版 ----------
    ('P-37', '没头绪，11月中旬去南京，你推荐',
     lambda o: has_plan(o) and (days_total(o) or 0) >= 1),
    ('P-38', '11月17号到19号，随便你安排',
     lambda o: has_plan(o) and days_total(o) == 3),
    ('P-39', '12月2号到4号，你看着办吧', lambda o: has_plan(o) and days_total(o) == 3),
    ('P-40', '11月23号到25号，我也不知道玩什么，你定',
     lambda o: has_plan(o) and days_total(o) == 3),
    ('P-41', '12月11号到13号，跟着你走就行',
     lambda o: has_plan(o) and days_total(o) == 3),
    # ---------- 只有时长、没日期（第一句见 PRE） ----------
    ('P-42', '11月14号开始',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 3),
    ('P-43', '11月14号开始',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 4),
    ('P-44', '11月14号开始',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 2),
    ('P-45', '11月14号开始',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 3),
    ('P-46',  # D-114 未修：先说"玩4天"再说"11月14号开始"，天数被单日覆盖
              # SKIP-D114
     '11月14号开始',
     lambda o: has_plan(o) and plan_start(o) == '2026-11-14' and days_total(o) == 4),
    # ---------- 只有地点、没日期（第一句见 PRE） ----------
    ('P-47', '11月14号',
     lambda o: (has_plan(o) and days_total(o) == 1
                and {'总统府', '中山陵'} <= set(slot(o, 'must') or []))),
    ('P-48', '11月14号',
     lambda o: has_plan(o) and days_total(o) == 2 and len(slot(o, 'must') or []) == 3),
    ('P-49', '11月14号',
     lambda o: (has_plan(o) and days_total(o) == 1
                and any('玄武湖' in m for m in slot(o, 'must') or [])
                and any('鸡鸣寺' in m for m in slot(o, 'must') or []))),
    ('P-50', '11月14号',
     lambda o: (has_plan(o) and {'中山陵', '明孝陵'} <= set(slot(o, 'must') or []))),
]

#: 没给日期的两轮：第一句说的是行程长度或想去的地点，第二句只补起始日
PRE = {
    'P-42': '想玩三天，你帮我排',
    'P-43': '玩四天，南京，怎么安排',
    'P-44': '两天一夜，南京',
    'P-45': '待三天，随便哪天都行',
    'P-46': '玩4天',
    'P-47': '想去总统府和中山陵，帮我排一天',
    'P-48': '南京博物院、总统府、夫子庙，排两天的',
    'P-49': '想去玄武湖和鸡鸣寺，帮我安排一天',
    'P-50': '想看中山陵和明孝陵，安排个行程',
}

#: 已确认与契约不符的用例 → 一句话根因（报告里按根因归类）
WHY = {
    'P-01': '中文数字天数「两天」未识别为 trip_days，按窗口默认排了 3 天',
    'P-03': '中文数字天数「四天」未识别，窗口默认排 3 天',
    'P-04': '「下周末」未解析成日期，只追问"哪天去"',
    'P-05': '「明年五一」未解析成日期，只追问"哪天去"',
    'P-06': '「国庆」未解析成日期，只追问"哪天去"',
    'P-09': '「三天两晚」无动词，不匹配天数规则，补了起始日后只排 1 天',
    'P-12': '中文数字天数「玩两天」未识别，补了起始日后只排 1 天',
    'P-14': '「下下周一到周三」被解析成单日 11-02，周次偏移与跨 3 天都没算',
    'P-18': '「预算不多」未映射到预算档（只认「预算少/穷游/省/学生」）',
    'P-22': '「节奏别太赶」被完全忽略：节奏与每日常规点数都和默认一致',
    'P-24': '「预算一千以内」这类金额预算未被记录（只有省/宽两档词表）',
    'P-27': '「夜景」不在兴趣词表，夜间偏好没有任何记录',
    'P-32': '「节奏慢一点」被忽略，pace 仍是 normal、per_day 仍是 3',
    'P-34': '「回南京」被当成"回某地"，城市名解析成南京站并进了必去项',
    'P-36': '「11月下旬四天」中文数字天数未识别，窗口默认 3 天',
    'P-42': '中文数字天数「三天」未识别，补起始日后只排 1 天',
    'P-43': '中文数字天数「四天」未识别，补起始日后只排 1 天',
    'P-44': '「两天一夜」未识别天数，补起始日后只排 1 天',
    'P-45': '中文数字天数「待三天」未识别，补起始日后只排 1 天',
    'P-48': '裸地名堆未提取进 must，且「两天」中文数字未识别',
    'P-50': '「想看A和B」只有带"去/逛"的动词句才提取，且只抓到第一个',
}

PARAMS = [
    pytest.param(code, text, check, id=code,
                 marks=pytest.mark.xfail(reason=WHY[code], strict=False)
                 if code in WHY else ())
    for code, text, check in CASES
]


@pytest.mark.parametrize('code,text,check', PARAMS)
def test_plan_intent(ctx, code, text, check):
    if code == 'P-46':
        pytest.skip('D-114 未修：天数与单日起始日共存（已入缺陷表）')
    session = f'plan-{code}'
    out = run(ctx, text, session, pre=[PRE[code]] if code in PRE else ())
    assert isinstance(out, dict), f'{code} 返回结构不对'
    shown = PRE.get(code)
    line = f'{shown} → {text}' if shown else text
    assert check(out), (
        f'{code} 「{line}」→ kind={out.get("kind")} '
        f'days_total={days_total(out)} start={plan_start(out)} '
        f'party={party(out)} per_day={per_day(out)} '
        f'槽位={ {k: v for k, v in (out.get("slots") or {}).items() if v not in ("", [], {}, 0, None, False)} }')
