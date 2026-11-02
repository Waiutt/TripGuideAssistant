# -*- coding: utf-8 -*-
"""城市包与多城市支持测试（对应报告第 4 章 TC-25-*）。

覆盖：城市包注册表、BR-01 作用域参数化、跨城拒绝、同日多城互不干扰、
POI 营业信息"不落库但从城市包补全"、老库迁移。
"""
import sqlite3
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.cities import (BEIJING_PACK, CITY_PACKS, DEFAULT_CITY,
                                get_city, normalize_city)
from nanjingtrip.models import Poi

D = date(2026, 10, 28)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


# ---------------------------------------------------------------- TC-25-01
def test_registry_has_two_cities():
    """TC-25-01 城市包注册表：南京为默认城市，北京已注册"""
    assert DEFAULT_CITY == '南京'
    assert set(CITY_PACKS) == {'南京', '北京'}
    assert normalize_city('不存在的城市') == '南京'      # 未知城市回落，不抛异常
    assert normalize_city('北京') == '北京'


# ---------------------------------------------------------------- TC-25-02
def test_every_poi_passes_district_check():
    """TC-25-02 每个 POI 的区县都在本城市行政区列表内（BR-01 自洽）"""
    for name, pack in CITY_PACKS.items():
        bad = [p['name'] for p in pack.pois
               if not pack.in_city(p.get('district', ''))]
        assert bad == [], f'{name} 有 POI 区县不属于本市：{bad}'


# ---------------------------------------------------------------- TC-25-03
@pytest.mark.parametrize('city', ['南京', '北京'])
def test_city_covers_eat_live_play_go(city):
    """TC-25-03 吃/住/玩/行四类在每座城市都有数据（不是只有景点）"""
    pack = get_city(city)
    kinds = {}
    for p in pack.pois:
        kinds[p.get('kind', '')] = kinds.get(p.get('kind', ''), 0) + 1
    assert kinds.get('餐饮', 0) >= 5, '吃'
    assert kinds.get('住宿', 0) >= 3, '住'
    assert kinds.get('景点', 0) + kinds.get('场馆', 0) >= 5, '玩'
    assert kinds.get('交通', 0) >= 2, '行'


# ---------------------------------------------------------------- TC-25-04
def test_beijing_suggests_transit_by_default():
    """TC-25-04 北京包默认公交地铁（地铁覆盖好），南京包默认驾车"""
    assert BEIJING_PACK.default_mode == 'transit'
    assert get_city('南京').default_mode == 'driving'


# ---------------------------------------------------------------- TC-25-05
def test_alias_resolution_per_city():
    """TC-25-05 别名按城市解析：'故宫'→故宫博物院，且不会跨城命中"""
    assert get_city('北京').find('故宫')['name'] == '故宫博物院'
    assert get_city('北京').find('国博')['name'] == '中国国家博物馆'
    assert get_city('南京').find('故宫') is None
    assert get_city('南京').find('南博')['name'] == '南京博物院'


# ---------------------------------------------------------------- TC-25-06
def test_cross_city_poi_rejected_in_nanjing(ctx):
    """TC-25-06 南京模式下解析北京地点：给出可执行的跨城提示，不落库"""
    out = ctx.trip_svc.preview_text('上午九点去故宫看展，大概3小时', D)
    assert out.ok is False
    assert '南京' in out.error and '故宫' in out.error


# ---------------------------------------------------------------- TC-25-07
def test_switch_to_beijing_then_parse(ctx):
    """TC-25-07 切到北京后可解析故宫，且带出营业/预约信息"""
    ctx.set_city('北京')
    assert ctx.city == '北京'
    out = ctx.trip_svc.preview_text('上午九点去故宫看展，大概3小时', D)
    assert out.ok, out.error
    poi = out.draft.poi
    assert poi.name == '故宫博物院'
    assert poi.city == '北京'
    assert poi.closed_days == ('周一',)
    assert '预约' in poi.book_hint
    assert any(c.kind == 'need_booking' for c in out.notices)


# ---------------------------------------------------------------- TC-25-08
def test_beijing_switch_changes_default_mode(ctx):
    """TC-25-08 切城市时默认出行方式跟随城市包（用户改过则不覆盖）"""
    assert ctx.settings.transit_mode == 'driving'
    ctx.set_city('北京')
    assert ctx.settings.transit_mode == 'transit'
    ctx.settings.transit_mode = 'bicycling'          # 用户手动改过
    ctx.set_city('南京')
    assert ctx.settings.transit_mode == 'bicycling'  # 不被覆盖


# ---------------------------------------------------------------- TC-25-09
def test_poi_meta_rehydrated_after_reload(ctx):
    """TC-25-09 营业信息只存城市包：从库里读回时按名称补全（否则闭馆判断会失效）"""
    ctx.set_city('北京')
    out = ctx.trip_svc.add_text('上午九点去故宫看展，大概3小时', D)
    assert out.ok, out.error
    reloaded = ctx.trip_svc.day_view(D).tasks[0].poi
    assert reloaded.name == '故宫博物院'
    assert reloaded.kind == '景点'
    assert reloaded.closed_days == ('周一',)
    assert reloaded.open_from == '08:30'


