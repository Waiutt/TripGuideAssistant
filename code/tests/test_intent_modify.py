# -*- coding: utf-8 -*-
"""修改类意图矩阵（50 轮）：这句话到底是不是在"改已有安排"，改对了没有。

为什么每条用例都先建一份行程
----------------------------
"改"只有在**已经有一份安排**时才谈得上。没有底稿时说"多加一天"，系统给的是
"新建一份行程"，测到的是别的东西——所以每条用例先用 `BASE` 把
11-14~11-16、三点（南京博物院/总统府/夫子庙）的底稿铺好，再发待测的那一句，
`NEEDS_BASE` 的做法与 `test_intent_matrix.py` 保持一致。

判定口径（只看可观察结果，不看它自称理解了什么）
------------------------------------------------
* 槽位：`trip_days` / `per_day` / `wake` / `day_end` / `taste` / `budget` /
  `hotel` / `meal` 有没有被写成用户说的那个值；
* 草案：窗口型会话（"11 月中旬"）里说"多加一天"，`plan.days_total` 必须真的变大
  （窗口宽度 10 天，不等于行程天数）；
* 离线、`tmp_path`，不碰真实库。

失败一律 `xfail(strict=False)` 并写明根因，**不把现状写成断言**。
判定细节见 `code/docs/意图测试结果_修改删除.md`。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)

#: 底稿：三天，三个点名必去的点，每天两个点
BASE = '11月14号到16号去南京玩，想去南京博物院、总统府和夫子庙，每天两个点'
#: 窗口型底稿：只给了一段窗口，没给行程长度
WINDOW_BASE = '11月中旬去南京玩'


@pytest.fixture
def ctx(tmp_path):
    """每个用例一个临时库：改过的槽位/写过的行程不串到下一条用例"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def slot(out, key, default=None):
    return (out.get('slots') or {}).get(key, default)


def days_total(out):
    return (out.get('plan') or {}).get('days_total')


def plan_pois(out):
    """草案里排进去的地名（只取真正的点位，跳过"在X附近吃午饭"这类占位块）"""
    plan = out.get('plan') or {}
    return [str(i.get('poi') or '') for d in plan.get('days') or []
            for i in d.get('items') or [] if i.get('poi')]


def trip_days(out):
    try:
        return int(slot(out, 'trip_days') or 0)
    except (TypeError, ValueError):
        return 0


def brief(out):
    """失败信息里要看到"到底写成了什么" """
    keep = {k: v for k, v in (out.get('slots') or {}).items()
            if v not in ('', [], {}, 0, None, False)
            and k in ('start', 'end', 'trip_days', 'per_day', 'wake', 'day_end',
                      'taste', 'budget', 'hotel', 'meal', 'pace', 'avoid',
                      'must', 'interests')}
    return f'kind={out.get("kind")} 槽位={keep} days_total={days_total(out)}'


