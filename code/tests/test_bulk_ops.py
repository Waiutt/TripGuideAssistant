# -*- coding: utf-8 -*-
"""批量删除 / 撤销 / 批量改方式的测试（对应报告第 4 章 TC-22-*）。"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext

D = date(2026, 10, 28)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _seed_three(ctx):
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概1小时', D)
    ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    out = ctx.trip_svc.add_text('下午去鼓楼医院取报告，半小时', D)
    return [t.task_id for t in ctx.trip_svc.day_view(D).tasks]


# ---------------------------------------------------------------- TC-22-01
def test_delete_many_removes_selected(ctx):
    """TC-22-01 批量删除：一次删多项，返回可撤销的快照"""
    ids = _seed_three(ctx)
    assert len(ids) == 3
    outcome, snaps = ctx.trip_svc.delete_many(ids[:2])
    assert len(snaps) == 2
    left = [t.title for t in ctx.trip_svc.day_view(D).tasks]
    assert len(left) == 1
    assert outcome.result is not None


# ---------------------------------------------------------------- TC-22-02
def test_restore_after_delete(ctx):
    """TC-22-02 撤销删除：把快照恢复回当天"""
    ids = _seed_three(ctx)
    before = sorted(t.title for t in ctx.trip_svc.day_view(D).tasks)
    outcome, snaps = ctx.trip_svc.delete_many(ids)
    assert ctx.trip_svc.day_view(D).tasks == []
    restored = ctx.trip_svc.restore_many(snaps, D)
    after = sorted(t.title for t in ctx.trip_svc.day_view(D).tasks)
    assert after == before
    assert restored.result is not None


# ---------------------------------------------------------------- TC-22-03
def test_delete_many_with_empty_input(ctx):
    """TC-22-03 未选择任何项 → 明确提示，不产生副作用"""
    _seed_three(ctx)
    outcome, snaps = ctx.trip_svc.delete_many([])
    assert outcome.ok is False and snaps == []
    assert len(ctx.trip_svc.day_view(D).tasks) == 3


# ---------------------------------------------------------------- TC-22-04
def test_bulk_set_mode(ctx):
    """TC-22-04 批量改本段方式：所选段全部生效"""
    ids = _seed_three(ctx)
    outcome = ctx.trip_svc.set_mode_many(ids[1:], 'walking')
    assert outcome.result is not None
    tasks = {t.title: t for t in ctx.trip_svc.day_view(D).tasks}
    assert tasks['吃饭'].mode.value == 'walking'
    assert tasks['取报告'].mode.value == 'walking'
    # 未被选中的项不受影响
    assert tasks['看展'].mode is None


# ---------------------------------------------------------------- TC-22-05
def test_restore_keeps_original_mode_and_duration(ctx):
    """TC-22-05 撤销恢复后，时长与交通方式与原项一致"""
    ids = _seed_three(ctx)
    orig = {t.title: (t.duration_min, t.mode) for t in ctx.trip_svc.day_view(D).tasks}
    _, snaps = ctx.trip_svc.delete_many(ids)
    ctx.trip_svc.restore_many(snaps, D)
    now = {t.title: (t.duration_min, t.mode) for t in ctx.trip_svc.day_view(D).tasks}
    assert now == orig
