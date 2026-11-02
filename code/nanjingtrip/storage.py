# -*- coding: utf-8 -*-
"""基础设施层：SQLite 仓储实现（6 张表，见第 2 章表 2.1～2.6）。

所有 SQL 均使用参数化查询（NFR-06），事务边界由调用方控制。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from .models import (Poi, Task, TaskStatus, Trip, TripVersion, TransitMode)

# 库忙时的等待秒数：用户重复双击启动脚本会开两个进程，第二个进程应当排队等待
# 前一个释放，而不是在启动期就抛 "database is locked"（sqlite 默认只等 5s）。
BUSY_TIMEOUT_S = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS trip (
    trip_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date   TEXT NOT NULL,
    city        TEXT NOT NULL DEFAULT '南京',
    title       TEXT,
    lodging_poi_id INTEGER REFERENCES poi(poi_id) ON DELETE SET NULL,
    lodging_declined INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS poi (
    poi_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,
    provider_poi_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    address         TEXT,
    district        TEXT NOT NULL,
    city            TEXT NOT NULL DEFAULT '',
    lng             REAL NOT NULL,
    lat             REAL NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_poi_provider ON poi(provider, provider_poi_id);

CREATE TABLE IF NOT EXISTS task (
    task_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id       INTEGER NOT NULL REFERENCES trip(trip_id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    category      TEXT,
    duration_min  INTEGER NOT NULL CHECK (duration_min > 0),
    desired_start TEXT,
    poi_id        INTEGER REFERENCES poi(poi_id) ON DELETE SET NULL,
    raw_text      TEXT,
    is_locked     INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
    status        TEXT NOT NULL DEFAULT 'unscheduled',
    seq           INTEGER,
    start_ts      TEXT,
    end_ts        TEXT,
    mode          TEXT,
    deadline      TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_trip ON task(trip_id);
CREATE INDEX IF NOT EXISTS idx_task_poi ON task(poi_id);

CREATE TABLE IF NOT EXISTS transit_leg (
    leg_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    from_poi_id   INTEGER NOT NULL REFERENCES poi(poi_id),
    to_poi_id     INTEGER NOT NULL REFERENCES poi(poi_id),
    mode          TEXT NOT NULL,
    duration_min  INTEGER NOT NULL CHECK (duration_min > 0),
    distance_m    INTEGER,
    source        TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    UNIQUE (from_poi_id, to_poi_id, mode)
);

CREATE TABLE IF NOT EXISTS trip_version (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id       INTEGER NOT NULL REFERENCES trip(trip_id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    note          TEXT,
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_trip ON trip_version(trip_id, created_at);

CREATE TABLE IF NOT EXISTS app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _t2s(t: Optional[time]) -> Optional[str]:
    return t.strftime('%H:%M') if t else None


def _s2t(s: Optional[str]) -> Optional[time]:
    if not s:
        return None
    try:
        h, m = s.split(':')[:2]
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


class Database:
    """SQLite 连接与 schema 管理（支持局域网服务线程并发访问）"""

    REV_KEY = 'app.rev'

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ('', '.'):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # 局域网服务在独立线程里读写同一个库，因此关闭线程检查，并用可重入锁串行化写入；
        # timeout 给足 15s：用户重复双击启动脚本（两个进程同开一个库）时，等前一个释放，
        # 而不是立刻抛 "database is locked"。
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                    timeout=BUSY_TIMEOUT_S)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode = WAL')
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate()
            self.conn.commit()

    #: 通行缓存保留天数（真实路况会变，太老的时长没有参考价值）
    CACHE_KEEP_DAYS = 120
    #: 通行缓存条目上限（超出时按取数时间从旧到新清）
    CACHE_MAX_ROWS = 60000
    #: 情报（开放时间/票价/评分）保留天数
    INTEL_KEEP_DAYS = 45
    #: WAL 文件超过这个体积才截断（小库不必每次维护）
    WAL_LIMIT_BYTES = 8 * 1024 * 1024
    #: 主库超过这个体积才做 VACUUM（VACUUM 会锁库，不能勤做）
    VACUUM_MIN_BYTES = 64 * 1024 * 1024

    def _file_bytes(self, suffix: str = '') -> int:
        try:
            return (self.path.parent / (self.path.name + suffix)).stat().st_size
        except OSError:
            return 0

    def stats(self) -> dict:
        """当前库的规模快照（界面/报告/维护脚本都用它）"""
        def count(table: str) -> int:
            try:
                row = self.conn.execute(f'SELECT COUNT(*) AS c FROM {table}').fetchone()
                return int(row['c'] if row else 0)
            except sqlite3.Error:
                return 0
        return {'db_bytes': self._file_bytes(), 'wal_bytes': self._file_bytes('-wal'),
                'trip': count('trip'), 'task': count('task'), 'poi': count('poi'),
                'transit_leg': count('transit_leg'), 'trip_version': count('trip_version'),
                'poi_intel': count('poi_intel')}

    def maintain(self, force_checkpoint: bool = False) -> dict:
        """日常维护：清过期缓存 + 截断 WAL + （库很大时）VACUUM。

        设计成**便宜且幂等**：启动时随手调一次，未超阈值就几乎不做事；
        真实数据里 WAL 曾涨到 4 MB 而从没截断过，切日期自然越来越慢。
        """
        report = {'transit_pruned': 0, 'intel_pruned': 0,
                  'checkpointed': False, 'vacuumed': False}
        cutoff = (datetime.now() - timedelta(days=self.CACHE_KEEP_DAYS)
                  ).isoformat(timespec='seconds')
        intel_cutoff = (datetime.now() - timedelta(days=self.INTEL_KEEP_DAYS)
                        ).isoformat(timespec='seconds')
        with self.lock, self.transaction() as conn:
            cur = conn.execute(
                'DELETE FROM transit_leg WHERE fetched_at != "" AND fetched_at < ?',
                (cutoff,))
            report['transit_pruned'] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            total = conn.execute('SELECT COUNT(*) AS c FROM transit_leg').fetchone()['c']
            if total > self.CACHE_MAX_ROWS:
                # 只留最新的 CACHE_MAX_ROWS 条（按取数时间，其次按写入顺序）
                cur = conn.execute(
                    'DELETE FROM transit_leg WHERE leg_id NOT IN ('
                    '  SELECT leg_id FROM transit_leg'
                    '  ORDER BY fetched_at DESC, leg_id DESC LIMIT ?)',
                    (self.CACHE_MAX_ROWS,))
                report['transit_pruned'] += max(0, cur.rowcount or 0)
            try:
                cur = conn.execute(
                    'DELETE FROM poi_intel WHERE fetched_at != "" AND fetched_at < ?',
                    (intel_cutoff,))
                report['intel_pruned'] = max(0, cur.rowcount or 0)
            except sqlite3.Error:
                report['intel_pruned'] = 0
        # WAL 截断：放在写事务之外，避免锁等待
        if force_checkpoint or self._file_bytes('-wal') > self.WAL_LIMIT_BYTES:
            with self.lock:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                self.conn.commit()
            report['checkpointed'] = True
        if self._file_bytes() > self.VACUUM_MIN_BYTES:
            with self.lock:
                self.conn.execute('VACUUM')
                self.conn.commit()
            report['vacuumed'] = True
        return report

    # ---------- 轻量迁移 ----------
    def _migrate(self) -> None:
        """老库升级：只补列、只重建必要的表，绝不丢数据。

        v1.0 → v1.2 的两次演进：
          * ``task.mode``：逐段独立出行方式；
          * ``trip.city`` / ``poi.city`` / ``trip.lodging_poi_id``：多城市与住宿锚点。
        其中 ``trip.plan_date`` 早期是**内联 UNIQUE**，无法容纳"同一天在不同城市各有一份行程"，
        因此该约束存在时必须重建 trip 表（其余情况只 ALTER ADD COLUMN）。
        """
        task_cols = {r['name'] for r in
                     self.conn.execute('PRAGMA table_info(task)').fetchall()}
        if 'mode' not in task_cols:
            self.conn.execute('ALTER TABLE task ADD COLUMN mode TEXT')
        if 'deadline' not in task_cols:
            # 绝对截止（"10:30 前必须抵达"）；老库补列即可，不影响既有数据
            self.conn.execute('ALTER TABLE task ADD COLUMN deadline TEXT')

        poi_cols = {r['name'] for r in
                    self.conn.execute('PRAGMA table_info(poi)').fetchall()}
        if 'city' not in poi_cols:
            self.conn.execute(
                "ALTER TABLE poi ADD COLUMN city TEXT NOT NULL DEFAULT ''")

        if self._trip_needs_rebuild():
            self._rebuild_trip()
            return
        trip_cols = {r['name'] for r in
                     self.conn.execute('PRAGMA table_info(trip)').fetchall()}
        if 'city' not in trip_cols:
            self.conn.execute(
                "ALTER TABLE trip ADD COLUMN city TEXT NOT NULL DEFAULT '南京'")
        if 'lodging_poi_id' not in trip_cols:
            self.conn.execute(
                'ALTER TABLE trip ADD COLUMN lodging_poi_id INTEGER')
        if 'lodging_declined' not in trip_cols:
            self.conn.execute(
                'ALTER TABLE trip ADD COLUMN lodging_declined INTEGER NOT NULL'
                ' DEFAULT 0')
        # 多城市的唯一键要在列补齐之后才能建（老库的 trip 表原本没有 city 列）
        self.conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_date_city'
            ' ON trip(plan_date, city)')

    def _trip_needs_rebuild(self) -> bool:
        """旧库的 trip.plan_date 是否带内联 UNIQUE 约束（origin='u'）"""
        rows = self.conn.execute('PRAGMA index_list(trip)').fetchall()
        return any(r['origin'] == 'u' for r in rows)

    def _rebuild_trip(self) -> None:
        """把 plan_date 的单列唯一约束换成 (plan_date, city) 复合唯一"""
        self.conn.commit()
        self.conn.execute('PRAGMA foreign_keys = OFF')
        try:
            self.conn.executescript("""
                CREATE TABLE trip_new (
                    trip_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_date   TEXT NOT NULL,
                    city        TEXT NOT NULL DEFAULT '南京',
                    title       TEXT,
                    lodging_poi_id INTEGER REFERENCES poi(poi_id) ON DELETE SET NULL,
                    lodging_declined INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
                INSERT INTO trip_new (trip_id, plan_date, city, title,
                                      lodging_poi_id, lodging_declined,
                                      created_at, updated_at)
                    SELECT trip_id, plan_date, '南京', title, NULL, 0,
                           created_at, updated_at FROM trip;
                DROP TABLE trip;
                ALTER TABLE trip_new RENAME TO trip;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_date_city
                    ON trip(plan_date, city);
            """)
            self.conn.commit()
        finally:
            self.conn.execute('PRAGMA foreign_keys = ON')

    # ---------- 版本号（多端同步用） ----------
    def revision(self) -> int:
        with self.lock:
            row = self.conn.execute(
                'SELECT value FROM app_setting WHERE key = ?',
                (self.REV_KEY,)).fetchone()
            try:
                return int(row['value']) if row else 0
            except (TypeError, ValueError):
                return 0

    def bump_revision(self) -> int:
        """任何写操作成功后调用：版本号 +1，供各端轮询感知变化"""
        with self.lock:
            rev = self.revision() + 1
            self.conn.execute(
                'INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?)'
                ' ON CONFLICT(key) DO UPDATE SET value = excluded.value,'
                ' updated_at = excluded.updated_at',
                (self.REV_KEY, str(rev), _now()))
            self.conn.commit()
            return rev

    @contextmanager
    def transaction(self):
        """事务边界：异常自动回滚（FR-07 数据一致性）"""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class SqliteTripRepository:
    """行程仓储：行程 + 事项 + 地点 的读写"""

    def __init__(self, db: Database):
        self.db = db

    # ---------- 地点 ----------
    def ensure_poi(self, poi: Poi) -> Optional[int]:
        """确保地点已入库并返回主键（供 GeoService 与通行段缓存使用）"""
        return self._poi_id(poi)

    def _poi_id(self, poi: Poi) -> Optional[int]:
        if poi is None:
            return None
        cur = self.db.conn.execute(
            'SELECT poi_id FROM poi WHERE provider = ? AND provider_poi_id = ?',
            (poi.provider, poi.provider_poi_id or poi.name))
        row = cur.fetchone()
        if row:
            poi.poi_id = row['poi_id']
            self.db.conn.execute('UPDATE poi SET city = ? WHERE poi_id = ?',
                                 (poi.city or '', poi.poi_id))
            return poi.poi_id
        cur = self.db.conn.execute(
            'INSERT INTO poi (provider, provider_poi_id, name, address, district,'
            ' city, lng, lat, updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (poi.provider, poi.provider_poi_id or poi.name, poi.name, poi.address,
             poi.district, poi.city or '', poi.lng, poi.lat, _now()))
        poi.poi_id = int(cur.lastrowid)
        return poi.poi_id

    def get_poi(self, poi_id: int) -> Optional[Poi]:
        row = self.db.conn.execute('SELECT * FROM poi WHERE poi_id = ?',
                                   (poi_id,)).fetchone()
        if not row:
            return None
        poi = Poi(poi_id=row['poi_id'], provider=row['provider'],
                  provider_poi_id=row['provider_poi_id'], name=row['name'],
                  address=row['address'] or '', district=row['district'],
                  lng=row['lng'], lat=row['lat'],
                  city=(row['city'] if 'city' in row.keys() else '') or '')
        return self._rehydrate(poi)

    @staticmethod
    def _rehydrate(poi: Poi) -> Poi:
        """把城市包里的营业信息补回从库里读出的 POI。

        **营业信息（类别/开放时间/闭馆日/预约/价位）只存城市包、不落库**，
        避免同一份数据两处维护；但读回来时必须补齐，否则"周一闭馆""是不是餐厅"
        这类判断在重载后就失效了（曾经的真实缺陷）。

        老库里的 poi 行可能没有 city（多城市之前写入的），此时按全部城市包
        依次查找，命中后顺便把 city 补上。
        """
        from .cities import CITY_PACKS, get_city
        packs = []
        if poi.city:
            packs.append(get_city(poi.city))
        known = {p.name for p in packs}
        packs.extend(p for p in CITY_PACKS.values() if p.name not in known)

        for pack in packs:
            data = pack.find(poi.name)
            if data is None:
                continue
            fresh = Poi.from_pack(data, poi.city or pack.name)
            fresh.poi_id = poi.poi_id
            fresh.provider = poi.provider or fresh.provider
            fresh.provider_poi_id = poi.provider_poi_id or fresh.provider_poi_id
            if poi.lng and poi.lat:             # 库里的坐标可能来自在线检索，更准
                fresh.lng, fresh.lat = poi.lng, poi.lat
            if poi.address:
                fresh.address = poi.address
            if poi.district:
                fresh.district = poi.district
            return fresh
        return poi

    # ---------- 行程 ----------
    def upsert_day(self, trip: Trip) -> int:
        ds = trip.plan_date.isoformat()
        city = trip.city or '南京'
        with self.db.transaction() as conn:
            row = conn.execute(
                'SELECT trip_id FROM trip WHERE plan_date = ? AND city = ?',
                (ds, city)).fetchone()
            lodging_id = self._poi_id(trip.lodging) if trip.lodging else None
            if row:
                trip.trip_id = row['trip_id']
                conn.execute(
                    'UPDATE trip SET title = ?, lodging_poi_id = ?,'
                    ' lodging_declined = ?, updated_at = ? WHERE trip_id = ?',
                    (trip.title or f'{ds} 行程', lodging_id,
                     1 if trip.lodging_declined else 0, _now(),
                     trip.trip_id))
            else:
                cur = conn.execute(
                    'INSERT INTO trip (plan_date, city, title, lodging_poi_id,'
                    ' lodging_declined, created_at, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?)',
                    (ds, city, trip.title or f'{ds} 行程', lodging_id,
                     1 if trip.lodging_declined else 0, _now(), _now()))
                trip.trip_id = int(cur.lastrowid)

            # 增量写当日事项：**按 task_id 更新**而不是先删后插，
            # 否则每次保存都会换一批 id，手机端等外部引用立刻失效
            existing = {r['task_id'] for r in conn.execute(
                'SELECT task_id FROM task WHERE trip_id = ?',
                (trip.trip_id,)).fetchall()}
            keep: set[int] = set()
            for t in trip.tasks:
                poi_id = self._poi_id(t.poi) if t.poi else None
                values = (t.title, t.category, t.duration_min,
                          _t2s(t.desired_start), poi_id, t.raw_text,
                          1 if t.is_locked else 0, t.status.value, t.seq,
                          _t2s(t.start_ts), _t2s(t.end_ts),
                          t.mode.value if t.mode else None,
                          _t2s(t.deadline))
                if t.task_id and t.task_id in existing:
                    conn.execute(
                        'UPDATE task SET title=?, category=?, duration_min=?,'
                        ' desired_start=?, poi_id=?, raw_text=?, is_locked=?,'
                        ' status=?, seq=?, start_ts=?, end_ts=?, mode=?, deadline=?'
                        ' WHERE task_id=?', values + (t.task_id,))
                    keep.add(t.task_id)
                else:
                    cur = conn.execute(
                        'INSERT INTO task (title, category, duration_min,'
                        ' desired_start, poi_id, raw_text, is_locked, status,'
                        ' seq, start_ts, end_ts, mode, deadline, trip_id)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        values + (trip.trip_id,))
                    t.task_id = int(cur.lastrowid)
                    keep.add(t.task_id)
                t.trip_id = trip.trip_id
            # 本次不再存在的项才删除
            for tid in existing - keep:
                conn.execute('DELETE FROM task WHERE task_id = ?', (tid,))
        return trip.trip_id

    def update_poi_coord(self, poi: Poi, lng: float, lat: float) -> bool:
        """把地点坐标改成「确认过」的值（地图页第 1 步"确认点"的落库动作）"""
        if poi is None:
            return False
        row = self.db.conn.execute(
            'SELECT poi_id FROM poi WHERE provider = ? AND provider_poi_id = ?',
            (poi.provider, poi.provider_poi_id or poi.name)).fetchone()
        if not row:
            return False
        poi.lng, poi.lat = float(lng), float(lat)
        with self.db.lock:
            self.db.conn.execute('UPDATE poi SET lng = ?, lat = ? WHERE poi_id = ?',
                                 (poi.lng, poi.lat, row['poi_id']))
        return True

    def month_counts(self, year: int, month: int,
                     city: Optional[str] = None) -> dict[str, int]:
        """按月统计「哪些日期有安排、各排了几项」，供左栏月历使用。

        返回 {ISO 日期: 事项数}；只认识日期与城市，不碰任何界面概念。
        """
        sql = ("SELECT t.plan_date, COUNT(k.task_id) FROM trip t"
               " LEFT JOIN task k ON k.trip_id = t.trip_id"
               " WHERE t.plan_date LIKE ?")
        args: list = ['%04d-%02d-%%' % (year, month)]
        if city:
            sql += ' AND t.city = ?'
            args.append(city)
        sql += ' GROUP BY t.plan_date'
        return {row[0]: row[1] for row in self.db.conn.execute(sql, args)}

    def list_days(self, city: Optional[str] = None) -> list[dict]:
        """列出所有"有安排"的日期（当作规划记录来管理：日期/城市/项数/首末时刻）"""
        sql = ("SELECT t.plan_date AS d, t.city AS city, t.title AS title,"
               "       COUNT(k.task_id) AS cnt,"
               "       MIN(k.start_ts) AS first_ts, MAX(k.end_ts) AS last_ts"
               "  FROM trip t LEFT JOIN task k ON k.trip_id = t.trip_id")
        args: list = []
        if city:
            sql += ' WHERE t.city = ?'
            args.append(city)
        sql += ' GROUP BY t.trip_id ORDER BY t.plan_date DESC, t.city'
        def hhmm(raw) -> str:
            s = str(raw or '')
            if not s:
                return ''
            # 兼容 "2026-11-03 09:02:00" / "2026-11-03T09:02" / "09:02:00"
            s = s.replace('T', ' ')
            part = s.split(' ')[-1]
            return part[:5]

        out = []
        for row in self.db.conn.execute(sql, args):
            out.append({'date': row['d'], 'city': row['city'] or '南京',
                        'title': row['title'] or '', 'count': row['cnt'],
                        'first': hhmm(row['first_ts']),
                        'last': hhmm(row['last_ts'])})
        return out

    def delete_day(self, plan_date: date, city: Optional[str] = None) -> int:
        """删掉某天的整份行程（trip 级联删除 task / 通行段），返回受影响行数"""
        sql = 'DELETE FROM trip WHERE plan_date = ?'
        args: list = [plan_date.isoformat()]
        if city:
            sql += ' AND city = ?'
            args.append(city)
        with self.db.lock:
            cur = self.db.conn.execute(sql, args)
            self.db.bump_revision()
        return cur.rowcount or 0

    def get_day(self, plan_date: date, city: Optional[str] = None) -> Optional[Trip]:
        """按日期取行程；给了 city 就只取该城市的那一份（一城一日一份）"""
        ds = plan_date.isoformat()
        if city:
            row = self.db.conn.execute(
                'SELECT * FROM trip WHERE plan_date = ? AND city = ?',
                (ds, city)).fetchone()
        else:
            row = self.db.conn.execute(
                'SELECT * FROM trip WHERE plan_date = ?'
                ' ORDER BY trip_id LIMIT 1', (ds,)).fetchone()
        if not row:
            return None
        lodging = self.get_poi(row['lodging_poi_id']) if row['lodging_poi_id'] else None
        trip = Trip(plan_date=plan_date, trip_id=row['trip_id'],
                    title=row['title'] or '', city=row['city'] or '南京',
                    lodging=lodging,
                    lodging_declined=bool(row['lodging_declined'])
                    if 'lodging_declined' in row.keys() else False)
        for r in self.db.conn.execute(
                'SELECT * FROM task WHERE trip_id = ? ORDER BY seq, task_id',
                (trip.trip_id,)):
            poi = self.get_poi(r['poi_id']) if r['poi_id'] else None
            trip.tasks.append(Task(
                title=r['title'], duration_min=r['duration_min'],
                task_id=r['task_id'], trip_id=r['trip_id'],
                category=r['category'] or '其他',
                desired_start=_s2t(r['desired_start']), poi=poi,
                raw_text=r['raw_text'] or '', is_locked=bool(r['is_locked']),
                status=TaskStatus(r['status']), seq=r['seq'] or 0,
                start_ts=_s2t(r['start_ts']), end_ts=_s2t(r['end_ts']),
                mode=TransitMode(r['mode']) if r['mode'] else None,
                deadline=_s2t(r['deadline'])))
        return trip

    def get_task(self, task_id: int) -> Optional[Task]:
        row = self.db.conn.execute('SELECT * FROM task WHERE task_id = ?',
                                   (task_id,)).fetchone()
        if not row:
            return None
        t_row = self.db.conn.execute(
            'SELECT plan_date, city FROM trip WHERE trip_id = ?',
            (row['trip_id'],)).fetchone()
        if not t_row:
            return None
        trip = self.get_day(date.fromisoformat(t_row['plan_date']),
                            t_row['city'] or None)
        if not trip:
            return None
        for t in trip.tasks:
            if t.task_id == task_id:
                return t
        return None

    def delete_task(self, task_id: int) -> Optional[int]:
        row = self.db.conn.execute('SELECT trip_id FROM task WHERE task_id = ?',
                                   (task_id,)).fetchone()
        if not row:
            return None
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM task WHERE task_id = ?', (task_id,))
        return row['trip_id']

    # ---------- 配置 ----------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.conn.execute('SELECT value FROM app_setting WHERE key = ?',
                                   (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?)'
                ' ON CONFLICT(key) DO UPDATE SET value = excluded.value,'
                ' updated_at = excluded.updated_at',
                (key, value, _now()))


class SqliteTransitCache:
    """通行段缓存仓储（减少地图 API 调用，见 4.4 数据设计说明 1）"""

    def __init__(self, db: Database):
        self.db = db

    def get(self, from_poi_id: int, to_poi_id: int, mode: str) -> Optional[int]:
        row = self.db.conn.execute(
            'SELECT duration_min FROM transit_leg WHERE from_poi_id = ?'
            ' AND to_poi_id = ? AND mode = ?',
            (from_poi_id, to_poi_id, mode)).fetchone()
        return int(row['duration_min']) if row else None

    def get_full(self, from_poi_id: int, to_poi_id: int, mode: str):
        row = self.db.conn.execute(
            'SELECT * FROM transit_leg WHERE from_poi_id = ? AND to_poi_id = ?'
            ' AND mode = ?', (from_poi_id, to_poi_id, mode)).fetchone()
        return dict(row) if row else None

    def put(self, from_poi_id: int, to_poi_id: int, mode: str,
            duration_min: int, distance_m: int, source: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO transit_leg (from_poi_id, to_poi_id, mode,'
                ' duration_min, distance_m, source, fetched_at)'
                ' VALUES (?,?,?,?,?,?,?)'
                ' ON CONFLICT(from_poi_id, to_poi_id, mode) DO UPDATE SET'
                ' duration_min = excluded.duration_min,'
                ' distance_m = excluded.distance_m,'
                ' source = excluded.source, fetched_at = excluded.fetched_at',
                (from_poi_id, to_poi_id, mode, duration_min, distance_m,
                 source, _now()))

    def count(self) -> int:
        row = self.db.conn.execute('SELECT COUNT(*) AS c FROM transit_leg').fetchone()
        return int(row['c'])


class SqliteVersionRepository:
    """行程版本仓储（BR-07 可回滚，保留最近 N 个版本）"""

    def __init__(self, db: Database, keep: int = 20):
        self.db = db
        self.keep = keep

    def add(self, trip_id: int, note: str, snapshot_json: str) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute(
                'INSERT INTO trip_version (trip_id, created_at, note, snapshot_json)'
                ' VALUES (?,?,?,?)', (trip_id, _now(), note, snapshot_json))
            vid = int(cur.lastrowid)
            # 仅保留最近 keep 个版本
            conn.execute(
                'DELETE FROM trip_version WHERE trip_id = ? AND version_id NOT IN'
                ' (SELECT version_id FROM trip_version WHERE trip_id = ?'
                '  ORDER BY created_at DESC, version_id DESC LIMIT ?)',
                (trip_id, trip_id, self.keep))
        return vid

    def list(self, trip_id: int) -> list[TripVersion]:
        out = []
        for r in self.db.conn.execute(
                'SELECT * FROM trip_version WHERE trip_id = ?'
                ' ORDER BY created_at DESC, version_id DESC', (trip_id,)):
            out.append(TripVersion(version_id=r['version_id'], trip_id=r['trip_id'],
                                   created_at=r['created_at'], note=r['note'] or '',
                                   snapshot_json=r['snapshot_json']))
        return out

    def get(self, version_id: int) -> Optional[TripVersion]:
        r = self.db.conn.execute('SELECT * FROM trip_version WHERE version_id = ?',
                                 (version_id,)).fetchone()
        if not r:
            return None
        return TripVersion(version_id=r['version_id'], trip_id=r['trip_id'],
                           created_at=r['created_at'], note=r['note'] or '',
                           snapshot_json=r['snapshot_json'])
