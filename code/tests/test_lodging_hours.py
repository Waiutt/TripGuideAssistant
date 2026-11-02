# -*- coding: utf-8 -*-
"""营业时间、饭点与过夜住宿推荐测试（对应报告第 4 章 TC-26-* / TC-27-*）。

覆盖：闭馆日、超出开放时段、预约提示、饭点缺失、过夜检测、
住宿候选排序、住宿锚点（出发/回程段）、拒宿后不再推荐。
"""
from datetime import date, time, timedelta

import pytest

from nanjingtrip import hours as hours_mod
from nanjingtrip import lodging as lodging_mod
from nanjingtrip.app import AppContext
from nanjingtrip.cities import get_city
from nanjingtrip.models import (Poi, ScheduleOptions, ScheduleResult,
                                ScheduledItem, Task, TransitMode)

# 2026-11-02 是周一（故宫、国博、科技馆等闭馆日）
MON = date(2026, 11, 2)
TUE = date(2026, 11, 3)


@pytest.fixture()
def bj(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    c.set_city('北京')
    yield c
    c.close()


def _task(title: str, poi_name: str, city: str = '北京',
          start: time | None = None, duration: int = 60) -> Task:
    data = get_city(city).find(poi_name)
    poi = Poi.from_pack(data, city) if data else None
    return Task(title=title, duration_min=duration, poi=poi, start_ts=start,
                is_locked=start is not None)


# ================================================================ 营业时间
# ---------------------------------------------------------------- TC-26-01
def test_closed_day_detected_on_monday(bj):
    """TC-26-01 周一把故宫排进去：产生闭馆提示，并给出"挪到周二"的建议"""
    out = bj.trip_svc.preview_text('上午九点去故宫看展，大概3小时', MON)
    assert out.ok                                   # 排得下，只是会扑空
    closed = [c for c in out.notices if c.kind == 'closed_day']
    assert closed and '周一闭馆' in closed[0].detail
    texts = [s.text for s in closed[0].suggestions]
    assert any('11月3日' in t and '周二' in t for t in texts)
    assert any('仍按原计划' in t for t in texts)     # 保留"坚持要去"的选项


# ---------------------------------------------------------------- TC-26-02
def test_no_closed_notice_on_tuesday(bj):
    """TC-26-02 周二去故宫：不产生闭馆提示"""
    out = bj.trip_svc.preview_text('上午九点去故宫看展，大概3小时', TUE)
    assert out.ok
    assert not [c for c in out.notices if c.kind == 'closed_day']


# ---------------------------------------------------------------- TC-26-03
def test_out_of_open_hours(bj):
    """TC-26-03 天坛 06:00-22:00，把参观排到 21:30 之后应提示超出开放时段"""
    poi = Poi.from_pack(get_city('北京').find('北京动物园'), '北京')
    assert poi.open_to == '18:00'
    detail = hours_mod.hours_detail(poi, 17 * 60 + 30, 18 * 60 + 30)
    assert '超出开放时段' in detail
    ok_detail = hours_mod.hours_detail(poi, 9 * 60, 10 * 60)
    assert ok_detail == ''


# ---------------------------------------------------------------- TC-26-04
def test_meal_missing_when_lunch_occupied(bj):
    """TC-26-04 午餐时段被行程占满且当天没有餐饮项 → 提示漏了饭点"""
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='看展', duration_min=180), start_min=690,
                      end_min=870)])
    notices = hours_mod.meal_notices(res)
    assert notices and notices[0].kind == 'meal_missing'
    assert '午餐' in notices[0].detail


# ---------------------------------------------------------------- TC-26-05
def test_no_meal_notice_when_food_scheduled(bj):
    """TC-26-05 午餐时段已有餐饮项 → 不再提示"""
    food = _task('午餐', '四季民福烤鸭店故宫店')
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='看展', duration_min=120), start_min=690,
                      end_min=780),
        ScheduledItem(task=food, start_min=720, end_min=780),
    ])
    assert hours_mod.meal_notices(res) == []


# ---------------------------------------------------------------- TC-26-06
def test_meal_notice_can_be_disabled(bj):
    """TC-26-06 关掉饭点提示后不再产出（纯软提示，可配置）"""
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='看展', duration_min=180), start_min=690,
                      end_min=870)])
    opts = ScheduleOptions(meal_notice=False)
    assert hours_mod.meal_notices(res, opts) == []


