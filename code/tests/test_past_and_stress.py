# -*- coding: utf-8 -*-
"""P2-2：过去时间校验 + 大量节点不崩（D-45）。

两件事：
  1. 用户拿一个**已经过去的日期**来排程时，要明确提示（而不是静默当成"今天"）；
  2. 节点很多（20+）时，时间轴要完整、顺序不乱、时刻单调不减——
     这是"渲染不崩、顺序不乱"的后端等价验证。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.services import TripService  # noqa: F401  仅用于类型提示

TODAY = date.today()
PAST = TODAY - timedelta(days=3)
FUTURE = TODAY + timedelta(days=2)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def kinds(result):
    return {n.kind for n in result.notices}


# ------------------------------------------------------------------ 过去日期
def test_past_date_is_flagged(ctx):
    """排一个已经过去的日期，必须给出提示"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', PAST)
    result = ctx.trip_svc.compute_day(PAST)
    assert 'past_date' in kinds(result), f'没有过去日期提示：{kinds(result)}'
    detail = next(n.detail for n in result.notices if n.kind == 'past_date')
    assert '已经过去' in detail or '过去' in detail


def test_future_date_has_no_past_warning(ctx):
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', FUTURE)
    result = ctx.trip_svc.compute_day(FUTURE)
    assert 'past_date' not in kinds(result)


# ---------------------------------------------------------------- 大量节点
def test_many_items_stay_ordered_and_complete(ctx):
    """20 项：全部排入、时间单调不减、序号连续，不出现重复或丢失"""
    stops = ['南京博物院', '总统府', '夫子庙', '老门东', '中山陵',
             '明孝陵', '玄武湖公园', '鸡鸣寺', '新街口', '德基广场',
             '南京图书馆', '六朝博物馆', '朝天宫', '瞻园', '中华门瓮城',
             '雨花台', '阅江楼', '燕子矶公园', '紫峰大厦', '1912街区']
    items = [{'title': f'游览{i + 1}', 'poi': name, 'duration_min': 15}
             for i, name in enumerate(stops)]
    ok, msg = ctx.trip_svc.add_batch(FUTURE, items, keep_order=True)
    assert ok, msg

    result = ctx.trip_svc.compute_day(FUTURE)
    ordered = result.ordered()
    assert len(ordered) == 20, f'排入 {len(ordered)} 项，应为 20（未排入 {len(result.unscheduled)}）'
    starts = [i.start_min for i in ordered]
    assert starts == sorted(starts), '时间轴顺序乱了'
    ends = [i.end_min for i in ordered]
    assert ends == sorted(ends), '结束时间未随开始时间单调'
    assert [i.task.seq for i in ordered] == list(range(1, 21)), '序号不连续'
    titles = [i.task.title for i in ordered]
    assert len(set(titles)) == 20, '存在重复项'
