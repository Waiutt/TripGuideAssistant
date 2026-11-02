"""引擎边界：去重、同点通勤、无地点块、返程截止、体力阈值、统计口径

（由 test_dedup.py + test_same_point_transit.py + test_gap_before_block.py + test_return_deadline.py + test_mode_caps.py + test_stats_consistency.py 合并而来：用例数不变，文件数减少，便于分层阅读）
"""

from __future__ import annotations
from datetime import date
from datetime import time
from nanjingtrip.app import AppContext
from nanjingtrip.engine import _transit
from nanjingtrip.engine import schedule
from nanjingtrip.models import (Poi, ScheduleOptions, Task, TransitMode)
from nanjingtrip.models import Poi
from nanjingtrip.models import Poi, TransitMode
from nanjingtrip.services import title_with_poi
import pytest


# ======================================================================
# 原 test_dedup.py
# ======================================================================

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def items():
    return [
        {'title': '游览鸡鸣寺', 'poi': '鸡鸣寺', 'duration_min': 50},
        {'title': '再游鸡鸣寺', 'poi': '鸡鸣寺', 'duration_min': 40},
        {'title': '游览总统府', 'poi': '总统府', 'duration_min': 60},
    ]


def test_duplicate_in_one_batch_is_skipped(ctx):
    """同一批里重复的地点只写入一次"""
    ok, msg = ctx.trip_svc.add_batch(D, items(), keep_order=True)
    assert ok, msg
    trip = ctx.trip_svc.day_view(D)
    names = [t.poi.name for t in trip.tasks if t.poi]
    assert names.count('鸡鸣寺') == 1, f'鸡鸣寺被排了 {names.count("鸡鸣寺")} 次：{names}'
    assert '总统府' in names
    assert '跳过' in msg, f'应当告诉用户跳过了重复项：{msg}'


def test_duplicate_across_calls_is_skipped(ctx):
    """先加过一次，再加同一地点，也不该重复"""
    ctx.trip_svc.add_batch(D, [{'title': '游览鸡鸣寺', 'poi': '鸡鸣寺',
                                'duration_min': 50}], keep_order=True)
    ok, msg = ctx.trip_svc.add_batch(D, [{'title': '游览鸡鸣寺', 'poi': '鸡鸣寺',
                                          'duration_min': 30}], keep_order=True)
    trip = ctx.trip_svc.day_view(D)
    names = [t.poi.name for t in trip.tasks if t.poi]
    assert names.count('鸡鸣寺') == 1, f'重复排入：{names}'
    assert '跳过' in msg


def test_different_places_still_both_added(ctx):
    """不同地点不受影响（别把去重做成"只留一个"）"""
    ok, msg = ctx.trip_svc.add_batch(D, [
        {'title': '游览鸡鸣寺', 'poi': '鸡鸣寺', 'duration_min': 50},
        {'title': '游览总统府', 'poi': '总统府', 'duration_min': 60},
        {'title': '游览夫子庙', 'poi': '夫子庙', 'duration_min': 60},
    ], keep_order=True)
    assert ok, msg
    names = [t.poi.name for t in ctx.trip_svc.day_view(D).tasks if t.poi]
    assert set(names) == {'鸡鸣寺', '总统府', '夫子庙'}


# ======================================================================
# 原 test_same_point_transit.py
# ======================================================================

D = date(2026, 11, 18)


@pytest.fixture()
def svc(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c.trip_svc
    c.close()


def test_same_poi_transit_is_zero(svc):
    """同一地点之间通行时长必须是 0，不是 1 分钟也不是 30 分钟"""
    poi = svc.preview_text('去鸡鸣寺停留50分钟', D).draft.poi
    assert poi is not None
    for mode in (TransitMode.DRIVING, TransitMode.WALKING, TransitMode.BICYCLING):
        assert svc.transit.estimate(poi, poi, mode) == 0, \
            f'{mode.value} 同点通行应为 0'


def test_transit_none_not_treated_as_30_for_same_point():
    """引擎层：同一点即使查不到，也不能兜底成 30 分钟"""
    poi = Poi(name='甲', poi_id=1, lng=118.79, lat=32.06)
    minutes, leg = _transit(lambda a, b, m: None, poi, poi, TransitMode.DRIVING)
    assert minutes == 0, f'同点被算成了 {minutes} 分钟'
    assert leg is None or leg.duration_min == 0


def test_transit_none_still_conservative_for_real_leg():
    """不同地点查不到时，仍保留 30 分钟保守估计（这是既有行为，别改坏）"""
    a = Poi(name='甲', poi_id=1, lng=118.79, lat=32.06)
    b = Poi(name='乙', poi_id=2, lng=118.86, lat=32.12)
    minutes, _ = _transit(lambda x, y, m: None, a, b, TransitMode.DRIVING)
    assert minutes == 30


# ======================================================================
# 原 test_gap_before_block.py
# ======================================================================

def poi(name: str, pid: int) -> Poi:
    return Poi(name=name, district='玄武区', lng=118.8 + pid * 0.01, lat=32.0,
               provider='mock', provider_poi_id=name, poi_id=pid)


def transit_fn(table=None, default=20):
    table = table or {}

    def fn(a, b, mode):
        return table.get((a.name, b.name), default)
    return fn


def opts() -> ScheduleOptions:
    return ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0),
                           # 真实案例都设了起点锚点——这正是踩坑的条件
                           lodging=poi('南京审计大学莫愁校区', 9))


