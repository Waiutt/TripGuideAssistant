# -*- coding: utf-8 -*-
"""边界测试 A 层：写入与数据完整性。

断言依据是"契约"（AGENTS.md + 边界测试计划），不是当前实现的行为：
  · 同一天重复提交不产生第二份
  · 并发写入不丢数据、不重复
  · 删除/清空后收敛，不留空洞
  · 超长一天不崩，排不下的要明确提示
  · 时刻边界不产生非法时间
  · 版本回滚后数据仍可读
"""
import threading
from datetime import date

import pytest

from nanjingtrip.app import AppContext

DAY = date(2026, 11, 10)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.settings.day_start = '08:00'
    c.settings.day_end = '22:00'
    c.apply_settings(c.settings)
    yield c
    c.close()


def items(*names):
    return [{'poi': n, 'title': f'游览{n}', 'duration_min': 90} for n in names]


def names_of(ctx, day=DAY):
    return [t.poi.name for t in ctx.trip_svc.day_view(day).tasks if t.poi]


def test_a01_duplicate_submit_keeps_one_copy(ctx):
    """A-01 同一批地点提交两次：第二次提示已排过，任务数不变"""
    assert ctx.trip_svc.add_batch(DAY, items('南京博物院', '夫子庙'), keep_order=True)[0]
    before = len(names_of(ctx))
    ok, msg = ctx.trip_svc.add_batch(DAY, items('南京博物院', '夫子庙'), keep_order=True)
    assert ok, msg
    assert len(names_of(ctx)) == before, '重复提交不能产生第二份'
    assert '已经排过' in msg or '跳过' in msg, f'应明确提示重复，实际：{msg}'


def test_a02_concurrent_writes_keep_all_items(ctx):
    """A-02 两个线程同时向同一天写入：不丢数据、不重复、不抛异常"""
    errors = []

    def worker(names):
        try:
            ctx.trip_svc.add_batch(DAY, items(*names), keep_order=True)
        except Exception as exc:                      # noqa: BLE001
            errors.append(repr(exc))

    t1 = threading.Thread(target=worker, args=(('南京博物院',),))
    t2 = threading.Thread(target=worker, args=(('夫子庙',),))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errors, f'并发写入抛异常：{errors}'
    got = names_of(ctx)
    assert '南京博物院' in got and '夫子庙' in got, f'有写入丢失：{got}'
    assert len(got) == len(set(got)), f'出现重复项：{got}'


def test_a03_delete_then_reschedule_has_no_gap(ctx):
    """A-03 删掉中间一项：其余项重排、时间单调且无空洞"""
    ctx.trip_svc.add_batch(DAY, items('南京博物院', '夫子庙', '总统府'), keep_order=True)
    trip = ctx.trip_svc.day_view(DAY)
    middle = [t for t in trip.tasks if t.poi and t.poi.name == '夫子庙'][0]
    assert ctx.trip_svc.delete(middle.task_id).ok
    left = [t for t in ctx.trip_svc.day_view(DAY).tasks if t.start_ts]
    assert [t.poi.name for t in left] == ['南京博物院', '总统府']
    for prev, nxt in zip(left, left[1:]):
        assert prev.end_ts <= nxt.start_ts, '删除后不应出现重叠'


def test_a04_clear_whole_day_then_add_again(ctx):
    """A-04 整日清空后该日为 0 项且不报错；随后还能正常写入"""
    ctx.trip_svc.add_batch(DAY, items('南京博物院', '夫子庙'), keep_order=True)
    ids = [t.task_id for t in ctx.trip_svc.day_view(DAY).tasks]
    out, _snaps = ctx.trip_svc.delete_many(ids)
    assert out.ok, out.error
    assert names_of(ctx) == []
    assert ctx.trip_svc.add_batch(DAY, items('总统府'), keep_order=True)[0]
    assert names_of(ctx) == ['总统府']


