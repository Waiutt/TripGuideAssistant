# -*- coding: utf-8 -*-
"""出行时刻建议的测试（对应报告第 4 章 TC-16-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import TransitMode
from nanjingtrip.traffic import TrafficModel

D = date(2026, 10, 28)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


# ---------------------------------------------------------------- TC-16-01
def test_traffic_factor_by_slot():
    """TC-16-01 时段系数：早晚高峰放大、夜间收窄、步行不受拥堵影响"""
    m = TrafficModel()
    assert m.factor(8 * 60, TransitMode.DRIVING) == pytest.approx(1.35)     # 早高峰
    assert m.factor(18 * 60, TransitMode.DRIVING) == pytest.approx(1.35)    # 晚高峰
    assert m.factor(12 * 60, TransitMode.DRIVING) == pytest.approx(1.0)     # 平峰
    assert m.factor(23 * 60, TransitMode.DRIVING) < 1.0                     # 夜间
    assert m.factor(8 * 60, TransitMode.WALKING) == pytest.approx(1.0)      # 步行不受影响
    # 预测值随时段变化
    assert m.predict(20, 8 * 60, TransitMode.DRIVING) == 27
    assert m.predict(20, 12 * 60, TransitMode.DRIVING) == 20


# ---------------------------------------------------------------- TC-16-02
def test_departure_advice_latest_respects_buffer(ctx):
    """TC-16-02 最晚出发时刻满足"预测通行 + 缓冲 ≤ 目标开始时刻" """
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    out = ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    target = next(i for i in out.result.ordered() if i.task.title == '吃饭')

    advice = ctx.trip_svc.departure_advice(target.task.task_id)
    assert advice.ok, advice.message
    assert advice.latest is not None
    buffer = int(ctx.settings.buffer_min)
    assert advice.latest.arrive_min + buffer <= 12 * 60
    assert advice.latest.depart_min < 12 * 60
    assert '最晚' in advice.summary()


# ---------------------------------------------------------------- TC-16-03
def test_departure_advice_offers_offpeak_alternative(ctx):
    """TC-16-03 目标时刻在高峰时，给出"提前出发少堵但要多等"的备选"""
    from nanjingtrip.models import Task, TaskStatus
    from nanjingtrip.models import Poi

    a = Poi(name='南京博物院', district='玄武区', lng=118.82, lat=32.04,
            provider='mock', provider_poi_id='南京博物院')
    b = Poi(name='新街口', district='秦淮区', lng=118.77, lat=32.04,
            provider='mock', provider_poi_id='新街口')
    ctx.trip_repo.ensure_poi(a)
    ctx.trip_repo.ensure_poi(b)

    # 构造：9:00 在前一个地点，目标 10:00 到另一个地点（9 点仍在早高峰内）
    trip_out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概1小时', D)
    assert trip_out.ok
    prev = trip_out.result.ordered()[0].task
    prev.start_ts, prev.end_ts = time(8, 30), time(9, 30)

    draft = Task(title='开会', duration_min=60, poi=b, is_locked=True,
                 start_ts=time(10, 0), status=TaskStatus.SCHEDULED)
    out2 = ctx.trip_svc.commit_task(D, draft, note='测试')
    target = next(i for i in out2.result.ordered() if i.task.title == '开会')

    advice = ctx.trip_svc.departure_advice(target.task.task_id,
                                           TransitMode.DRIVING)
    assert advice.ok, advice.message
    assert advice.options, '应给出候选出发时刻表'
    # 备选（若存在）必须比最晚出发更省时、但要多等
    if advice.best_offpeak:
        assert advice.best_offpeak.minutes <= advice.latest.minutes
        assert advice.best_offpeak.wait_min >= advice.latest.wait_min


# ---------------------------------------------------------------- TC-16-04
def test_departure_advice_without_previous_place(ctx):
    """TC-16-04 当天第一项（无前置地点）→ 明确说明，不硬凑建议"""
    out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    task = out.result.ordered()[0].task
    advice = ctx.trip_svc.departure_advice(task.task_id)
    assert advice.ok is False
    assert '第一项' in advice.message


# ---------------------------------------------------------------- TC-16-05
def test_departure_advice_explains_data_source(ctx):
    """TC-16-05 建议里必须说明数据来源与估算边界（不能把估算说成高德预测）"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    out = ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    target = next(i for i in out.result.ordered() if i.task.title == '吃饭')
    advice = ctx.trip_svc.departure_advice(target.task.task_id)
    assert '非高德实时预测' in advice.caveat
    assert '基础时长' in advice.caveat
