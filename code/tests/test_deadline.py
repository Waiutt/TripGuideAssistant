# -*- coding: utf-8 -*-
"""P0-4：绝对截止语义——"10:30 前必须抵达"（D-43）。

与"锁定开始时刻"不同：deadline 允许更早到，只禁止更晚。
赶不上时必须给 deadline_miss 冲突，而不是排到 11:20 还装作没事。
"""
from __future__ import annotations

from datetime import date, time

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.parsing_rules import parse_deadline

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


@pytest.mark.parametrize('text,expect', [
    ('我 10:30 前必须抵达鸡鸣寺，停留50分钟', (10, 30)),
    ('九点一刻之前到鸡鸣寺，停留50分钟', (9, 15)),
    ('十点半以前必须到达总统府', (10, 30)),
    ('上午九点去鸡鸣寺停留50分钟', None),          # 这是出发时刻，不是截止
    ('去鸡鸣寺，停留50分钟', None),
])
def test_parse_deadline(text, expect):
    """截止时刻的解析（区分"出发时刻"与"必须抵达时刻"）"""
    got = parse_deadline(text)
    if expect is None:
        assert got is None, f'{text} 不该解析出截止：{got}'
    else:
        assert got == expect, f'{text} 解析成 {got}，期望 {expect}'


def test_deadline_kept_on_draft(ctx):
    """解析出的截止要落到事项上，别只解析不保存"""
    out = ctx.trip_svc.preview_text('我 10:30 前必须抵达鸡鸣寺，停留50分钟', D)
    assert out.ok, out.error
    assert out.draft.deadline == time(10, 30)


def test_deadline_respected_when_feasible(ctx):
    """截止能满足时：安排到达不得晚于截止"""
    ctx.trip_svc.add_text('我 11:30 前必须抵达南京博物院，停留2小时', D)
    result = ctx.trip_svc.compute_day(D)
    item = next(i for i in result.ordered() if '南京博物院' in i.task.title
                or (i.task.poi and i.task.poi.name == '南京博物院'))
    assert item.start_min <= 11 * 60 + 30, f'实际 {item.start_ts} 晚于截止'


def test_deadline_miss_is_conflict(ctx):
    """截止根本赶不上时：给 deadline_miss 冲突，并说明原因"""
    # 09:00-11:00 已被锁死，另一项却要求 10:05 前抵达 → 无论如何排不进去
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ctx.trip_svc.add_text('我 10:05 前必须抵达总统府，停留1小时', D)
    result = ctx.trip_svc.compute_day(D)
    kinds = {c.kind for c in result.conflicts}
    assert 'deadline_miss' in kinds, f'没有截止冲突告警：{kinds}'
    detail = next(c.detail for c in result.conflicts if c.kind == 'deadline_miss')
    assert '前抵达' in detail
