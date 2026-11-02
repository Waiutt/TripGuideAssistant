# -*- coding: utf-8 -*-
"""逐段独立出行方式的测试（对应报告第 4 章 TC-19-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.engine import schedule
from nanjingtrip.models import (Poi, ScheduleOptions, Task, TaskStatus,
                                TransitMode)
from nanjingtrip.storage import Database, SqliteTripRepository

D = date(2026, 10, 28)


def _poi(name: str, pid: int) -> Poi:
    return Poi(name=name, district='玄武区', lng=118.8, lat=32.0,
               provider='mock', provider_poi_id=name, poi_id=pid)


def _opts() -> ScheduleOptions:
    return ScheduleOptions(mode=TransitMode.DRIVING, buffer_min=10,
                           day_start=time(8, 0), day_end=time(22, 0))


# ---------------------------------------------------------------- TC-19-01
def test_engine_uses_per_task_mode():
    """TC-19-01 排程引擎按"到达那一项"自己的方式算该段通行"""
    calls: list[tuple[str, str, str]] = []

    def transit(a, b, mode):
        calls.append((a.name, b.name, mode.value))
        # 公交比驾车慢一倍，便于区分
        return 40 if mode == TransitMode.TRANSIT else 20

    a = Task(title='A', duration_min=60, poi=_poi('P1', 1), is_locked=True,
             start_ts=time(9, 0))
    b = Task(title='B', duration_min=60, poi=_poi('P2', 2),
             mode=TransitMode.TRANSIT)
    result = schedule([a, b], _opts(), transit)

    assert any(c[2] == 'transit' for c in calls), 'B 段应使用公交方式'
    leg = result.ordered()[1].leg_before
    assert leg is not None and leg.mode == TransitMode.TRANSIT
    assert leg.duration_min == 40


# ---------------------------------------------------------------- TC-19-02
def test_storage_round_trip_mode(tmp_path):
    """TC-19-02 交通方式随事项持久化"""
    db = Database(tmp_path / 'm.db')
    repo = SqliteTripRepository(db)
    from nanjingtrip.models import Trip
    trip = Trip(plan_date=D)
    trip.add_task(Task(title='看展', duration_min=60, poi=_poi('P1', 1),
                       mode=TransitMode.WALKING, status=TaskStatus.SCHEDULED,
                       start_ts=time(9, 0), end_ts=time(10, 0)))
    repo.upsert_day(trip)
    loaded = repo.get_day(D)
    assert loaded.tasks[0].mode == TransitMode.WALKING
    db.close()


# ---------------------------------------------------------------- TC-19-03
def test_old_database_is_migrated(tmp_path):
    """TC-19-03 老库（无 mode 列）自动补列，不丢数据"""
    import sqlite3
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trip (trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_date TEXT NOT NULL UNIQUE, title TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE poi (poi_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL, provider_poi_id TEXT NOT NULL,
            name TEXT NOT NULL, address TEXT, district TEXT NOT NULL,
            lng REAL NOT NULL, lat REAL NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE task (task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL, title TEXT NOT NULL, category TEXT,
            duration_min INTEGER NOT NULL, desired_start TEXT, poi_id INTEGER,
            raw_text TEXT, is_locked INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unscheduled', seq INTEGER,
            start_ts TEXT, end_ts TEXT);
        INSERT INTO trip (plan_date, title, created_at, updated_at)
            VALUES ('2026-10-28', '老数据', 'x', 'x');
        INSERT INTO task (trip_id, title, duration_min, status)
            VALUES (1, '旧事项', 30, 'scheduled');
    """)
    conn.commit()
    conn.close()

    db = Database(path)                       # 触发迁移
    cols = {r['name'] for r in db.conn.execute('PRAGMA table_info(task)')}
    assert 'mode' in cols
    repo = SqliteTripRepository(db)
    loaded = repo.get_day(D)
    assert loaded is not None and loaded.tasks[0].title == '旧事项'
    assert loaded.tasks[0].mode is None       # 老数据默认跟随全局
    db.close()


# ---------------------------------------------------------------- TC-19-04
def test_adjust_sets_segment_mode_only(tmp_path):
    """TC-19-04 改某段方式后：该段生效、其他段方式不变"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)

    c.trip_svc.add_text('上午九点去南京博物院看展，大概1小时', D)
    out2 = c.trip_svc.add_text('中午去新街口吃饭，大约1小时', D)
    out3 = c.trip_svc.add_text('下午去鼓楼医院取报告，半小时', D)
    items = out3.result.ordered()
    assert len(items) == 3

    mid = next(i for i in items if i.task.title == '吃饭')
    res = c.trip_svc.adjust(mid.task.task_id, {'mode': 'walking'})
    assert res.ok
    after = {i.task.title: i for i in res.result.ordered()}
    assert after['吃饭'].leg_before.mode == TransitMode.WALKING
    # 第三段（取报告）不是被改的那一段，应仍跟随全局默认（驾车）
    if after['取报告'].leg_before:
        assert after['取报告'].leg_before.mode == TransitMode.DRIVING
    c.close()


# ---------------------------------------------------------------- TC-19-05
def test_parsed_mode_hint_sets_段_mode(tmp_path):
    """TC-19-05 "坐地铁去…"解析出的方式会写进该段的 Task.mode"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    out = c.trip_svc.preview_text('坐地铁去南京博物院看展，大概1小时', D)
    assert out.ok, out.error
    assert out.draft.mode == TransitMode.TRANSIT
    c.close()
