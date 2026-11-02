# -*- coding: utf-8 -*-
"""P2-3：节假日（拥堵加成 + 人流/预约预警）（D-46）。

诚实边界（重要）：
  * 只收录**公历固定**的法定节假日（元旦、劳动节、国庆节）与周末；
  * 春节/清明/端午/中秋按**农历**，需要官方放假通知，本项目**不臆造**——
    接口留出配置入口，未配置就明说"未收录"。
"""
from __future__ import annotations

from datetime import date

import pytest

from nanjingtrip.holidays import holiday_of, is_rest_day


@pytest.mark.parametrize('day,expect', [
    (date(2026, 1, 1), '元旦'),
    (date(2026, 5, 1), '劳动节'),
    (date(2026, 10, 1), '国庆节'),
    (date(2026, 10, 7), '国庆节'),
    (date(2026, 10, 8), ''),          # 假期结束
    (date(2026, 11, 18), ''),         # 普通工作日
])
def test_holiday_of_fixed_dates(day, expect):
    assert holiday_of(day) == expect


def test_weekend_counts_as_rest_day():
    assert is_rest_day(date(2026, 11, 21)) is True     # 周六
    assert is_rest_day(date(2026, 11, 22)) is True     # 周日
    assert is_rest_day(date(2026, 11, 18)) is False    # 周三
    assert is_rest_day(date(2026, 10, 1)) is True      # 节假日也算


def test_unknown_lunar_holiday_not_fabricated():
    """未收录的农历节日必须返回 None/空，不能编一个名字出来"""
    from nanjingtrip.holidays import lunar_holidays_note
    assert holiday_of(date(2026, 2, 17)) == ''      # 春节（未收录）
    assert '未收录' in lunar_holidays_note()


# ------------------------------------------------------- 节假日：拥堵与预警
def test_holiday_raises_driving_estimate(tmp_path):
    """节假日驾车耗时要在同样时刻上更慢（拥堵加成），步行不受影响"""
    from nanjingtrip.traffic import TrafficModel
    normal = TrafficModel()
    busy = TrafficModel(holiday=True)
    minute = 11 * 60            # 平峰时段：普通日不加成，节假日要加成
    assert normal.factor(minute, __import__('nanjingtrip.models', fromlist=['TransitMode']).TransitMode.DRIVING) == pytest.approx(1.0)
    assert busy.factor(minute, __import__('nanjingtrip.models', fromlist=['TransitMode']).TransitMode.DRIVING) > 1.0


def test_holiday_notice_reported(tmp_path):
    """节假日排程要给出人流/预约预警"""
    from nanjingtrip.app import AppContext
    c = AppContext(tmp_path)
    try:
        c.settings.offline_mode = True
        c.apply_settings(c.settings)
        d = date(2026, 10, 2)                 # 国庆假期内
        c.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', d)
        result = c.trip_svc.compute_day(d)
        kinds = {n.kind for n in result.notices}
        assert 'holiday' in kinds, f'缺少节假日提示：{kinds}'
        detail = next(n.detail for n in result.notices if n.kind == 'holiday')
        assert '国庆' in detail
    finally:
        c.close()
