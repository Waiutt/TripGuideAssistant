# -*- coding: utf-8 -*-
"""预算接入（D-53）：档案预算 → 餐饮价位校验与平价替代建议。

规则（只提示不改数据）：
  * 当天餐饮项人均超出预算 → 给 `budget_over` 提示，并列出更便宜的候选；
  * 有午休块（无地点）又设了预算 → 给 `budget_meal` 提示，列 2–3 家平价候选；
  * 预算「不设」→ 两条提示都不出现（不打扰）；
  * **绝不自动删除或替换**用户选的点位。
"""
from __future__ import annotations

from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import Poi
from nanjingtrip.profile import Profile

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


# ------------------------------------------------------------------ 预算映射
@pytest.mark.parametrize('budget,max_price', [
    ('省', 60), ('中', 150), ('不设', None), ('外星预算', None),
])
def test_budget_maps_to_price_cap(budget, max_price):
    assert Profile(name='t', budget=budget).max_price == max_price


@pytest.mark.parametrize('raw,value', [
    ('人均约 ¥90', 90), ('人均约 ¥300', 300), ('¥25', 25), ('', None), ('面议', None),
])
def test_price_parsing(raw, value):
    assert Poi(name='x', price=raw).price_num == value


# ------------------------------------------------------------------ 超预算提示
def kinds(result):
    return {n.kind for n in result.notices}


def test_over_budget_is_flagged(ctx):
    """选了一家 300/人的淮扬菜，预算「省」→ 提示并给平价替代"""
    ctx.apply_profile(Profile(name='省钱档', budget='省'))
    ctx.trip_svc.add_text('中午去金陵饭店梅苑餐厅吃饭，1小时', D)
    result = ctx.trip_svc.compute_day(D)
    assert 'budget_over' in kinds(result), f'没提示超预算：{kinds(result)}'
    detail = next(n.detail for n in result.notices if n.kind == 'budget_over')
    assert '预算' in detail and '¥' in detail


def test_within_budget_is_quiet(ctx):
    ctx.apply_profile(Profile(name='省钱档', budget='省'))
    ctx.trip_svc.add_text('中午去鸡鸣汤包湖南路店吃饭，1小时', D)
    result = ctx.trip_svc.compute_day(D)
    assert 'budget_over' not in kinds(result), kinds(result)


def test_no_budget_means_no_noise(ctx):
    ctx.apply_profile(Profile(name='默认'))
    ctx.trip_svc.add_text('中午去金陵饭店梅苑餐厅吃饭，1小时', D)
    result = ctx.trip_svc.compute_day(D)
    assert 'budget_over' not in kinds(result)


def test_budget_over_suggests_cheaper_name(ctx):
    """替代建议要给出**具体的、更便宜的**店名，而不是空话"""
    ctx.apply_profile(Profile(name='省钱档', budget='省'))
    ctx.trip_svc.add_text('中午去金陵饭店梅苑餐厅吃饭，1小时', D)
    result = ctx.trip_svc.compute_day(D)
    sug = next(n.suggestions for n in result.notices if n.kind == 'budget_over')
    assert sug and any('¥' in s.text for s in sug), [s.text for s in sug]


# ------------------------------------------------------- 午休块 + 预算建议
def test_meal_block_suggests_affordable(ctx):
    ctx.apply_profile(Profile(name='省钱档', budget='省'))
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ctx.trip_svc.add_batch(D, [{'title': '午饭', 'poi': '', 'duration_min': 60,
                                'category': '餐饮', 'desired_start': '12:00',
                                'kind': 'block'}])
    result = ctx.trip_svc.compute_day(D)
    assert 'budget_meal' in kinds(result), f'午休块没给平价建议：{kinds(result)}'
    detail = next(n.detail for n in result.notices if n.kind == 'budget_meal')
    assert '¥' in detail


def test_budget_never_deletes_tasks(ctx):
    """预算只提示，不许动用户选的点位"""
    ctx.apply_profile(Profile(name='省钱档', budget='省'))
    ctx.trip_svc.add_text('中午去金陵饭店梅苑餐厅吃饭，1小时', D)
    before = [t.title for t in ctx.trip_svc.day_view(D).tasks]
    ctx.trip_svc.compute_day(D)
    after = [t.title for t in ctx.trip_svc.day_view(D).tasks]
    assert before == after, f'预算改动了数据：{before} → {after}'