def build():
    """牛首山 08:55-10:05 → 午休 12:00-13:00（锁） → 两个待排项"""
    a = Task(title='牛首山', duration_min=70, poi=poi('牛首山', 1),
             is_locked=True, start_ts=time(8, 55))
    lunch = Task(title='午饭', duration_min=60, poi=None, category='餐饮',
                 is_locked=True, start_ts=time(12, 0))
    b = Task(title='雨花台', duration_min=60, poi=poi('雨花台', 2))
    c = Task(title='甘熙宅第', duration_min=50, poi=poi('甘熙宅第', 3))
    table = {('牛首山', '雨花台'): 31, ('雨花台', '甘熙宅第'): 15,
             ('牛首山', '甘熙宅第'): 25}
    return [a, lunch, b, c], transit_fn(table)


def test_task_fits_before_poiless_block():
    tasks, fn = build()
    result = schedule(tasks, opts(), fn)
    item = next(i for i in result.ordered() if i.task.title == '雨花台')
    assert item.start_min < 12 * 60, (
        f'雨花台被排到 {item.start_ts}，本可以塞进午休前（10:46-11:46）')


def test_no_big_idle_gap_in_morning():
    tasks, fn = build()
    result = schedule(tasks, opts(), fn)
    items = result.ordered()
    gaps = [(items[i + 1].start_min - items[i].end_min)
            for i in range(len(items) - 1)]
    assert max(gaps) <= 70, f'出现了 {max(gaps)} 分钟的大空档：{gaps}'


def test_lunch_block_itself_has_no_transit():
    """午休块自己（无地点）不该凭空产生通行段"""
    tasks, fn = build()
    result = schedule(tasks, opts(), fn)
    meal = next(i for i in result.ordered() if i.task.title == '午饭')
    assert meal.leg_before is None


# ======================================================================
# 原 test_return_deadline.py
# ======================================================================

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.settings.return_by = ''
    c.apply_settings(c.settings)
    yield c
    c.close()


def build_day(ctx):
    ctx.trip_svc.set_lodging(D, '南京邮电大学三牌楼校区')
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)


def kinds(result):
    return {c.kind for c in result.notices}


def test_return_late_is_reported(ctx):
    """返回截止早于实际回程时刻 → 必须给出 return_late 提示"""
    ctx.settings.return_by = '09:30'        # 明显赶不上
    ctx.apply_settings(ctx.settings)
    build_day(ctx)
    result = ctx.trip_svc.compute_day(D)
    assert 'return_late' in kinds(result), f'没有告警：{kinds(result)}'
    detail = next(c.detail for c in result.notices if c.kind == 'return_late')
    assert '回到起点' in detail and '晚' in detail


def test_return_within_deadline_is_quiet(ctx):
    """返回截止足够晚 → 不该报"""
    ctx.settings.return_by = '23:00'
    ctx.apply_settings(ctx.settings)
    build_day(ctx)
    result = ctx.trip_svc.compute_day(D)
    assert 'return_late' not in kinds(result)


def test_no_deadline_no_notice(ctx):
    """不设截止 → 不产生这类提示（默认行为不变）"""
    ctx.settings.return_by = ''
    ctx.apply_settings(ctx.settings)
    build_day(ctx)
    result = ctx.trip_svc.compute_day(D)
    assert 'return_late' not in kinds(result)


# ======================================================================
# 原 test_mode_caps.py
# ======================================================================

A = Poi(name='甲', lng=118.79000, lat=32.06000)
B = Poi(name='乙', lng=118.86000, lat=32.12000)      # 约 9 km 直线，避开 500/1500 米两条下限