# ---------------------------------------------------------------- 有底稿的修改
CASES = [
    # ---------- 改天数：加天 / 减天 / 改成几天 ----------
    ('M-01', '多加一天', lambda o: days_total(o) == 4),
    ('M-02', '再加一天', lambda o: days_total(o) == 4),
    ('M-03', '延一天', lambda o: days_total(o) == 4),
    ('M-04', '少一天', lambda o: days_total(o) == 2),
    ('M-05', '缩短一天', lambda o: days_total(o) == 2),
    ('M-06', '改成五天', lambda o: trip_days(o) == 5 and days_total(o) == 5),
    ('M-07', '改到4天', lambda o: trip_days(o) == 4),
    ('M-08', '变成两天', lambda o: trip_days(o) == 2 and days_total(o) == 2),
    ('M-09', '总共3天', lambda o: trip_days(o) == 3),
    ('M-10', '玩5天', lambda o: trip_days(o) == 5),
    # ---------- 改每日点数 ----------
    ('M-11', '每天别超过两个点', lambda o: slot(o, 'per_day') == 2),
    ('M-12', '每天最多4个景点', lambda o: slot(o, 'per_day') == 4),
    ('M-13', '每日2个点', lambda o: slot(o, 'per_day') == 2),
    ('M-14', '少排一点', lambda o: 1 <= (slot(o, 'per_day') or 9) < 3),
    ('M-15', '太满了', lambda o: 1 <= (slot(o, 'per_day') or 9) < 3),
    # ---------- 改节奏与时刻 ----------
    ('M-16', '10点起床', lambda o: slot(o, 'wake') == '10:00'),
    ('M-17', '我十点起床', lambda o: slot(o, 'wake') == '10:00'),
    ('M-18', '12点起床', lambda o: slot(o, 'wake') == '12:00'),
    ('M-19', '23点后回酒店', lambda o: slot(o, 'day_end') == '23:00'),
    ('M-20', '晚上十一点以后回酒店', lambda o: slot(o, 'day_end') == '23:00'),
    ('M-21', '晚上10点回酒店', lambda o: slot(o, 'day_end') == '22:00'),
    ('M-22', '别太赶', lambda o: slot(o, 'pace') == 'slow'
        or slot(o, 'per_day') == 2),
    ('M-23', '我比较懒，别太赶', lambda o: slot(o, 'pace') == 'slow'),
    ('M-24', '特种兵一点', lambda o: slot(o, 'pace') == 'fast'),
    # ---------- 改口味与预算 ----------
    ('M-25', '我不吃辣', lambda o: slot(o, 'taste') == '不吃辣'),
    ('M-26', '清真', lambda o: slot(o, 'taste') == '清真'),
    ('M-27', '吃素', lambda o: slot(o, 'taste') == '素'),
    ('M-28', '预算不多', lambda o: slot(o, 'budget') == '省'),
    ('M-29', '预算有限', lambda o: slot(o, 'budget') == '省'),
    ('M-30', '省钱', lambda o: slot(o, 'budget') == '省'),
    ('M-31', '不差钱', lambda o: slot(o, 'budget') == '宽'),
    # ---------- 改住宿 ----------
    ('M-32', '住金陵饭店', lambda o: slot(o, 'hotel') == '金陵饭店'),
    ('M-33', '住宿你帮我选', lambda o: slot(o, 'hotel') == 'recommend'),
    ('M-34', '帮我选个酒店', lambda o: slot(o, 'hotel') == 'recommend'),
    ('M-35', '不用酒店', lambda o: slot(o, 'hotel') in ('none', 'day_trip')),
    ('M-36', '不需要酒店', lambda o: slot(o, 'hotel') in ('none', 'day_trip')),
    # ---------- 改餐饮 ----------
    ('M-37', '午饭你帮我选', lambda o: slot(o, 'meal') == 'recommend'),
    ('M-38', '午饭就近随便', lambda o: slot(o, 'meal') == 'recommend'),
    ('M-39', '中午在德基广场吃饭', lambda o: slot(o, 'meal') == '德基广场'),
    ('M-40', '中午去南京大牌档德基店吃饭',
     lambda o: '南京大牌档' in str(slot(o, 'meal') or '')),
    ('M-41', '午饭在德基广场解决', lambda o: slot(o, 'meal') == '德基广场'),
    # ---------- 改交通（这一段换成步行/骑车） ----------
    ('M-42', '这段改步行', lambda o: 'walk' in str(slot(o, 'mode') or '')
        or '步行' in str(slot(o, 'mode') or '')),
    ('M-43', '这段改骑车', lambda o: 'bike' in str(slot(o, 'mode') or '')
        or '骑行' in str(slot(o, 'mode') or '')
        or '骑车' in str(slot(o, 'mode') or '')),
    # ---------- 换一版 ----------
    ('M-44', '再来一版', lambda o: bool((o.get('plan') or {}).get('variant')
                                       or (o.get('plan') or {}).get('rev')
                                       or '换' in str(o.get('message') or ''))),
    ('M-45', '多点博物馆', lambda o: '博物馆' in (slot(o, 'interests') or [])),
    ('M-46', '再来一版，多点博物馆',
     lambda o: '博物馆' in (slot(o, 'interests') or [])),
]

