# -*- coding: utf-8 -*-
"""数据治理测试：缓存裁剪、WAL 截断与维护幂等（对应报告 4.10 补充）。"""
from datetime import datetime, timedelta

import pytest

from nanjingtrip.storage import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / 'x.db')
    yield d
    d.close()


def _poi(db, poi_id: int, name: str) -> None:
    db.conn.execute(
        'INSERT OR REPLACE INTO poi (poi_id, provider, provider_poi_id, name,'
        ' address, district, city, lng, lat, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (poi_id, 'mock', name, name, '', '玄武区', '南京', 118.8, 32.05, ''))
    db.conn.commit()


def _leg(db, fid: int, tid: int, fetched_at: str, mode: str = 'driving') -> None:
    _poi(db, fid, f'起点{fid}')
    _poi(db, tid, f'终点{tid}')
    db.conn.execute(
        'INSERT OR REPLACE INTO transit_leg (from_poi_id, to_poi_id, mode,'
        ' duration_min, distance_m, source, fetched_at) VALUES (?,?,?,?,?,?,?)',
        (fid, tid, mode, 12, 3400, 'amap', fetched_at))
    db.conn.commit()


def test_stats_reports_counts_and_sizes(tmp_path):
    """统计口径可直接用于界面/报告：条数 + 库与 WAL 体积"""
    d = Database(tmp_path / 'y.db')
    try:
        _leg(d, 1, 2, datetime.now().isoformat(timespec='seconds'))
        st = d.stats()
        assert st['transit_leg'] == 1
        assert st['db_bytes'] > 0 and st['wal_bytes'] >= 0
    finally:
        d.close()


def test_maintain_prunes_expired_cache(db):
    """超期通行缓存要被清掉，最近的必须留着"""
    old = (datetime.now() - timedelta(days=Database.CACHE_KEEP_DAYS + 10)
           ).isoformat(timespec='seconds')
    new = datetime.now().isoformat(timespec='seconds')
    _leg(db, 1, 2, old)
    _leg(db, 3, 4, new)
    report = db.maintain()
    assert report['transit_pruned'] == 1
    left = db.conn.execute('SELECT from_poi_id AS f FROM transit_leg').fetchall()
    assert [r['f'] for r in left] == [3]


def test_maintain_enforces_row_cap(db, monkeypatch):
    """条目超上限时按取数时间从旧到新清（保留最新的那批）"""
    monkeypatch.setattr(Database, 'CACHE_MAX_ROWS', 3)
    base = datetime.now()
    for i in range(6):
        _leg(db, i + 1, i + 2, (base - timedelta(minutes=6 - i)).isoformat(timespec='seconds'))
    db.maintain()
    rows = db.conn.execute(
        'SELECT from_poi_id AS f FROM transit_leg ORDER BY fetched_at').fetchall()
    assert len(rows) == 3
    assert [r['f'] for r in rows] == [4, 5, 6], '应保留最新的三条'


def test_maintain_is_idempotent_and_safe_on_empty_db(db):
    """空库/重复调用都要安全，返回值形状稳定"""
    first = db.maintain()
    second = db.maintain()
    assert first['transit_pruned'] == 0 and second['transit_pruned'] == 0
    assert set(first) == set(second) == {'transit_pruned', 'intel_pruned',
                                         'checkpointed', 'vacuumed'}


def test_maintain_checkpoints_wal(db):
    """强制 checkpoint 要把 WAL 截断（真实库里它曾涨到 4 MB 从没截断过）"""
    for i in range(200):
        _leg(db, i + 1, i + 2, datetime.now().isoformat(timespec='seconds'))
    before = db.stats()['wal_bytes']
    report = db.maintain(force_checkpoint=True)
    after = db.stats()['wal_bytes']
    assert report['checkpointed'] is True
    assert after <= before, f'WAL 未变小：{before} → {after}'


def test_startup_maintenance_does_not_break_app(tmp_path):
    """装配层启动维护不能挡住启动（失败也只记日志）"""
    from nanjingtrip.app import AppContext
    ctx = AppContext(tmp_path)
    try:
        assert ctx.db.stats()['task'] == 0
        assert ctx.trip_svc.day_view(datetime.now().date()) is not None
    finally:
        ctx.close()
