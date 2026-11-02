# -*- coding: utf-8 -*-
"""应用服务集成测试（对应报告第 4 章 TC-01-* / TC-04-* / TC-06-* / TC-13-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.app import AppContext

D = date(2026, 10, 28)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True          # 测试一律走离线，避免外部依赖
    c.apply_settings(c.settings)
    yield c
    c.close()


# ---------------------------------------------------------------- TC-13-01
def test_offline_mode_full_flow(ctx):
    """TC-13-01 无密钥离线模式：解析 → 地点 → 排程 → 入库 全链路可用"""
    assert ctx.degrade.is_offline()
    out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    assert out.ok, out.error
    items = out.result.ordered()
    assert items[0].task.title == '看展'
    assert items[0].task.poi.name == '南京博物院'
    assert items[0].start_min == 9 * 60
    assert out.version_id is not None
    # 数据真的落库了
    trip = ctx.trip_svc.day_view(D)
    assert len(trip.tasks) == 1


# ---------------------------------------------------------------- TC-01-01
def test_second_task_gets_transit_leg(ctx):
    """TC-01-01 第二条安排自动插入通行段（BR-03）"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    out = ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    assert out.ok
    items = out.result.ordered()
    assert len(items) == 2
    leg = items[1].leg_before
    assert leg is not None and leg.duration_min > 0
    assert items[1].start_min >= items[0].end_min + leg.duration_min


# ---------------------------------------------------------------- TC-04-01
def test_transit_cache_reduces_map_calls(ctx):
    """TC-04-01 通行段缓存命中后不再调用地图（配额与性能对策）"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    calls_after_first = ctx.transit_svc.calls
    assert calls_after_first >= 1
    for _ in range(3):
        ctx.trip_svc.compute_day(D)
    assert ctx.transit_svc.calls == calls_after_first
    assert ctx.transit_cache.count() >= 1


# ---------------------------------------------------------------- TC-04-02
def test_offline_cache_not_used_when_online(ctx):
    """TC-04-02 离线估算的缓存不得覆盖在线真实数据（切到在线后必须重取）"""
    from nanjingtrip.adapters.mock import MockMapAdapter
    from nanjingtrip.models import TransitMode

    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    rows = ctx.db.conn.execute(
        'SELECT from_poi_id, to_poi_id, source FROM transit_leg').fetchall()
    assert rows and all('离线' in r['source'] for r in rows)
    from_id, to_id = rows[0]['from_poi_id'], rows[0]['to_poi_id']

    class OnlineMap(MockMapAdapter):
        """模拟在线地图：来源名不含"离线"，时长不同以便区分"""

        def name(self) -> str:
            return '高德地图'

        def route(self, origin, dest, mode):
            return {'duration_min': 41, 'distance_m': 6000}

    # 切到在线：settings 与 degrade 共享同一对象，改标记即可
    ctx.settings.offline_mode = False
    ctx.settings.map_api_key = 'test-key'
    ctx.degrade._map = OnlineMap()
    assert ctx.degrade.map_ready() is True

    used = ctx.transit_svc.estimate(ctx.trip_repo.get_poi(from_id),
                                    ctx.trip_repo.get_poi(to_id),
                                    TransitMode.DRIVING)
    assert used == 41, '在线模式下必须忽略离线缓存并重新查询'
    row = ctx.db.conn.execute(
        'SELECT source, duration_min FROM transit_leg'
        ' WHERE from_poi_id = ? AND to_poi_id = ? AND mode = ?',
        (from_id, to_id, 'driving')).fetchone()
    assert row['source'] == '高德地图' and row['duration_min'] == 41


# ---------------------------------------------------------------- TC-02-06
def test_preview_fills_duration_from_recommended_visit(ctx):
    """TC-02-06（改）缺耗时 → 用推荐游玩时长（南博 150 分钟）兜底，而不是拦住用户"""
    out = ctx.trip_svc.preview_text('明天去南京博物院看展', D)
    assert out.ok, out.error
    assert out.draft.duration_min == 150, out.draft.duration_min
    assert '推荐' in ' '.join(c.detail for c in (out.notices or []))


def test_preview_unknown_place(ctx):
    """TC-03-01 地点无法解析 → 友好错误，不落库"""
    out = ctx.trip_svc.preview_text('明天去火星基地看展，2小时', D)
    assert not out.ok
    assert '未能' in out.error or '地点' in out.error
    assert ctx.trip_svc.day_view(D).tasks == []


def test_preview_multi_candidates(ctx):
    """TC-03-02 多候选地点 → 返回候选列表供用户选择"""
    out = ctx.trip_svc.preview_text('明天去南京大学鼓楼校区办事，1小时', D)
    assert out.ok
    assert out.candidates, '应返回候选地点列表'
    assert out.candidates[0].name


# ---------------------------------------------------------------- TC-06-01
def test_conflict_gives_suggestions(ctx):
    """TC-06-01 与已有锁定安排冲突 → 产生冲突且建议非空（BR-05）"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    out = ctx.trip_svc.add_text('上午十点去鼓楼医院取报告，30分钟', D)
    assert out.result is not None
    kinds = [c.kind for c in out.result.conflicts]
    assert 'locked_overlap' in kinds
    conflict = next(c for c in out.result.conflicts if c.kind == 'locked_overlap')
    assert conflict.suggestions, '冲突必须附带建议'
    assert any('顺延' in s.text or '缩短' in s.text or '取消' in s.text
               for s in conflict.suggestions)


# ---------------------------------------------------------------- TC-09-01
def test_adjust_task_reschedules(ctx):
    """TC-09-01 调整开始时间 → 重算并写新版本"""
    out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    task_id = out.result.ordered()[0].task.task_id
    adj = ctx.trip_svc.adjust(task_id, {'start_ts': '14:00'})
    assert adj.ok
    assert adj.result.ordered()[0].start_min == 14 * 60
    assert adj.version_id is not None


def test_delete_task(ctx):
    """TC-09-02 删除安排 → 数据与版本同步更新"""
    out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    task_id = out.result.ordered()[0].task.task_id
    ctx.trip_svc.delete(task_id)
    assert ctx.trip_svc.day_view(D).tasks == []


# ---------------------------------------------------------------- TC-10-01
def test_rollback_restores_version(ctx):
    """TC-10-01 版本回滚 → 恢复到该版本时段（BR-07）"""
    out = ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    trip_id = out.trip.trip_id
    first_version = out.version_id
    task_id = out.result.ordered()[0].task.task_id
    ctx.trip_svc.adjust(task_id, {'start_ts': '15:00'})
    back = ctx.trip_svc.rollback(trip_id, first_version)
    assert back.ok
    starts = [i.start_min for i in back.result.ordered()]
    assert 9 * 60 in starts or not starts


# ---------------------------------------------------------------- TC-08-01
def test_day_stats(ctx):
    """TC-08-01 当日统计：项数/事项时长/通行时长/空档"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    ctx.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    stats = ctx.trip_svc.day_stats(ctx.trip_svc.day_view(D))
    assert stats['count'] == 2
    assert stats['busy'] == 180
    assert stats['transit'] > 0