# ---------------------------------------------------------------- TC-25-10
def test_same_date_two_cities_isolated(ctx):
    """TC-25-10 同一天在北京与南京各有一份行程，互不干扰"""
    ctx.set_city('北京')
    assert ctx.trip_svc.add_text('上午九点去故宫看展，大概3小时', D).ok
    assert len(ctx.trip_svc.day_view(D).tasks) == 1

    ctx.set_city('南京')
    assert len(ctx.trip_svc.day_view(D).tasks) == 0        # 南京当天是空的
    assert ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D).ok
    assert len(ctx.trip_svc.day_view(D).tasks) == 1

    ctx.set_city('北京')
    tasks = ctx.trip_svc.day_view(D).tasks
    assert len(tasks) == 1 and tasks[0].poi.name == '故宫博物院'


# ---------------------------------------------------------------- TC-25-11
def test_old_database_gains_city_column(tmp_path):
    """TC-25-11 老库（trip 无 city 列、plan_date 单列唯一）自动迁移后可存两城"""
    path = tmp_path / 'nanjingtrip.db'          # AppContext 固定使用该库名
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trip (
            trip_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_date   TEXT NOT NULL UNIQUE,
            title       TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        INSERT INTO trip (plan_date, title, created_at, updated_at)
            VALUES ('2026-10-28', '老行程', 'x', 'x');
    """)
    conn.commit()
    conn.close()

    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    try:
        cols = {r[1] for r in c.db.conn.execute('PRAGMA table_info(trip)')}
        assert {'city', 'lodging_poi_id', 'lodging_declined'} <= cols
        # 老数据保留，并落在默认城市南京下
        nj = c.trip_svc.day_view(D)
        assert nj.title == '老行程' and nj.city == '南京'
        # 同一天可以再有一份北京行程
        c.set_city('北京')
        assert c.trip_svc.add_text('上午九点去故宫看展，大概3小时', D).ok
        ctx2 = c.trip_svc.day_view(D)
        assert ctx2.city == '北京' and len(ctx2.tasks) == 1
    finally:
        c.close()


# ---------------------------------------------------------------- TC-25-12
def test_poi_in_city_between_cities():
    """TC-25-12 Poi.in_city 按城市判定：北京地点在南京作用域下为 False"""
    bj = Poi.from_pack(get_city('北京').find('天坛公园'), '北京')
    assert bj.in_city('北京') is True
    assert bj.in_city('南京') is False
    nj = Poi.from_pack(get_city('南京').find('玄武湖公园'), '南京')
    assert nj.in_city('南京') is True
    assert nj.in_city('北京') is False


# ======================================================================
# 以下三条用于证明：**核心用例在新增城市（北京）上同样可用**
# 新增城市是覆盖面扩展，不是新增功能，因此核心用例必须逐条成立
# ======================================================================
# ---------------------------------------------------------------- TC-25-13
def test_core_uc08_adjust_works_in_new_city(ctx):
    """TC-25-13 UC-08 调整行程在北京同样生效（改时间 + 锁定）"""
    ctx.set_city('北京')
    out = ctx.trip_svc.add_text('上午九点去天坛公园逛逛，大概2小时', D)
    assert out.ok, out.error
    task = ctx.trip_svc.day_view(D).tasks[0]

    adj = ctx.trip_svc.adjust(task.task_id, {'start_ts': '14:00'})
    assert adj.result is not None
    moved = ctx.trip_svc.day_view(D).tasks[0]
    assert moved.start_ts.strftime('%H:%M') == '14:00'
    assert moved.is_locked is True


# ---------------------------------------------------------------- TC-25-14
def test_core_uc09_delete_works_in_new_city(ctx):
    """TC-25-14 UC-09 删除安排在北京同样生效，且不影响南京那份行程"""
    ctx.set_city('南京')
    assert ctx.trip_svc.add_text('上午九点去玄武湖公园散步，大概1小时', D).ok
    ctx.set_city('北京')
    assert ctx.trip_svc.add_text('上午十点去天坛公园逛逛，大概2小时', D).ok
    bj_task = ctx.trip_svc.day_view(D).tasks[0]

    out, snaps = ctx.trip_svc.delete_many([bj_task.task_id])
    assert out.ok and snaps
    assert ctx.trip_svc.day_view(D).tasks == []          # 北京当天清空

    ctx.set_city('南京')
    assert len(ctx.trip_svc.day_view(D).tasks) == 1      # 南京不受影响


# ---------------------------------------------------------------- TC-25-15
def test_core_uc10_rollback_works_in_new_city(ctx):
    """TC-25-15 UC-10 版本回滚在北京同样生效（版本按城市隔离）"""
    ctx.set_city('北京')
    assert ctx.trip_svc.add_text('上午九点去天坛公园逛逛，大概2小时', D).ok
    trip = ctx.trip_svc.day_view(D)
    task = trip.tasks[0]
    assert task.start_ts.strftime('%H:%M') == '09:00'

    ctx.trip_svc.adjust(task.task_id, {'start_ts': '14:00'})
    assert ctx.trip_svc.day_view(D).tasks[0].start_ts.strftime('%H:%M') == '14:00'

    versions = ctx.trip_svc.list_versions(trip.trip_id)
    assert len(versions) >= 2, '应有新增与调整两个版本'
    first = versions[-1]                                  # 最早的那个版本
    back = ctx.trip_svc.rollback(trip.trip_id, first.version_id)
    assert back.result is not None
    assert ctx.trip_svc.day_view(D).tasks[0].start_ts.strftime('%H:%M') == '09:00'
