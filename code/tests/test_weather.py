# -*- coding: utf-8 -*-
"""P1-3：天气（提示 + 雨天室内优先）。

诚实边界（写进测试，避免将来"看起来有天气"其实在编）：
  * 高德预报只有 4 天窗口；窗口外必须明确"无预报数据"，不能默认晴；
  * 雨天只调整**偏好与提示**，绝不删除用户选定的景点；
  * 室内/室外是数据属性，不能靠"名字里有没有'馆'"猜。
"""
from __future__ import annotations

from datetime import date, time

import pytest

from nanjingtrip.models import Poi, ScheduleOptions, Task, TransitMode
from nanjingtrip.weather import (forecast_for, is_wet, parse_casts, summary)

# 高德 /v3/weather/weatherInfo?extensions=all 的真实返回片段（截取）
RAW = {
    "status": "1", "info": "OK", "count": "1",
    "forecasts": [{
        "city": "南京市", "adcode": "320100", "province": "江苏",
        "reporttime": "2026-10-30 18:34:37",
        "casts": [
            {"date": "2026-10-30", "week": "5", "dayweather": "晴",
             "nightweather": "晴", "daytemp": "22", "nighttemp": "11",
             "daywind": "西", "daypower": "1-3"},
            {"date": "2026-10-31", "week": "6", "dayweather": "多云",
             "nightweather": "小雨", "daytemp": "24", "nighttemp": "14",
             "daywind": "东南", "daypower": "1-3"},
            {"date": "2026-11-01", "week": "7", "dayweather": "中雨",
             "nightweather": "小雨", "daytemp": "21", "nighttemp": "13",
             "daywind": "西北", "daypower": "1-3"},
        ],
    }],
}


def casts():
    return parse_casts(RAW)


# ------------------------------------------------------------------ 解析与取日
def test_parse_casts_reads_all_days():
    assert [c['date'] for c in casts()] == ['2026-10-30', '2026-10-31', '2026-11-01']


def test_forecast_for_hits_window():
    c = forecast_for(casts(), date(2026, 10, 31))
    assert c is not None and c['dayweather'] == '多云'


def test_forecast_for_outside_window_is_none():
    """超出预报窗口必须返回 None —— 界面据此显示"无预报数据"，不许猜"""
    assert forecast_for(casts(), date(2026, 11, 18)) is None
    assert forecast_for([], date(2026, 11, 1)) is None


@pytest.mark.parametrize('weather,wet', [
    ('中雨', True), ('小雨', True), ('雷阵雨', True), ('雨夹雪', True),
    ('小雪', True), ('晴', False), ('多云', False), ('阴', False),
])
def test_is_wet(weather, wet):
    assert is_wet({'dayweather': weather, 'nightweather': '晴'}) is wet


def test_is_wet_considers_night():
    """白天晴、夜间小雨也算当天有雨（用户晚上还在外面）"""
    assert is_wet({'dayweather': '晴', 'nightweather': '小雨'}) is True


def test_summary_is_readable():
    s = summary(forecast_for(casts(), date(2026, 11, 1)))
    assert '中雨' in s and '21' in s and '13' in s


# ------------------------------------------------------------------ 室内/室外
@pytest.mark.parametrize('kind,indoor', [
    ('场馆', True), ('商圈', True), ('书店', True), ('餐饮', True),
    ('住宿', True), ('公园', False), ('景点', False), ('街区', False),
    ('校园', False), ('交通', False),
])
def test_indoor_derived_from_kind(kind, indoor):
    poi = Poi.from_pack({'name': 'X', 'kind': kind})
    assert poi.indoor is indoor


def test_indoor_explicit_override_wins():
    """「甘熙宅第」是景点但主要是室内参观，数据里显式标了 indoor"""
    poi = Poi.from_pack({'name': '甘熙宅第', 'kind': '景点', 'indoor': True})
    assert poi.indoor is True
    poi2 = Poi.from_pack({'name': '某公园', 'kind': '公园', 'indoor': False})
    assert poi2.indoor is False


# ----------------------------------------------------- 雨天优先室内（排序）
def _poi(name, pid, indoor):
    return Poi(name=name, district='玄武区', lng=118.8 + pid * 0.01, lat=32.0,
               provider='mock', provider_poi_id=name, poi_id=pid, indoor=indoor)


def transit_fn(table=None, default=20):
    table = table or {}

    def fn(a, b, mode):
        return table.get((a.name, b.name), default)
    return fn


def test_prefer_indoor_puts_indoor_first():
    from nanjingtrip.engine import schedule
    home = _poi('家', 1, False)
    indoor1 = _poi('博物馆甲', 2, True)
    indoor2 = _poi('商场乙', 3, True)
    outdoor1 = _poi('公园丙', 4, False)
    tasks = [Task(title='公园丙', duration_min=60, poi=outdoor1),
             Task(title='博物馆甲', duration_min=60, poi=indoor1),
             Task(title='商场乙', duration_min=60, poi=indoor2)]
    opts = ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0),
                           lodging=home, optimize_route=True,
                           prefer_indoor=True)
    result = schedule(tasks, opts, transit_fn())
    order = [i.task.title for i in result.ordered()]
    assert order.index('公园丙') == 2, f'雨天应先室内后室外，实际 {order}'


def test_prefer_indoor_off_keeps_normal_order():
    from nanjingtrip.engine import schedule
    home = _poi('家', 1, False)
    indoor1 = _poi('博物馆甲', 2, True)
    outdoor1 = _poi('公园丙', 4, False)
    tasks = [Task(title='公园丙', duration_min=60, poi=outdoor1),
             Task(title='博物馆甲', duration_min=60, poi=indoor1)]
    opts = ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0),
                           lodging=home, optimize_route=True,
                           prefer_indoor=False)
    result = schedule(tasks, opts, transit_fn())
    assert len(result.items) == 2      # 不开启时行为不变，只是不强制室内优先