#: 已知未通过：句子 → 根因（报告里逐条对得上）
FAILING = {
    'M-06': '中文数字"五"没有进天数正则，只有阿拉伯数字能改天数（改成五天后 trip_days 仍为 0，草案仍 3 天）',
    'M-08': '同上：中文数字"两"改不动天数（变成两天后草案仍 3 天）',
    'M-11': '中文数字"两"没有进每日点数正则（每天别超过两个点后 per_day 仍为 0）',
    'M-17': '起床时刻正则只吃阿拉伯数字，"十点起床"没写入 wake（仍 09:00）',
    'M-20': '同上：可串数字解析缺失，"晚上十一点以后回酒店"没写入 day_end',
    'M-21': '"晚上10点"被当成上午 10 点：day_end 写成 10:00，而不是 22:00',
    'M-22': '"别太赶"不在节奏词表里（只认 太满/太多/累/少一点/少排），pace 与 per_day 都不动',
    'M-28': '预算词表只认 省/穷游/预算少/学生，"预算不多"没命中，budget 为空',
    'M-29': '同上："预算有限"没命中，budget 为空',
    'M-35': '住宿槽位没有"不用酒店/当天往返"这个值：hotel 保持空串，与"还没问过"无法区分，追问会一直问下去',
    'M-36': '同上："不需要酒店"同样无处可写',
    'M-41': '点名吃饭地点的正则只认 吃饭/用餐/聚餐，"在德基广场解决"没命中，meal 为空',
    'M-42': '顾问层没有"某一段交通方式"槽位（FIELDS 里没有 mode），这句话在 consult 里没有任何落点',
    'M-43': '同上：骑行同样没有落点（能力只在 AssistantService.route 那一侧）',
    'M-44': '"再来一版"没有任何可观察结果：草案与上一轮逐字相同，也没有版本/变体标记',
}

#: 窗口型会话：只给"11 月中旬"这段窗口，行程长度是系统暂定的
WINDOW_CASES = [
    ('M-47', '多加一天', lambda before, after, o: after > before),
    ('M-48', '少一天', lambda before, after, o: 1 <= after < before),
    ('M-49', '改成五天', lambda before, after, o: after == 5),
    ('M-50', '每天别超过两个点', lambda before, after, o: slot(o, 'per_day') == 2),
]

WINDOW_FAILING = {
    'M-47': '窗口会话下加天没有生效：st.start/st.end 已被上一轮草案覆写成"已排的 3 天"，'
            '加天用的上限 min(base+1, date_span) 取的是这 3 天而不是窗口宽度 '
            'win_start~win_end(10 天)，trip_days 被夹回 3，days_total 不变',
    'M-49': '窗口会话 + 中文数字：天数没写入（trip_days 为空），窗口仍按 3 天排；'
            '即便写成"改成5天"也只是天数生效，窗口上限仍是旧跨度',
    'M-50': '窗口会话下每日点数没生效：中文数字"两"未进正则，per_day 仍为 0（按默认 3 排）',
}

PARAMS = [
    pytest.param(code, text, check, id=code,
                 marks=pytest.mark.xfail(reason=FAILING[code], strict=False)
                 if code in FAILING else ())
    for code, text, check in CASES
]
WINDOW_PARAMS = [
    pytest.param(code, text, check, id=code,
                 marks=pytest.mark.xfail(reason=WINDOW_FAILING[code], strict=False)
                 if code in WINDOW_FAILING else ())
    for code, text, check in WINDOW_CASES
]


@pytest.mark.parametrize('code,text,check', PARAMS)
def test_modify_intent(ctx, code, text, check):
    """先有底稿，再说要改什么：槽位/草案必须跟着变"""
    session = f'intent-mod-{code}'
    run(ctx, BASE, session)
    out = run(ctx, text, session)
    assert isinstance(out, dict), f'{code} 返回结构不对'
    assert check(out), f'{code} 「{text}」→ {brief(out)}'


@pytest.mark.parametrize('code,text,check', WINDOW_PARAMS)
def test_modify_intent_in_window(ctx, code, text, check):
    """窗口型会话："11月中旬"只是窗口，加天加的是行程长度，草案天数必须真的变"""
    session = f'intent-modw-{code}'
    first = run(ctx, WINDOW_BASE, session)
    before = days_total(first)
    out = run(ctx, text, session)
    after = days_total(out)
    assert isinstance(out, dict), f'{code} 返回结构不对'
    assert check(before, after, out), (
        f'{code} 「{text}」（窗口）→ days_total {before} → {after}；{brief(out)}')