# ---------------------------------------------------------------- TC-26-07
def test_booking_notice_aggregated(bj):
    """TC-26-07 需预约的地点汇总成一条，不逐条刷屏"""
    tasks = [_task('看展', '故宫博物院'), _task('看展2', '中国国家博物馆')]
    notices = hours_mod.booking_notices(tasks)
    assert len(notices) == 1
    assert '故宫博物院' in notices[0].detail
    assert '中国国家博物馆' in notices[0].detail


# ================================================================ 过夜与住宿
# ---------------------------------------------------------------- TC-26-08
def test_late_end_with_next_day_needs_lodging(bj):
    """TC-26-08 末项 21:40 结束且次日还有安排 → 建议住宿"""
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='夜游', duration_min=60), start_min=1240,
                      end_min=1300)])          # 20:40-21:40，跨过 21:30
    nxt = ScheduleResult(items=[
        ScheduledItem(task=Task(title='看展', duration_min=60), start_min=600,
                      end_min=660)])
    reason = lodging_mod.detect_reason(res, next_has_items=True)
    assert '次日还有安排' in reason
    notice = lodging_mod.overnight_notice(get_city('北京'), res, nxt)
    assert notice is not None and notice.kind == 'lodging_suggest'
    lodging_suggest = [s for s in notice.suggestions if s.kind == 'set_lodging']
    assert len(lodging_suggest) == 3                    # 给出三家候选
    kinds = {s.kind for s in notice.suggestions}
    assert {'set_lodging', 'move_next_day', 'shorten', 'no_lodging'} <= kinds


# ---------------------------------------------------------------- TC-26-09
def test_early_end_no_lodging(bj):
    """TC-26-09 末项 15:00 结束、次日也无安排 → 不推荐住宿"""
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='看展', duration_min=60), start_min=840,
                      end_min=900)])
    assert lodging_mod.detect_reason(res, next_has_items=False) == ''
    assert lodging_mod.overnight_notice(get_city('北京'), res, None) is None


# ---------------------------------------------------------------- TC-26-10
def test_very_late_end_always_needs_lodging(bj):
    """TC-26-10 末项 23:20 结束：深夜不便，即使次日没有安排也建议住宿"""
    res = ScheduleResult(items=[
        ScheduledItem(task=Task(title='夜宵', duration_min=60), start_min=1400,
                      end_min=1420)])
    assert '深夜' in lodging_mod.detect_reason(res, next_has_items=False)


# ---------------------------------------------------------------- TC-26-11
def test_recommend_sorted_by_distance():
    """TC-26-11 候选按"到锚点的平均距离"升序，最近的在最前"""
    pack = get_city('北京')
    anchor = Poi.from_pack(pack.find('北京南站'), '北京')
    recs = lodging_mod.recommend(pack, [anchor], limit=3)
    assert recs, '应有候选'
    dists = [r['distance_km'] for r in recs]
    assert dists == sorted(dists)
    assert '全季酒店北京南站店' in [r['poi'].name for r in recs]
    assert '公里' in recs[0]['reason'] or '米' in recs[0]['reason']


# ---------------------------------------------------------------- TC-26-12
def test_lodging_mentioned_in_sentence(bj):
    """TC-26-12 一句话"住在XX"即设为住宿锚点，且不产生事项"""
    out = bj.trip_svc.preview_text('住在王府井希尔顿酒店', TUE)
    assert out.ok and out.lodging is not None
    assert out.lodging.name == '王府井希尔顿酒店'
    assert out.draft is None                       # 不变成一条事项
    assert '入住 15:00' in out.explanation

    res = bj.trip_svc.set_lodging(TUE, '王府井希尔顿酒店')
    assert res.ok
    assert bj.trip_svc.day_view(TUE).lodging.name == '王府井希尔顿酒店'
    assert len(bj.trip_svc.day_view(TUE).tasks) == 0


# ---------------------------------------------------------------- TC-26-13
def test_lodging_anchor_adds_origin_and_return_leg(bj):
    """TC-26-13 设住宿后：首项前有"酒店→首项"通行段，末项后有回程段"""
    bj.trip_svc.set_lodging(TUE, '王府井希尔顿酒店')
    assert bj.trip_svc.add_text('上午十点去天坛公园看展，大概2小时', TUE).ok
    res = bj.trip_svc.compute_day(TUE)
    first = res.items[0]
    assert first.leg_before is not None and first.leg_before.duration_min > 0
    assert res.return_leg is not None and res.return_leg.duration_min > 0
    assert '王府井希尔顿酒店' in res.explanation


