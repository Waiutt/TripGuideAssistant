# -*- coding: utf-8 -*-
"""午休块：只占时间、不指定地点（D-39）。

背景：用户说"12:00-13:00 必须留午饭时间"时，系统原先强制要求一个地点，
只能把午饭挂到某个 POI 上（如熙南里）。结果引擎按"最短路"把那 40 分钟的游览
排到了行程末尾，产生 15.8 公里的折返。

这里守住三件事：
  1. 没有地点的午休块可以被解析和写入；
  2. 它**不能打断通行链**（上一站 → 下一站的距离仍要算上）；
  3. 有了午休块之后，就不该再提示"午餐时段没有餐饮项"。
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


def test_meal_block_preview_without_place(ctx):
    """「中午12点吃饭，1小时」应解析成无地点的午休块，而不是报"缺少地点" """
    out = ctx.trip_svc.preview_text('中午12点吃饭，1小时', D)
    assert out.ok, out.error
    assert out.draft is not None
    assert out.draft.poi is None, '午休块不该被强行挂到某个 POI 上'
    assert out.draft.duration_min == 60


def test_meal_block_keeps_transit_chain(ctx):
    """午休块夹在中间时，后一段仍要带上"上一站→下一站"的通行"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ok, msg = ctx.trip_svc.add_batch(D, [{
        'title': '午饭', 'poi': '', 'duration_min': 60, 'category': '餐饮',
        'desired_start': '12:00', 'kind': 'block'}])
    assert ok, msg
    ctx.trip_svc.add_text('下午一点半去总统府参观，1小时', D)

    result = ctx.trip_svc.compute_day(D)
    items = result.ordered()
    meal = next(i for i in items if i.task.title == '午饭')
    assert meal.task.poi is None
    assert meal.start_min == 12 * 60, '午休块应锁在 12:00'
    after = [i for i in items if i.start_min >= meal.end_min]
    assert after, '午休块之后应还有安排'
    leg = after[0].leg_before
    assert leg is not None and leg.duration_min > 0, \
        '午休块把通行链打断了：后一段变成了"无通行"'


def test_meal_notice_satisfied_by_block(ctx):
    """已经有午休块时，不应再提示"午餐时段没有餐饮项" """
    ctx.trip_svc.add_text('上午十一点去南京博物院看展，大概2小时', D)
    before = {c.kind for c in ctx.trip_svc.compute_day(D).notices}
    assert 'meal_missing' in before, f'前提不成立：没报缺餐饮（{before}）'

    ok, msg = ctx.trip_svc.add_batch(D, [{
        'title': '午饭', 'poi': '', 'duration_min': 60, 'category': '餐饮',
        'desired_start': '12:00', 'kind': 'block'}])
    assert ok, msg
    after = {c.kind for c in ctx.trip_svc.compute_day(D).notices}
    assert 'meal_missing' not in after, f'已有午休块却仍提示缺餐饮：{after}'
