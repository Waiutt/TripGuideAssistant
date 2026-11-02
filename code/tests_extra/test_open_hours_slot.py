"""开放时段参与择时（D-60 回归）

"排进去"不等于"排得对"：把博物馆排在 08:00（09:00 才开门）、把陵园排到 20:04
（17:00 就闭园），时间轴上看着满满的，实际上一天白跑。
开放时段不是硬约束（用户硬要那个点也得排），但只要有落在营业时间内的空档，
就该优先选它，排不进去时再由提示模块如实告知。
"""
from datetime import time

import pytest

from nanjingtrip.cities import get_city
from nanjingtrip.engine import schedule
from nanjingtrip.models import Poi, ScheduleOptions, Task, TransitMode
from nanjingtrip import hours as hours_mod


def _poi(name):
    return Poi.from_pack(get_city('南京').find(name), '南京')


def _est(a, b, mode=None):
    return 6


def _opt(**kw):
    base = dict(mode=TransitMode.DRIVING, buffer_min=5,
                day_start=time(8, 0), day_end=time(22, 0))
    base.update(kw)
    return ScheduleOptions(**base)


def test_museum_not_before_opening():
    """南京博物院 09:00 开门：首项不许排在 08:00"""
    tasks = [Task(title='南京博物院', duration_min=120, poi=_poi('南京博物院'))]
    res = schedule(tasks, _opt(), _est)
    it = res.items[0]
    assert it.start_min >= 9 * 60, f'排在 {it.start_min // 60:02d}:{it.start_min % 60:02d}'
    assert hours_mod.hours_hint(it.task.poi, it.start_min, it.end_min) == ''


def test_short_task_fills_gap_outside_hours_rather_than_inside():
    """有空档时优先选营业时间内的那个，而不是"最早能放"的开门前"""
    tasks = [Task(title='总统府', duration_min=60, poi=_poi('总统府')),
             Task(title='先锋书店', duration_min=40, poi=_poi('先锋书店五台山店'))]
    res = schedule(tasks, _opt(), _est)
    for it in res.items:
        assert hours_mod.hours_hint(it.task.poi, it.start_min, it.end_min) == '', \
            (it.task.title, it.start_min)


def test_closing_time_respected_when_possible():
    """鸡鸣寺 17:00 关门：一天里排在它之后的任务要挪到闭馆前，而不是硬塞到 20:00"""
    tasks = [Task(title='玄武湖公园', duration_min=60, poi=_poi('玄武湖公园')),
             Task(title='鸡鸣寺', duration_min=60, poi=_poi('鸡鸣寺')),
             Task(title='总统府', duration_min=60, poi=_poi('总统府'))]
    res = schedule(tasks, _opt(), _est)
    ji = next(i for i in res.items if i.task.title == '鸡鸣寺')
    assert ji.end_min <= 17 * 60, f'鸡鸣寺排到 {ji.end_min // 60:02d}:{ji.end_min % 60:02d}'


def test_impossible_case_still_schedules_and_warns():
    """全天排满仍塞不进营业时间时：照样排（不丢项），但提示必须给出"""
    tasks = [Task(title=f'总统府', duration_min=60, poi=_poi('总统府'))
             for _ in range(1)]
    tasks += [Task(title='玄武湖公园', duration_min=600, poi=_poi('玄武湖公园'))]
    res = schedule(tasks, _opt(day_start=time(19, 0)), _est)
    assert res.items, '排不进也不能丢项'
    bad = [hours_mod.hours_hint(i.task.poi, i.start_min, i.end_min) for i in res.items]
    assert any(bad), '超出营业时间却没产生提示'