# ---------------------------------------------------------------- TC-26-14
def test_clear_lodging_removes_return_leg(bj):
    """TC-26-14 清除住宿锚点后：回程段消失、首段回到"无前驱"状态"""
    bj.trip_svc.set_lodging(TUE, '王府井希尔顿酒店')
    bj.trip_svc.add_text('上午十点去天坛公园看展，大概2小时', TUE)
    assert bj.trip_svc.compute_day(TUE).return_leg is not None

    out = bj.trip_svc.clear_lodging(TUE)
    assert out.ok
    res = bj.trip_svc.compute_day(TUE)
    assert bj.trip_svc.day_view(TUE).lodging is None
    assert res.return_leg is None
    assert res.items[0].leg_before is None


# ---------------------------------------------------------------- TC-26-15
def test_lodging_suggestion_suppressed_when_set(bj):
    """TC-26-15 已设住宿锚点后不再推荐住宿"""
    bj.trip_svc.set_lodging(MON, '北京饭店')
    bj.trip_svc.add_text('晚上八点去什刹海逛逛，2小时', MON)
    bj.trip_svc.add_text('上午十点去中国国家博物馆看展，大概3小时', TUE)
    res = bj.trip_svc.compute_day(MON)
    assert not [c for c in res.notices if c.kind == 'lodging_suggest']


# ---------------------------------------------------------------- TC-26-16
def test_decline_lodging_stops_repeating(bj):
    """TC-26-16 明确"当天往返、不住"后，不再反复推荐（但仍可恢复）"""
    bj.trip_svc.add_text('晚上八点去什刹海逛逛，2小时', MON)
    bj.trip_svc.add_text('上午十点去中国国家博物馆看展，大概3小时', TUE)
    res = bj.trip_svc.compute_day(MON)
    assert [c for c in res.notices if c.kind == 'lodging_suggest']

    assert bj.trip_svc.decline_lodging(MON).ok
    res2 = bj.trip_svc.compute_day(MON)
    assert not [c for c in res2.notices if c.kind == 'lodging_suggest']

    bj.trip_svc.decline_lodging(MON, declined=False)
    res3 = bj.trip_svc.compute_day(MON)
    assert [c for c in res3.notices if c.kind == 'lodging_suggest']


# ---------------------------------------------------------------- TC-26-17
def test_recommend_lodging_api_shape(bj):
    """TC-26-17 界面用接口返回可直接展示的字段（名称/星级/价位/理由）"""
    bj.trip_svc.add_text('晚上八点去什刹海逛逛，2小时', MON)
    recs = bj.trip_svc.recommend_lodging(MON)
    assert 1 <= len(recs) <= 3
    r = recs[0]
    assert {'name', 'star', 'price', 'reason', 'distance_km'} <= set(r)
    assert r['price'].startswith('约 ¥')


# ---------------------------------------------------------------- TC-26-18
def test_food_without_duration_uses_default(bj):
    """TC-26-18 只说"去吃饭"没给时长：餐饮按 60 分钟兜底，不再报"缺少耗时" """
    out = bj.trip_svc.preview_text('中午去四季民福烤鸭店吃饭', TUE)
    assert out.ok, out.error
    # 以前一律 60 分钟；现在优先用城市包的推荐时长（这家店 90 分钟）
    assert out.draft.duration_min == (out.draft.poi.visit_min or 60)
    assert out.draft.poi.is_food


# ---------------------------------------------------------------- TC-26-19
def test_sight_without_duration_uses_recommended_visit(bj):
    """TC-26-19（改）景点缺耗时不拦：按城市包的**推荐游玩时长**兜底，并说明来源"""
    out = bj.trip_svc.preview_text('明天去故宫看展', TUE)
    assert out.ok, out.error
    assert out.draft.poi is not None and out.draft.poi.visit_min
    assert out.draft.duration_min == out.draft.poi.visit_min


# ---------------------------------------------------------------- TC-26-20
def test_engine_anchor_keeps_first_item_after_travel(bj):
    """TC-26-20 引擎层面：设锚点后首项不早于"酒店出发 + 通行 + 缓冲" """
    hotel = Poi.from_pack(get_city('北京').find('全季酒店北京南站店'), '北京')
    task = _task('看展', '故宫博物院', duration=120)
    opts = ScheduleOptions(mode=TransitMode.TRANSIT, buffer_min=10,
                          lodging=hotel)

    def transit(a, b, mode):
        return 30

    from nanjingtrip.engine import schedule
    res = schedule([task], opts, transit)
    assert res.items[0].start_min == 8 * 60 + 30 + 10      # 08:00 + 30 通行 + 10 缓冲
    assert res.return_leg is not None