@pytest.fixture()
def svc(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c.trip_svc
    c.close()


def stub(svc, **modes):
    svc.transit.estimate_all = lambda a, b: dict(modes)


def test_bike_over_30min_is_not_picked(svc):
    """骑行 53 分钟（>30）不参与，改选最快的可行方式"""
    stub(svc, driving=68, transit=75, walking=169, bicycling=53)
    assert svc.pick_fastest_mode(A, B, TransitMode.DRIVING) == TransitMode.DRIVING


def test_walk_over_45min_is_not_picked(svc):
    """步行 66 分钟（>45）不参与，即使它比驾车快 1 分钟"""
    stub(svc, driving=67, transit=105, walking=66, bicycling=50)
    assert svc.pick_fastest_mode(A, B, TransitMode.DRIVING) == TransitMode.DRIVING


def test_within_caps_still_takes_fastest(svc):
    """阈值内的骑行仍然照常择优（不能把短途骑行也砍掉）"""
    stub(svc, driving=28, transit=50, walking=41, bicycling=13)
    assert svc.pick_fastest_mode(A, B, TransitMode.DRIVING) == TransitMode.BICYCLING
    # 步行 41 分钟 ≤45，若它最快也应可选
    stub(svc, driving=50, transit=60, walking=41, bicycling=48)
    assert svc.pick_fastest_mode(A, B, TransitMode.DRIVING) == TransitMode.WALKING


def test_all_excluded_falls_back_to_default(svc):
    """全被排除时退回默认方式，返回 None/异常都不行"""
    stub(svc, driving=None, transit=None, walking=120, bicycling=90)
    assert svc.pick_fastest_mode(A, B, TransitMode.TRANSIT) == TransitMode.TRANSIT


# ======================================================================
# 原 test_stats_consistency.py
# ======================================================================

D = date(2026, 11, 1)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True          # 不依赖外网
    c.apply_settings(c.settings)
    yield c
    c.close()


# ------------------------------------------------------------------ 统计口径
def test_stats_transit_matches_scheduled_legs(ctx):
    """统计条"通行"必须等于排程说明里的"通行合计"（同一个数，不能各算一套）"""
    # 复现真实场景：先按"全局默认驾车"建好行程（方式写进库），
    # 之后才打开逐段择优。库里的方式没被写回，而重算会给出步行/骑行。
    ctx.settings.auto_mode = False
    ctx.apply_settings(ctx.settings)
    ctx.trip_svc.set_lodging(D, '东南大学四牌楼校区')
    assert ctx.trip_svc.add_text('上午九点去鸡鸣寺，停留50分钟', D).ok
    assert ctx.trip_svc.add_text('十点半去南京城墙台城景区，停留1小时', D).ok
    # 全局默认方式不会写进任务：库里 tasks[].mode 是空的（真实案例 11-01/02 正是如此）
    assert not {t.mode for t in ctx.trip_svc.day_view(D).tasks if t.mode}

    ctx.settings.auto_mode = True            # 只刷新装配，不重排（与真实场景一致）
    ctx.apply_settings(ctx.settings)

    result = ctx.trip_svc.compute_day(D)
    picked = {i.leg_before.mode.value for i in result.ordered() if i.leg_before}
    assert picked and 'driving' not in picked, f'测试前提：择优不应选驾车，实际 {picked}'

    legs = sum(i.leg_before.duration_min for i in result.ordered() if i.leg_before)
    assert legs > 0, '测试前提：当天应有通行段'
    assert f'通行合计 {legs} 分钟' in result.explanation

    stats = ctx.trip_svc.day_stats(ctx.trip_svc.day_view(D))
    assert stats['transit'] == legs, (
        f"统计条 {stats['transit']} 分钟 ≠ 排程说明 {legs} 分钟："
        '统计不能按"任务上存的方式或全局默认"另算一套')


# -------------------------------------------------------------- 离线标题形态
def test_offline_bare_title_normalized():
    """规则解析抽不出事项名时会给"（停留）"这类空壳，标题要能用地点兜底"""
    poi = Poi(name='鸡鸣寺', district='玄武区', kind='景点',
              open_from='07:00', open_to='17:00')
    assert title_with_poi('（停留）', poi) == '游览鸡鸣寺'
    assert title_with_poi('(停留)', poi) == '游览鸡鸣寺'
    # 已经带地点名的不要动
    assert title_with_poi('玄武湖台城段（停留）', poi) == '玄武湖台城段（停留）'
    assert title_with_poi('取报告', poi) == '取报告'
