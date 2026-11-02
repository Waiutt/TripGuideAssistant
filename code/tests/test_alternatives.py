# -*- coding: utf-8 -*-
"""P2-1：冲突时给"并列备选方案"，而不是一句抽象建议（D-44）。

要求：
  * 每个备选都要**真跑一次模拟**再报结果（末项几点结束、还剩几项没排进去），
    不允许只是文案；
  * 至少给出三类：少去一个点 / 缩短停留 / 放宽返回截止（换日期视情况）；
  * 行程本来就排得下时，不要制造噪音（返回空）。
"""
from __future__ import annotations

from datetime import date

import pytest

from nanjingtrip.app import AppContext

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def make_crowded(ctx):
    """塞不下的一天：3 个各 120 分钟的点 + 8:00-14:00 的窗口（少去一个就排得下）"""
    ctx.settings.day_start = '08:00'
    ctx.settings.day_end = '14:00'
    ctx.apply_settings(ctx.settings)
    ctx.trip_svc.set_lodging(D, '南京邮电大学三牌楼校区')
    ctx.trip_svc.add_batch(D, [
        {'title': '游览中山陵', 'poi': '中山陵', 'duration_min': 120},
        {'title': '游览牛首山文化旅游区', 'poi': '牛首山', 'duration_min': 120},
        {'title': '游览雨花台', 'poi': '雨花台', 'duration_min': 120},
    ], keep_order=True)


def test_no_alternatives_when_plan_is_fine(ctx):
    """排得下就不该列备选（避免噪音）"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    assert ctx.trip_svc.alternatives(D) == []


def test_alternatives_offered_when_cannot_fit(ctx):
    make_crowded(ctx)
    base = ctx.trip_svc.compute_day(D)
    assert base.unscheduled or base.conflicts, '前提：这一天应该排不下'
    alts = ctx.trip_svc.alternatives(D)
    assert len(alts) >= 2, f'应给出并列备选，实际 {alts}'
    kinds = {a['kind'] for a in alts}
    assert {'drop', 'shorten'} <= kinds, f'至少要有"少去一个点"和"缩短停留"：{kinds}'
    for a in alts:
        assert a.get('label') and a.get('effect'), f'备选缺说明：{a}'
        assert 'feasible' in a and 'end' in a


def test_drop_alternative_is_actually_better(ctx):
    """“少去一个点”必须真的更可行：未排入项减少或末项更早"""
    make_crowded(ctx)
    base = ctx.trip_svc.compute_day(D)
    base_unscheduled = len(base.unscheduled)
    drops = [a for a in ctx.trip_svc.alternatives(D) if a['kind'] == 'drop']
    assert drops, '应至少给出一个"少去一个点"的备选'
    best = min(drops, key=lambda a: a.get('unscheduled', 99))
    assert best.get('unscheduled', 99) < base_unscheduled or best['feasible'], \
        f'少去一个点并没有变好：base 未排入 {base_unscheduled}，备选 {best}'


def test_return_by_alternative_reported(ctx):
    """放宽返回截止也要作为备选列出（带模拟后的回程时刻）"""
    make_crowded(ctx)
    # 这一天实际回程在 11:18 左右，截止卡到 10:00 才需要"放宽"
    ctx.settings.return_by = '10:00'
    ctx.apply_settings(ctx.settings)
    kinds = {a['kind'] for a in ctx.trip_svc.alternatives(D)}
    assert 'relax_return' in kinds, f'缺少放宽返回截止的备选：{kinds}'
