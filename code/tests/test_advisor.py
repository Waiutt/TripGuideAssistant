# -*- coding: utf-8 -*-
"""ConflictAdvisor 建议生成测试（对应报告第 4 章 TC-06-*）。"""
from datetime import time

from nanjingtrip.advisor import advise, attach_suggestions
from nanjingtrip.models import (Conflict, ScheduleOptions, ScheduleResult,
                                ScheduledItem, Task, TransitMode)


def opts() -> ScheduleOptions:
    return ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0))


def test_advise_no_slot_returns_actionable_suggestions():
    """TC-06-02 无可用空档 → 至少给出次日/压缩/换交通方式三类建议"""
    task = Task(title='体检', duration_min=120)
    result = ScheduleResult(unscheduled=[task])
    result.conflicts.append(Conflict(kind='no_slot', detail='无空档',
                                     task_title='体检'))
    sugg = advise(result, opts())
    kinds = {s.kind for s in sugg}
    assert {'next_day', 'shorten', 'mode'} <= kinds
    assert all(s.text for s in sugg)


def test_advise_overlap_suggests_shift_and_shrink():
    """TC-06-03 重叠冲突 → 给出顺延与缩短前项两类建议"""
    a = Task(title='看展', duration_min=120, start_ts=time(9, 0))
    b = Task(title='取报告', duration_min=30, start_ts=time(10, 0))
    result = ScheduleResult(items=[ScheduledItem(a, 9 * 60, 11 * 60),
                                   ScheduledItem(b, 10 * 60, 10 * 60 + 30)])
    result.conflicts.append(Conflict(kind='locked_overlap', detail='重叠 60 分钟',
                                     overlap_min=60, task_title='取报告',
                                     with_title='看展'))
    sugg = advise(result, opts())
    kinds = {s.kind for s in sugg}
    assert {'shift', 'shorten_prev', 'cancel'} <= kinds
    shift = next(s for s in sugg if s.kind == 'shift')
    assert '11:10' in shift.text        # 前项 11:00 结束 + 缓冲 10 分钟


def test_attach_suggestions_fills_conflicts():
    """TC-06-04 attach_suggestions 把建议挂回冲突对象供界面展示"""
    task = Task(title='体检', duration_min=120)
    result = ScheduleResult(unscheduled=[task])
    c1 = Conflict(kind='no_slot', detail='无空档', task_title='体检')
    result.conflicts.append(c1)
    attach_suggestions(result, opts())
    assert c1.suggestions
    assert any(s.patch.get('title') == '体检' or s.kind == 'mode'
               for s in c1.suggestions)


def test_advise_out_of_range():
    """TC-06-05 超出可排时段 → 建议延长时段"""
    result = ScheduleResult()
    result.conflicts.append(Conflict(kind='out_of_range', detail='超时段',
                                     task_title='夜宵'))
    sugg = advise(result, opts())
    assert any(s.kind == 'extend_window' for s in sugg)
