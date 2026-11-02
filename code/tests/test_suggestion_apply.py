# -*- coding: utf-8 -*-
"""冲突建议"一键采纳"的测试（对应报告第 4 章 TC-20-*）。"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import Suggestion

D = date(2026, 10, 28)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _make_conflict(ctx, date_=D):
    """造一个"锁定项重叠"的冲突，返回 (outcome, conflict)"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', date_)
    out = ctx.trip_svc.add_text('上午十点去鼓楼医院取报告，30分钟', date_)
    conflict = next(c for c in out.result.conflicts
                    if c.kind == 'locked_overlap')
    return out, conflict


# ---------------------------------------------------------------- TC-20-01
def test_apply_shift_suggestion(ctx):
    """TC-20-01 采纳"顺延"建议 → 该项开始时间被改到建议时刻"""
    out, conflict = _make_conflict(ctx)
    shift = next(s for s in conflict.suggestions if s.kind == 'shift')
    res = ctx.trip_svc.apply_suggestion(shift, D)
    assert res.ok, res.error
    target = shift.patch['title']
    task = next(t for t in ctx.trip_svc.day_view(D).tasks if t.title == target)
    expect = shift.patch['start_ts']
    assert task.start_ts.strftime('%H:%M') == expect


# ---------------------------------------------------------------- TC-20-02
def test_apply_shorten_suggestion(ctx):
    """TC-20-02 采纳"缩短耗时"建议 → 该项时长被缩短"""
    out, conflict = _make_conflict(ctx)
    sugg = next(s for s in conflict.suggestions if s.kind == 'shorten_prev')
    res = ctx.trip_svc.apply_suggestion(sugg, D)
    assert res.ok, res.error
    task = next(t for t in ctx.trip_svc.day_view(D).tasks
                if t.title == sugg.patch['title'])
    assert task.duration_min == sugg.patch['duration_min']


# ---------------------------------------------------------------- TC-20-03
def test_apply_next_day_suggestion(ctx):
    """TC-20-03 采纳"移到次日" → 该项从当天消失、出现在次日"""
    out, conflict = _make_conflict(ctx)
    res = ctx.trip_svc.apply_suggestion(
        Suggestion(kind='next_day', text='移到次日',
                   patch={'move_next_day': True, 'title': '取报告'}), D)
    assert res.ok, res.error
    today_titles = [t.title for t in ctx.trip_svc.day_view(D).tasks]
    next_titles = [t.title for t in
                   ctx.trip_svc.day_view(date(2026, 10, 29)).tasks]
    assert '取报告' not in today_titles
    assert '取报告' in next_titles


# ---------------------------------------------------------------- TC-20-04
def test_apply_delete_suggestion(ctx):
    """TC-20-04 采纳"取消该安排" → 该项被删除"""
    out, conflict = _make_conflict(ctx)
    res = ctx.trip_svc.apply_suggestion(
        Suggestion(kind='cancel', text='取消', patch={'delete': '取报告'}), D)
    assert res.ok
    assert '取报告' not in [t.title for t in ctx.trip_svc.day_view(D).tasks]


# ---------------------------------------------------------------- TC-20-05
def test_apply_mode_suggestion(ctx):
    """TC-20-05 采纳"换交通方式" → 该段方式被修改（未解决的重叠冲突仍保留）"""
    out, conflict = _make_conflict(ctx)
    res = ctx.trip_svc.apply_suggestion(
        Suggestion(kind='mode', text='改步行', patch={'mode': 'walking',
                                                     'title': '取报告'}), D)
    # 改方式不会解决"锁定项重叠"，因此整体仍可能不可行；关键是改动确实生效
    assert res.result is not None
    task = next(t for t in ctx.trip_svc.day_view(D).tasks if t.title == '取报告')
    assert task.mode is not None and task.mode.value == 'walking'


# ---------------------------------------------------------------- TC-20-06
def test_apply_unknown_patch_is_rejected(ctx):
    """TC-20-06 无法执行的 patch → 明确拒绝，不静默无操作"""
    _make_conflict(ctx)
    res = ctx.trip_svc.apply_suggestion(
        Suggestion(kind='unknown', text='x', patch={'foo': 1}), D)
    assert res.ok is False and '不支持' in res.error