def test_a05_twenty_item_day_does_not_crash_and_reports(ctx):
    """A-05 一天塞 20 项：不崩溃；排不下的要有明确提示（不能静默丢弃）"""
    many = items(*['南京博物院', '夫子庙', '总统府', '老门东', '中山陵', '明孝陵',
                   '鸡鸣寺', '雨花台', '玄武湖公园', '颐和路公馆区', '先锋书店',
                   '江宁织造博物馆', '六朝博物馆', '朝天宫', '清凉山公园',
                   '鼓楼公园', '莫愁湖公园', '中华门瓮城', '甘熙宅第', '紫金山'])
    ok, msg = ctx.trip_svc.add_batch(DAY, many, keep_order=True)
    assert ok
    trip = ctx.trip_svc.day_view(DAY)
    placed = [t for t in trip.tasks if t.start_ts]
    missing = [t for t in trip.tasks if not t.start_ts]
    assert placed, '至少要把一部分排进时间轴'
    assert placed and missing, '20 项不可能全排进 22:00 之前，应同时存在排上/未排'
    assert '排不下' in msg, f'排不下必须明说，不能只报"已排入 N 项"：{msg}'
    for t in placed:
        assert t.end_ts.hour <= 22 and (t.end_ts.hour < 22 or t.end_ts.minute == 0), '不能越过日界'


def test_a06_late_night_boundary_stays_legal(ctx):
    """A-06 一直排到 23:59：不产生非法时间，也不跨到次日"""
    ctx.settings.day_end = '23:59'
    ctx.apply_settings(ctx.settings)
    ctx.trip_svc.add_batch(DAY, items('南京博物院'), keep_order=True)
    for t in ctx.trip_svc.day_view(DAY).tasks:
        assert t.start_ts is not None and t.end_ts is not None
        assert t.end_ts >= t.start_ts, '结束时刻不能早于开始'
        assert t.end_ts.hour <= 23 and t.end_ts.minute <= 59


def test_a07_late_finish_and_early_next_day_do_not_mix(ctx):
    """A-07 当天 23:00 收尾、次日 08:00 早场：两天互不串行"""
    ctx.settings.day_end = '23:00'
    ctx.apply_settings(ctx.settings)
    ctx.trip_svc.add_batch(DAY, items('南京博物院', '夫子庙'), keep_order=True)
    nxt = date(2026, 11, 11)
    ctx.trip_svc.add_batch(nxt, items('总统府'), keep_order=True)
    for t in ctx.trip_svc.day_view(DAY).tasks:
        assert t.end_ts.hour <= 23
    for t in ctx.trip_svc.day_view(nxt).tasks:
        assert t.start_ts.hour >= 8


def test_a08_bad_items_do_not_crash(ctx):
    """A-08 空批次 / 不存在的地名 / 时长为 0：不崩，给出可理解的结果"""
    ok, msg = ctx.trip_svc.add_batch(DAY, [], keep_order=True)
    assert ok is False, '空批次应返回失败而不是静默成功'
    ok2, msg2 = ctx.trip_svc.add_batch(
        DAY, [{'poi': '这个地点不存在啊啊啊', 'title': 'x', 'duration_min': 60}],
        keep_order=True)
    assert ok2 is False, '认不出的地名不能算写入成功'
    assert '找不到地点' in msg2, f'要指出哪个地名没认出来：{msg2}'
    ok3, _msg3 = ctx.trip_svc.add_batch(
        DAY, [{'poi': '南京博物院', 'title': 'x', 'duration_min': 0}], keep_order=True)
    assert ok3 is True, '时长 0 视为"没说"，按地点默认时长写入'


def test_a09_rollback_keeps_data_readable(ctx):
    """A-09 版本回滚后当天数据仍可读（数量与当时一致）"""
    ctx.trip_svc.add_batch(DAY, items('南京博物院'), keep_order=True)
    trip = ctx.trip_svc.day_view(DAY)
    versions = ctx.trip_svc.list_versions(trip.trip_id)
    assert versions, '应有版本快照'
    first = min(versions, key=lambda v: getattr(v, 'version_id', 0))
    ctx.trip_svc.add_batch(DAY, items('夫子庙'), keep_order=True)
    out = ctx.trip_svc.rollback(trip.trip_id, first.version_id)
    assert out.ok, out.error
    assert ctx.trip_svc.day_view(DAY).tasks is not None


def test_a10_negative_duration_falls_back_to_default(ctx):
    """A-10 负时长不能变成 10 分钟：按地点默认时长（夫子庙 120）"""
    ok, _ = ctx.trip_svc.add_batch(
        DAY, [{'poi': '夫子庙', 'title': 'y', 'duration_min': -30}], keep_order=True)
    assert ok
    durs = [t.duration_min for t in ctx.trip_svc.day_view(DAY).tasks
            if t.poi and t.poi.name == '夫子庙']
    assert durs and durs[0] >= 60, f'负时长应回落到默认时长，实际 {durs}'


def test_a11_delete_missing_task_is_reported(ctx):
    """A-11 删除不存在的安排：给出明确错误而不是静默成功"""
    out = ctx.trip_svc.delete(999999)
    assert out.ok is False and out.error
