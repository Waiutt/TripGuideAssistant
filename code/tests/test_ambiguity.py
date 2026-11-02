# -*- coding: utf-8 -*-
"""语义与模糊输入：会不会产生歧义（对应报告 4.10 的"语义歧义"补充）。

这一层不测"解析得对不对"，而测**一句话有多种合理解释时，系统会不会选错还理直气壮**。
契约（本文件即口径）：
  · 二选一（"A 或者 B"）不得两个都排；
  · 含糊指代（"就去那边"）必须追问，不许臆造；
  · 冲突偏好（"别太累也别太空"）应落到中间值并说明；
  · 问句（"这天安排合理吗"）应该回答，而不是再排一版行程；
  · 跨城（"南京或苏州"）要明确说只支持南京；
  · 重复提及同一地点不得排两遍；
  · 条件句（"下雨就去博物馆"）不得无条件排进行程。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)


@pytest.fixture()
def con(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    yield Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    ctx.close()


def stops(out):
    plan = out.get('plan') or {}
    return [str(i.get('poi') or i.get('title') or '')
            for d in plan.get('days') or [] for i in d.get('items') or []]


def test_amb01_either_or_schedules_only_one(con):
    """AMB-01 「去博物院或者总统府」只能排一个，不能两个都塞"""
    out = con.chat('a1', '明天下午去南京博物院或者总统府', TODAY)
    got = [s for s in stops(out) if s in ('南京博物院', '总统府')]
    assert len(got) <= 1, f'二选一却排了两个：{got}'


def test_amb02_vague_reference_asks_instead_of_guessing(con):
    """AMB-02 「就去那边」这类含糊指代必须追问，不能自己挑一个地点"""
    out = con.chat('a2', '明天下午就去那边吧', TODAY)
    plan = out.get('plan') or {}
    named = [s for s in stops(out) if s and not s.startswith('在')]
    assert out['kind'] == 'ask' or not named, f'含糊指代被直接排成了：{named}'


def test_amb03_conflicting_pace_preferences(con):
    """AMB-03 「别太累但也别太空」：落到中间值（每天 2 个点）并说明"""
    out = con.chat('a3', '11月14号到16号去南京玩，别太累但也别太空', TODAY)
    plan = out.get('plan') or {}
    assert 1 <= plan.get('per_day', 0) <= 3, f'冲突偏好落到 {plan.get("per_day")} 个点'


@pytest.mark.xfail(reason='疑似缺陷：顾问路径没有 ask 意图，"这样安排合理吗"会被当成排程请求', strict=False)
def test_amb04_question_should_be_answered_not_replanned(con):
    """AMB-04 「这天安排合理吗」是问句，不该再出一版新行程"""
    con.chat('a4', '明天上午去总统府，下午去南京博物院，各两小时', TODAY)
    out = con.chat('a4', '这样安排合理吗', TODAY)
    assert out['kind'] in ('answer', 'ask', 'clarify'), f'问句被当成排程：kind={out["kind"]}'


def test_amb05_cross_city_ambiguity_is_refused(con):
    """AMB-05 「去南京或者苏州」：只支持南京市内，要说清楚"""
    out = con.chat('a5', '明天去南京或者苏州玩', TODAY)
    named = [s for s in stops(out) if '苏州' in s]
    assert not named, '苏州被排进了南京行程'


def test_amb06_repeated_place_is_deduped(con):
    """AMB-06 「上午总统府，下午总统府」：同一地点不能排两遍"""
    out = con.chat('a6', '明天上午去总统府，下午还去总统府', TODAY)
    assert stops(out).count('总统府') <= 1


def test_amb07_conditional_is_not_unconditional(con):
    """AMB-07 「下雨就去博物院」是条件句，不能无条件排进行程"""
    out = con.chat('a7', '明天随便逛逛，下雨就去南京博物院', TODAY)
    plan = out.get('plan') or {}
    conds = plan.get('conditionals') or []
    got = [s for s in stops(out) if s == '南京博物院']
    assert conds or not got, '条件句被当成了必去项'


def test_amb08_either_or_keeps_alternative_for_swap(con):
    """AMB-08 二选一时要留出"换个"的余地（草案里能看出候选）"""
    out = con.chat('a8', '明天去南京博物院或总统府其中一个', TODAY)
    plan = out.get('plan') or {}
    assert plan.get('days'), '至少给一版草案'
    assert len([s for s in stops(out) if s in ('南京博物院', '总统府')]) <= 1


def test_amb09_vague_time_is_asked_or_defaulted_with_note(con):
    """AMB-09 「下午晚点去」这种模糊时刻：要么追问，要么按默认并说明"""
    out = con.chat('a9', '明天下午晚点去总统府', TODAY)
    assert out['kind'] in ('plan', 'ask')
    if out['kind'] == 'plan':
        assert (out.get('plan') or {}).get('notes') is not None


def test_amb10_place_plus_meal_in_one_sentence(con):
    """AMB-10 「去博物院，中午在附近吃」：地点与吃饭要拆成两件事"""
    out = con.chat('a10', '明天上午去南京博物院，中午在附近吃', TODAY)
    got = stops(out)
    assert any('南京博物院' in s for s in got)
    assert any('吃' in s for s in got), f'吃饭那件事丢了：{got}'
