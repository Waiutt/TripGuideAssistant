# -*- coding: utf-8 -*-
"""P1-1：早晚高峰系数要进入**排程时间轴**，而不只是"出行建议"里。

现状问题：`traffic.py` 已有 ×1.35（驾车）/×1.20（公交）的时段系数，
但只被出行建议用到；时间轴上的通行时长仍是原始高德耗时。
于是"08:30 出发"和"11:00 出发"在同一段路上被算成一模一样的时间。
"""
from __future__ import annotations

from datetime import time

import pytest

from nanjingtrip.engine import schedule
from nanjingtrip.models import (Poi, ScheduleOptions, Task, TaskStatus,
                                TransitMode)
from nanjingtrip.traffic import TrafficModel


def poi(name: str, pid: int) -> Poi:
    return Poi(name=name, district='玄武区', lng=118.8, lat=32.0,
               provider='mock', provider_poi_id=name, poi_id=pid)


def transit_fn(table=None, default=20):
    table = table or {}

    def fn(a, b, mode):
        return table.get((a.name, b.name), default)
    return fn


def opts(**kw) -> ScheduleOptions:
    kw.setdefault('mode', TransitMode.DRIVING)
    kw.setdefault('buffer_min', 10)
    kw.setdefault('day_start', time(8, 0))
    kw.setdefault('day_end', time(22, 0))
    return ScheduleOptions(**kw)


def leg_of(result, title):
    item = next(i for i in result.ordered() if i.task.title == title)
    return item.leg_before.duration_min if item.leg_before else 0


# --------------------------------------------------------------- 系数模型本身
@pytest.mark.parametrize('minute,mode,expect', [
    (8 * 60 + 30, TransitMode.DRIVING, 1.35),     # 早高峰
    (18 * 60, TransitMode.DRIVING, 1.35),         # 晚高峰
    (12 * 60, TransitMode.DRIVING, 1.0),          # 平峰
    (8 * 60 + 30, TransitMode.WALKING, 1.0),      # 步行不受拥堵影响
])
def test_traffic_factor_table(minute, mode, expect):
    assert TrafficModel().factor(minute, mode) == pytest.approx(expect)


# ------------------------------------------------------- 引擎里是否真的生效
def test_leg_in_peak_is_scaled():
    """08:30 从甲出发（早高峰）→ 20 分钟应放大到 27 分钟"""
    a = Task(title='甲处', duration_min=30, poi=poi('甲', 1),
             is_locked=True, start_ts=time(8, 0))      # 8:00-8:30
    b = Task(title='乙处', duration_min=30, poi=poi('乙', 2))
    base = schedule([a, b], opts(), transit_fn())
    peak = schedule([a, b], opts(peak_model=TrafficModel()), transit_fn())
    assert leg_of(base, '乙处') == 20, '未启用时段模型时应保持原始耗时'
    assert leg_of(peak, '乙处') == 27, f"早高峰应放大到 27，实际 {leg_of(peak, '乙处')}"


def test_leg_off_peak_is_not_scaled():
    """11:00 出发（平峰）→ 不加成"""
    a = Task(title='甲处', duration_min=60, poi=poi('甲', 1),
             is_locked=True, start_ts=time(10, 0))     # 10:00-11:00
    b = Task(title='乙处', duration_min=30, poi=poi('乙', 2))
    result = schedule([a, b], opts(peak_model=TrafficModel()), transit_fn())
    assert leg_of(result, '乙处') == 20


def test_walking_never_scaled_by_peak():
    """步行在高峰也不放大（否则会凭空多出走不动的时间）"""
    a = Task(title='甲处', duration_min=30, poi=poi('甲', 1),
             is_locked=True, start_ts=time(8, 0))
    b = Task(title='乙处', duration_min=30, poi=poi('乙', 2),
             mode=TransitMode.WALKING)
    result = schedule([a, b], opts(peak_model=TrafficModel()), transit_fn())
    assert leg_of(result, '乙处') == 20


def test_first_leg_uses_actual_departure_not_day_start():
    """首段若锁在 11:00（平峰），系数应按"实际几点上路"算，而不是按 08:00 早高峰。

    真实场景：用户说"上午十一点去总统府"，从住处出发时已经过了早高峰，
    不该仍按 ×1.35 放大。
    """
    b = Task(title='乙处', duration_min=60, poi=poi('乙', 2),
             is_locked=True, start_ts=time(11, 0))
    home = poi('家', 9)
    result = schedule([b], opts(lodging=home, peak_model=TrafficModel()),
                      transit_fn())
    leg = leg_of(result, '乙处')
    assert leg == 20, f'平峰首段应保持原始 20 分钟，实际 {leg}（疑似按 08:00 早高峰放大）'


def test_first_leg_at_peak_still_scaled():
    """反向确认：锁在 08:10（早高峰内）时仍要放大"""
    b = Task(title='乙处', duration_min=60, poi=poi('乙', 2),
             is_locked=True, start_ts=time(8, 10))
    home = poi('家', 9)
    result = schedule([b], opts(lodging=home, peak_model=TrafficModel()),
                      transit_fn())
    assert leg_of(result, '乙处') == 27, f'早高峰应放大到 27，实际 {leg_of(result, "乙处")}'


# ------------------------------------------------- 装配层：什么时候才启用
def test_options_pass_peak_model_only_when_online(tmp_path):
    """在线且有 Key 时才注入时段模型；离线估算不叠加高峰系数"""
    from nanjingtrip.app import AppContext

    c = AppContext(tmp_path)
    try:
        c.settings.offline_mode = True
        c.apply_settings(c.settings)
        assert c.trip_svc.options().peak_model is None, '离线不该启用时段模型'

        c.settings.map_api_key = 'dummy-key-for-assembly-test'
        c.settings.offline_mode = False
        c.settings.peak_aware = True
        c.apply_settings(c.settings)
        assert c.trip_svc.options().peak_model is not None, '在线应启用时段模型'

        c.settings.peak_aware = False
        c.apply_settings(c.settings)
        assert c.trip_svc.options().peak_model is None, '关掉开关后不该启用'
    finally:
        c.close()
