"""「依次去 A、B、C」的顺序语义（D-59 回归）

用户说了顺序，就必须**后一项排在前一项之后**。早期实现只按给定顺序"遍历"，
每一项都找全天空档里最早能放的位置 —— 于是 30 分钟的短任务（台城）会抢进上午的
空档、跳到 2 小时的看展前面，顺序名存实亡。
"""
from datetime import time

import pytest

from nanjingtrip.cities import get_city
from nanjingtrip.engine import schedule
from nanjingtrip.models import Poi, ScheduleOptions, Task, TransitMode


def _poi(name):
    return Poi.from_pack(get_city('南京').find(name), '南京')


def _est(a, b, mode=None):
    return {'driving': 12, 'transit': 20, 'walking': 40, 'bicycling': 25}[
        getattr(mode, 'value', mode) or 'driving']


def _opt(keep_order: bool) -> ScheduleOptions:
    return ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0),
                           keep_order=keep_order)


def _tasks():
    return [Task(title='总统府', duration_min=60, poi=_poi('总统府')),
            Task(title='南京博物院', duration_min=120, poi=_poi('南京博物院')),
            Task(title='台城', duration_min=30, poi=_poi('南京城墙台城景区'))]


def test_keep_order_chains_tasks_in_given_order():
    res = schedule(_tasks(), _opt(True), _est)
    assert [i.task.title for i in res.items] == ['总统府', '南京博物院', '台城']
    starts = [i.start_min for i in res.items]
    assert starts == sorted(starts)
    # 后一项不得早于前一项结束（顺序优先于"抢最早空档"）
    for prev, cur in zip(res.items, res.items[1:]):
        assert cur.start_min >= prev.end_min


def test_without_keep_order_short_task_may_jump_ahead():
    """关掉顺序约束时，短任务抢进早期空档是允许的（按耗时降序铺排）"""
    res = schedule(_tasks(), _opt(False), _est)
    assert [i.task.title for i in res.items][0] == '南京博物院'


def test_keep_order_respects_locked_time():
    """锁定时刻优先于顺序：用户在 12:00 订了饭，它就不会被顺序拖到后面"""
    tasks = _tasks()
    tasks.append(Task(title='午饭', duration_min=60, poi=_poi('南京大牌档德基店'),
                      desired_start=time(12, 0), is_locked=True,
                      start_ts=time(12, 0)))
    res = schedule(tasks, _opt(True), _est)
    lunch = next(i for i in res.items if i.task.title == '午饭')
    assert lunch.start_min == 12 * 60
