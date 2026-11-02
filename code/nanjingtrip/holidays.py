# -*- coding: utf-8 -*-
"""节假日与休息日（节假日拥堵加成、人流/预约预警用）。

诚实边界
--------
只收录**公历固定**的法定节假日与周末：
  * 元旦 01-01、劳动节 05-01、国庆节 10-01~10-07；
  * 春节 / 清明 / 端午 / 中秋按**农历**，须以当年国务院放假通知为准，
    本项目**不臆造日期**——未收录就明说"未收录"，调用方可自行注入。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

#: 公历固定的法定节假日：(起始月-日, 结束月-日, 名称)
_FIXED_RANGES = (
    ((1, 1), (1, 1), '元旦'),
    ((5, 1), (5, 1), '劳动节'),
    ((10, 1), (10, 7), '国庆节'),
)

#: 可在运行时注入的额外假期（例如按当年官方通知补春节等）：{'YYYY-MM-DD': '春节'}
EXTRA_HOLIDAYS: dict[str, str] = {}


def _in_range(day: date, start: tuple[int, int], end: tuple[int, int]) -> bool:
    s = date(day.year, start[0], start[1])
    e = date(day.year, end[0], end[1])
    return s <= day <= e


def holiday_of(day: date) -> str:
    """返回节假日名称；不是节假日返回空串（未收录的农历节日也返回空串）"""
    key = day.isoformat()
    if key in EXTRA_HOLIDAYS:
        return EXTRA_HOLIDAYS[key]
    for start, end, name in _FIXED_RANGES:
        if _in_range(day, start, end):
            window = date(day.year, start[0], start[1])
            return name
    return ''


def is_rest_day(day: date) -> bool:
    """休息日 = 周末 或 法定节假日（节假日调休上班的情况本项目未建模，见说明）"""
    return day.weekday() >= 5 or bool(holiday_of(day))


def holiday_day_index(day: date) -> Optional[int]:
    """节假日第几天（1 起）；不是节假日返回 None"""
    name = holiday_of(day)
    if not name:
        return None
    for start, end, n in _FIXED_RANGES:
        if n == name and _in_range(day, start, end):
            return (day - date(day.year, start[0], start[1])).days + 1
    return 1


def lunar_holidays_note() -> str:
    """说明哪些节日未收录，供界面/报告如实交代"""
    return ('春节、清明、端午、中秋按农历，需以当年官方放假通知为准；'
            '本项目未收录，可通过 EXTRA_HOLIDAYS 注入')


def holiday_warning(day: date) -> str:
    """节假日人流/预约预警文案；非节假日返回空串"""
    name = holiday_of(day)
    if not name:
        return ''
    idx = holiday_day_index(day)
    tail = f'（第 {idx} 天）' if idx and idx > 1 else ''
    return (f'{name}假期{tail}：热门景点人流大、路上更堵，'
            '建议提前预约并留足路上时间')

