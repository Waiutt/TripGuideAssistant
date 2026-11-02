"""游览时长证据表：把"我估的"变成"有出处的"。

设计（与后续需求规划 P1 一致，**不含用户回填闭环**）
------------------------------------------------
* 一条地点可以有多条证据，来源分三级：
    - `official` 官方口径（景区官网/公众号/博物馆推荐参观路线）——最可信
    - `ota`      在线旅游平台/攻略页面登记的"建议游玩时长"
    - `estimate` 系统估算（类型基线 × 评分 × 营业窗口）
* 取值优先级：official > ota > estimate；同级多条取**中位数**；
* 对外给**区间**而不是单值：按来源给不同浮动
    official ±10%、ota ±20%、estimate ±35%
  排程仍用区间中值（`minutes`），界面上把区间与来源一起显示，用户可改。
* 没有用户回填通道（按要求不做闭环），所以这里只有三条来源。

数据从哪来：`add()` 是唯一入口——官方口径由 `tools/import_visit_evidence.py`
（人工核对后批量导入，带来源说明与核对日期）；估算由 `intel` 现算。
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Optional

from .models import Poi

#: 来源优先级（数值越小越可信）
PRIORITY = {'official': 0, 'ota': 1, 'estimate': 2}
#: 各来源的浮动比例（用于生成区间）
SPREAD = {'official': 0.10, 'ota': 0.20, 'estimate': 0.35}
#: 来源的中文标签
LABEL = {'official': '官方口径', 'ota': '平台登记', 'estimate': '估算'}
#: 没写时长、也没证据时的类型兜底
_KIND_DEFAULT = {'景点': 90, '场馆': 90, '公园': 90, '街区': 60, '商圈': 60,
                 '书店': 60, '校园': 60, '餐饮': 60}


class DurationEvidence:
    TABLE = """
    CREATE TABLE IF NOT EXISTS visit_evidence (
        city TEXT NOT NULL DEFAULT '南京',
        name TEXT NOT NULL,
        source TEXT NOT NULL,
        minutes INTEGER NOT NULL,
        note TEXT DEFAULT '',
        checked_at TEXT DEFAULT '',
        PRIMARY KEY (city, name, source)
    )"""

    def __init__(self, db):
        self.db = db
        with self.db.transaction() as conn:
            conn.execute(self.TABLE)

    # ---------- 写入证据 ----------
    def add(self, name: str, minutes: int, source: str, city: str = '南京',
            note: str = '') -> None:
        if source not in PRIORITY:
            raise ValueError(f'未知来源：{source}')
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO visit_evidence (city, name, source, minutes, note,'
                ' checked_at) VALUES (?,?,?,?,?,?)'
                ' ON CONFLICT(city, name, source) DO UPDATE SET'
                ' minutes=excluded.minutes, note=excluded.note,'
                ' checked_at=excluded.checked_at',
                (city, name, source, int(minutes), note,
                 datetime.now().isoformat(timespec='seconds')))

    def rows(self, name: str, city: str = '南京') -> list[dict]:
        cur = self.db.conn.execute(
            'SELECT * FROM visit_evidence WHERE city = ? AND name = ?',
            (city, name))
        return [dict(r) for r in cur.fetchall()]

    # ---------- 取值 ----------
    def resolve(self, poi: Optional[Poi], city: str = '') -> dict:
        """返回 {'minutes', 'low', 'high', 'source', 'label', 'samples'}

        minutes 是排程用的中值；low/high 给界面显示区间。
        """
        if poi is None:
            return {'minutes': 60, 'low': 45, 'high': 90, 'source': 'estimate',
                    'label': LABEL['estimate'], 'samples': 0}
        city = city or poi.city or '南京'
        found = self.rows(poi.name, city)
        by_source: dict[str, list[int]] = {}
        for r in found:
            by_source.setdefault(str(r['source']), []).append(int(r['minutes']))
        for source in ('official', 'ota'):
            vals = by_source.get(source)
            if vals:
                mid = int(round(statistics.median(vals)))
                return self._pack(mid, source, len(vals))
        # 没有 official/ota 证据 → 用估算（包内推荐时长优先，其次类型兜底）
        base = int(poi.visit_min or 0) or _KIND_DEFAULT.get(str(poi.kind), 60)
        return self._pack(base, 'estimate', 0)

    def _pack(self, mid: int, source: str, samples: int) -> dict:
        spread = SPREAD.get(source, 0.35)
        low = max(10, int(round(mid * (1 - spread) / 5.0)) * 5)
        high = int(round(mid * (1 + spread) / 5.0)) * 5
        return {'minutes': max(10, int(mid)), 'low': low, 'high': high,
                'source': source, 'label': LABEL.get(source, source),
                'samples': samples}

    # ---------- 展示 ----------
    @staticmethod
    def text(ev: dict) -> str:
        """「2 小时 20 分钟」"""
        m = int(ev.get('minutes') or 0)
        h, mm = divmod(m, 60)
        return (f'{h} 小时' if mm == 0 else
                f'{h} 小时 {mm} 分钟' if h else f'{mm} 分钟')

    @staticmethod
    def range_text(ev: dict) -> str:
        """「1 小时 30 分–2 小时 30 分 · 估算」"""
        lo, hi = int(ev.get('low') or 0), int(ev.get('high') or 0)
        m = int(ev.get('minutes') or 0)

        def hm(x: int) -> str:
            h, mm = divmod(int(round(x / 5.0)) * 5, 60)
            return f'{h} 小时' if mm == 0 else (f'{h} 小时 {mm} 分' if h else f'{mm} 分钟')
        if lo and hi and hi > lo:
            return f'{hm(lo)}–{hm(hi)} · {ev.get("label") or "估算"}'
        return f'{hm(m)} · {ev.get("label") or "估算"}'
