# -*- coding: utf-8 -*-
"""仓储与配置的测试（对应报告第 4 章 TC-07-* / TC-12-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.config import Settings, SettingsService
from nanjingtrip.models import Poi, Task, TaskStatus, Trip, TransitMode
from nanjingtrip.storage import (Database, SqliteTransitCache,
                                 SqliteTripRepository, SqliteVersionRepository)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / 'test.db')
    yield database
    database.close()


# ---------------------------------------------------------------- TC-07-01
def test_trip_round_trip(db):
    """TC-07-01 行程/事项/地点写入后读出一致（关开应用数据不丢）"""
    repo = SqliteTripRepository(db)
    trip = Trip(plan_date=date(2026, 10, 28))
    poi = Poi(name='南京博物院', address='中山东路321号', district='玄武区',
              lng=118.8266, lat=32.0433, provider='mock',
              provider_poi_id='南京博物院')
    trip.add_task(Task(title='看展', duration_min=120, poi=poi,
                       desired_start=time(9, 0), is_locked=True,
                       category='展览', raw_text='上午九点去南京博物院看展',
                       status=TaskStatus.SCHEDULED, seq=1,
                       start_ts=time(9, 0), end_ts=time(11, 0)))
    trip_id = repo.upsert_day(trip)
    assert trip_id > 0

    loaded = repo.get_day(date(2026, 10, 28))
    assert loaded is not None
    assert len(loaded.tasks) == 1
    t = loaded.tasks[0]
    assert t.title == '看展'
    assert t.duration_min == 120
    assert t.poi is not None and t.poi.name == '南京博物院'
    assert t.poi.district == '玄武区' and t.poi.in_nanjing()
    assert t.is_locked is True
    assert t.start_ts == time(9, 0) and t.end_ts == time(11, 0)


def test_get_day_returns_none_for_empty_date(db):
    repo = SqliteTripRepository(db)
    assert repo.get_day(date(2030, 1, 1)) is None


# ---------------------------------------------------------------- TC-07-02
def test_transit_cache_upsert_unique(db):
    """TC-07-02 通行段缓存唯一键（同起点终点方式只留一条，后写覆盖）"""
    repo = SqliteTripRepository(db)
    p1 = Poi(name='南京博物院', district='玄武区', provider='mock',
             provider_poi_id='南京博物院')
    p2 = Poi(name='新街口', district='秦淮区', provider='mock',
             provider_poi_id='新街口')
    id1 = repo.ensure_poi(p1)
    id2 = repo.ensure_poi(p2)
    assert id1 and id2

    cache = SqliteTransitCache(db)
    cache.put(id1, id2, TransitMode.DRIVING.value, 25, 8000, '高德地图')
    cache.put(id1, id2, TransitMode.DRIVING.value, 31, 8100, '高德地图')
    assert cache.get(id1, id2, TransitMode.DRIVING.value) == 31
    assert cache.count() == 1
    # 不同交通方式互不影响
    cache.put(id1, id2, TransitMode.WALKING.value, 60, 4500, '高德地图')
    assert cache.count() == 2
    assert cache.get(id1, id2, TransitMode.WALKING.value) == 60


# ---------------------------------------------------------------- TC-07-03
def test_version_keep_limit(db):
    """TC-07-03 版本保留上限（BR-07 只留最近 N 个）"""
    repo = SqliteTripRepository(db)
    trip = Trip(plan_date=date(2026, 10, 29))
    trip.add_task(Task(title='事项', duration_min=30))
    tid = repo.upsert_day(trip)
    ver_repo = SqliteVersionRepository(db, keep=3)
    for i in range(6):
        ver_repo.add(tid, f'v{i}', '{"items": []}')
    assert len(ver_repo.list(tid)) == 3


# ---------------------------------------------------------------- TC-12-01
def test_settings_round_trip(tmp_path):
    """TC-12-01 配置保存与读取（密钥与配置分离存储）"""
    svc = SettingsService(tmp_path)
    s = Settings(llm_provider='DeepSeek', llm_model='deepseek-chat',
                 llm_api_key='sk-test-123', map_api_key='amap-test-456',
                 transit_mode='transit', buffer_min=15, offline_mode=False)
    svc.save(s)
    assert (tmp_path / 'config.json').exists()
    assert (tmp_path / 'secrets.json').exists()
    # 密钥不得出现在 config.json 中（NFR-05）
    assert 'sk-test-123' not in (tmp_path / 'config.json').read_text(encoding='utf-8')
    loaded = svc.load()
    assert loaded.llm_api_key == 'sk-test-123'
    assert loaded.map_api_key == 'amap-test-456'
    assert loaded.buffer_min == 15
    assert loaded.transit_mode == 'transit'


# ---------------------------------------------------------------- TC-12-02
def test_effective_offline_when_key_missing(tmp_path):
    """TC-12-02 缺密钥时自动离线（FR-13）"""
    svc = SettingsService(tmp_path)
    s = Settings(offline_mode=False, llm_api_key='', map_api_key='')
    assert s.effective_offline is True
    s2 = Settings(offline_mode=False, llm_api_key='k1', map_api_key='k2')
    assert s2.effective_offline is False


# ---------------------------------------------------------------- TC-12-03
def test_partial_online_map_only():
    """TC-12-03 两路独立降级：只填地图 Key → 地图在线、解析仍离线"""
    from nanjingtrip.adapters.amap import AmapAdapter
    from nanjingtrip.adapters.mock import MockLLMAdapter, MockMapAdapter
    from nanjingtrip.services import DegradeController

    s = Settings(offline_mode=False, llm_api_key='', map_api_key='amap-key')
    assert s.map_ready is True and s.llm_ready is False
    assert s.effective_offline is True          # 非"全在线"

    deg = DegradeController(s)
    assert isinstance(deg.llm(), MockLLMAdapter)     # 解析走离线规则
    assert isinstance(deg.map(), AmapAdapter)        # 地图走真实接口
    assert deg.is_offline() is False                 # 属于"半在线"
    assert '半在线' in deg.mode_label()

    # 只填大模型 Key 时对称成立
    s2 = Settings(offline_mode=False, llm_api_key='sk', map_api_key='')
    deg2 = DegradeController(s2)
    assert isinstance(deg2.map(), MockMapAdapter)
    assert deg2.map_ready() is False and deg2.llm_ready() is True


# ---------------------------------------------------------------- TC-12-04
def test_db_waits_instead_of_locked(tmp_path):
    """TC-12-04 两个进程同开一个库时要「等」而不是立刻报 database is locked

    用户双击两次启动脚本就会开两个实例。sqlite3 默认只等 5s，且这期间若前一个
    进程正在写就立刻抛 OperationalError；这里把等待时间落到 15s，让第二个实例
    排队而不是崩在启动期。
    """
    from nanjingtrip.storage import BUSY_TIMEOUT_S, Database

    assert BUSY_TIMEOUT_S == 15
    a = Database(tmp_path / 'x.db')
    # 两个实例同开同一个库不应在启动期就崩（等待策略不影响正常路径）
    b = Database(tmp_path / 'x.db')
    assert a.conn.execute('SELECT COUNT(*) FROM trip').fetchone()[0] == 0
    assert b.conn.execute('SELECT COUNT(*) FROM trip').fetchone()[0] == 0
    a.close()
    b.close()
