# -*- coding: utf-8 -*-
"""ScheduleEngine 等价类测试（对应报告第 4 章 TC-05-*）。"""
from datetime import time

import pytest

from nanjingtrip.engine import schedule
from nanjingtrip.models import (Poi, ScheduleOptions, Task, TaskStatus,
                                TransitMode)


def poi(name: str, pid: int) -> Poi:
    return Poi(name=name, district='玄武区', lng=118.8, lat=32.0,
               provider='mock', provider_poi_id=name, poi_id=pid)


def make_transit(table=None, default=20):
    table = table or {}

    def fn(a, b, mode):
        if a is None or b is None:
            return 0
        return table.get((a.name, b.name), default)
    return fn


def opts(**kw) -> ScheduleOptions:
    base = dict(mode=TransitMode.DRIVING, buffer_min=10,
                day_start=time(8, 0), day_end=time(22, 0))
    base.update(kw)
    return ScheduleOptions(**base)


# ---------------------------------------------------------------- TC-05-01
def test_empty_task_set():
    """TC-05-01 空任务集 → 空结果且可行"""
    result = schedule([], opts(), make_transit())
    assert result.items == []
    assert result.unscheduled == []
    assert result.is_feasible()


# ---------------------------------------------------------------- TC-05-02
def test_single_task_without_poi_starts_at_day_start():
    """TC-05-02 单任务且无地点 → 从可排时段起点开始，不插入通行段"""
    t = Task(title='取快递', duration_min=30)
    result = schedule([t], opts(), make_transit())
    item = result.ordered()[0]
    assert item.start_min == 8 * 60
    assert item.leg_before is None
    assert t.status == TaskStatus.SCHEDULED
    assert t.start_ts == time(8, 0) and t.end_ts == time(8, 30)


# ---------------------------------------------------------------- TC-05-03
def test_transit_is_inserted_between_tasks():
    """TC-05-03 两任务均有地点 → 相邻之间计入通行时长且不重叠（BR-02/BR-03）"""
    a = Task(title='看展', duration_min=120, poi=poi('南京博物院', 1), is_locked=True,
             start_ts=time(9, 0))
    b = Task(title='午餐', duration_min=60, poi=poi('新街口', 2))
    transit = make_transit({('南京博物院', '新街口'): 25})
    result = schedule([a, b], opts(), transit)
    a_item, b_item = result.ordered()
    assert b_item.leg_before is not None
    assert b_item.leg_before.duration_min == 25
    # 缓冲 10 + 通行 25 = 35 分钟后方可开始
    assert b_item.start_min >= a_item.end_min + 25 + 10
    assert not result.conflicts


# ---------------------------------------------------------------- TC-05-04
def test_desired_start_is_respected():
    """TC-05-04 用户指定期望开始时刻 → 在不破坏可行性的前提下满足"""
    t = Task(title='体检', duration_min=60, desired_start=time(14, 0))
    result = schedule([t], opts(), make_transit())
    assert result.ordered()[0].start_min == 14 * 60


# ---------------------------------------------------------------- TC-05-05
def test_locked_task_keeps_position():
    """TC-05-05 已锁定任务位置不变（BR-02 例外）"""
    a = Task(title='会议', duration_min=60, is_locked=True, start_ts=time(15, 0))
    b = Task(title='取快递', duration_min=30)
    result = schedule([a, b], opts(), make_transit())
    titles = [(i.task.title, i.start_min) for i in result.ordered()]
    assert ('会议', 15 * 60) in titles


# ---------------------------------------------------------------- TC-05-06
def test_no_slot_produces_conflict_and_unscheduled():
    """TC-05-06 可排时段被占满 → 任务进入 unscheduled 并产生冲突（BR-05）"""
    a = Task(title='全天事项', duration_min=14 * 60, is_locked=True, start_ts=time(8, 0))
    b = Task(title='插入项', duration_min=60)
    result = schedule([a, b], opts(), make_transit())
    assert result.unscheduled and result.unscheduled[0].title == '插入项'
    assert any(c.kind == 'no_slot' for c in result.conflicts)
    assert not result.is_feasible()


# ---------------------------------------------------------------- TC-05-07
def test_locked_overlap_detected():
    """TC-05-07 两个锁定项互相重叠 → locked_overlap 冲突"""
    a = Task(title='A', duration_min=90, is_locked=True, start_ts=time(9, 0))
    b = Task(title='B', duration_min=60, is_locked=True, start_ts=time(10, 0))
    result = schedule([a, b], opts(), make_transit())
    kinds = [c.kind for c in result.conflicts]
    assert 'locked_overlap' in kinds
    c = next(c for c in result.conflicts if c.kind == 'locked_overlap')
    assert c.overlap_min == 30


# ---------------------------------------------------------------- TC-05-08
def test_buffer_between_tasks_enforced():
    """TC-05-08 待排任务之间保留缓冲（BR-04）"""
    a = Task(title='A', duration_min=60, poi=poi('P1', 1), is_locked=True,
             start_ts=time(9, 0))
    b = Task(title='B', duration_min=60, poi=poi('P2', 2))
    c = Task(title='C', duration_min=60, poi=poi('P3', 3))
    result = schedule([a, b, c], opts(buffer_min=10), make_transit(default=20))
    items = result.ordered()
    for prev, cur in zip(items, items[1:]):
        if cur.leg_before:
            assert cur.start_min >= prev.end_min + cur.leg_before.duration_min + 10


# ---------------------------------------------------------------- TC-05-09
def test_out_of_range_locked_task():
    """TC-05-09 锁定项超出可排时段 → out_of_range 冲突"""
    a = Task(title='夜宵', duration_min=60, is_locked=True, start_ts=time(22, 30))
    result = schedule([a], opts(), make_transit())
    assert any(c.kind == 'out_of_range' for c in result.conflicts)


# ---------------------------------------------------------------- TC-05-10
def test_explanation_mentions_counts():
    """TC-05-10 排程说明包含项数与通行合计（可解释性）"""
    a = Task(title='看展', duration_min=60, poi=poi('A', 1), is_locked=True,
             start_ts=time(9, 0))
    b = Task(title='午餐', duration_min=60, poi=poi('B', 2))
    result = schedule([a, b], opts(), make_transit(default=15))
    assert '共排入 2 项' in result.explanation
    assert '通行合计 15 分钟' in result.explanation
