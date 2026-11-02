"""取数管线：把"景点的真实营业时间/票价/评分/推荐时长"从外部数据源拿到并缓存。

为什么不写死在城市包里
----------------------
城市包里的 `visit` / `open_from` / `open_to` 是**演示种子数据**：一旦真实数据变了
（旺季/淡季、临时闭馆、涨价），写死的值就变成错误信息。所以规划时按需取数、落库缓存：

* **营业时间 / 票价 / 评分 / 类型**：高德 `place/text`（`extensions=all` 的 `biz_ext`）
  ——这是真实数据，取到即用，下次直接读缓存；
* **推荐游玩时长**：公开地图/搜索接口都不提供这个字段（携程、马蜂窝、百科均无开放 API），
  因此按"高德类型 + 评分 + 营业窗口长度"给出**估算值**，并如实标注来源为 estimate，
  界面上允许用户一键改；用户改过的值写回缓存，下回优先用它。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

from .models import Poi

log = logging.getLogger(__name__)

#: 类型 → 基础游览时长（分钟）。这是**估算的起点**，不是权威推荐值。
_KIND_BASE = (
    ('博物馆', 120), ('纪念馆', 120), ('展览馆', 120), ('科技馆', 120),
    ('寺庙', 60), ('道观', 60), ('教堂', 45),
    ('动物园', 150), ('植物园', 120), ('公园', 90),
    ('国家级景点', 120), ('风景名胜', 90),
    ('商业街', 60), ('购物中心', 90), ('书店', 60),
    ('体育', 90), ('剧院', 120),
)


def parse_open_time(text: str) -> tuple[str, str]:
    """把高德的 `open_time` 解析成 (开门, 关门)。

    真实返回值五花八门：`09:00-17:00`、`24小时营业`、`05:30-21:30 07:00-17:00 ...`
    （多时段）、`08:30-17:30;周一闭馆`。取**首个时间段**，并忽略闭馆说明。
    """
    s = (text or '').strip()
    if not s:
        return '', ''
    if '24小时' in s or '全天' in s:
        return '00:00', '23:59'
    m = re.search(r'(\d{1,2}:\d{2})\s*[-—~至]\s*(\d{1,2}:\d{2})', s)
    if not m:
        return '', ''
    return m.group(1).zfill(5), m.group(2).zfill(5)


def parse_closed_days(text: str) -> tuple[str, ...]:
    """从营业时间里抠闭馆日（`周一闭馆` / `每周一全天闭馆`）"""
    s = text or ''
    m = re.search(r'(?:每)?周([一二三四五六日天])[^;；]{0,6}闭馆', s)
    return (m.group(1),) if m else ()


def estimate_visit_min(kind_text: str, rating: float = 0.0,
                       open_from: str = '', open_to: str = '') -> int:
    """按类型/评分/营业窗口估推荐游览时长（分钟）。

    评分 ≥4.7 的长线景点再加 20%（真实大馆走一圈确实更久），
    但永远不超过营业窗口的 80% —— 当天关门前来不及逛完没有意义。
    """
    base = 90
    for key, minutes in _KIND_BASE:
        if key in (kind_text or ''):
            base = minutes
            break
    if rating >= 4.7:
        base = int(round(base * 1.2))
    cap = None
    if open_from and open_to:
        try:
            h1, m1 = open_from.split(':')[:2]
            h2, m2 = open_to.split(':')[:2]
            win = (int(h2) * 60 + int(m2)) - (int(h1) * 60 + int(m1))
            if win > 0:
                cap = max(45, int(win * 0.8))
                base = min(base, cap)
        except (ValueError, TypeError):
            pass
    rounded = max(30, int(round(base / 10.0)) * 10)
    if cap is not None:                      # 取整不许突破营业窗口上限
        rounded = min(rounded, cap)
    return rounded


class PoiIntelService:
    """景点情报的取数与缓存（营业时间/票价/评分/推荐时长）"""

    TABLE = """
    CREATE TABLE IF NOT EXISTS poi_intel (
        city TEXT NOT NULL DEFAULT '南京',
        name TEXT NOT NULL,
        kind TEXT DEFAULT '',
        open_from TEXT DEFAULT '',
        open_to TEXT DEFAULT '',
        closed_days TEXT DEFAULT '',
        cost TEXT DEFAULT '',
        rating TEXT DEFAULT '',
        visit_min INTEGER DEFAULT 0,
        star TEXT DEFAULT '',
        lowest_price TEXT DEFAULT '',
        business_area TEXT DEFAULT '',
        source TEXT DEFAULT '',
        fetched_at TEXT DEFAULT '',
        PRIMARY KEY (city, name)
    )"""

    #: 老库补列（CREATE TABLE IF NOT EXISTS 不会给已有表加列）
    EXTRA_COLUMNS = (('star', "TEXT DEFAULT ''"),
                     ('lowest_price', "TEXT DEFAULT ''"),
                     ('business_area', "TEXT DEFAULT ''"))

    def __init__(self, db, degrade):
        self.db = db
        self.degrade = degrade
        with self.db.transaction() as conn:
            conn.execute(self.TABLE)
            for col, decl in self.EXTRA_COLUMNS:
                try:
                    conn.execute(f'ALTER TABLE poi_intel ADD COLUMN {col} {decl}')
                except Exception:            # noqa: BLE001 列已存在
                    pass

    # ---------- 缓存读写 ----------
    def cached(self, name: str, city: str) -> Optional[dict]:
        row = self.db.conn.execute(
            'SELECT * FROM poi_intel WHERE city = ? AND name = ?',
            (city, name)).fetchone()
        return dict(row) if row else None

    def save(self, city: str, name: str, data: dict) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                'INSERT INTO poi_intel (city, name, kind, open_from, open_to,'
                ' closed_days, cost, rating, visit_min, star, lowest_price,'
                ' business_area, source, fetched_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
                ' ON CONFLICT(city, name) DO UPDATE SET'
                ' kind=excluded.kind, open_from=excluded.open_from,'
                ' open_to=excluded.open_to, closed_days=excluded.closed_days,'
                ' cost=excluded.cost, rating=excluded.rating,'
                ' visit_min=excluded.visit_min, star=excluded.star,'
                ' lowest_price=excluded.lowest_price,'
                ' business_area=excluded.business_area,'
                ' source=excluded.source, fetched_at=excluded.fetched_at',
                (city, name, data.get('kind', ''), data.get('open_from', ''),
                 data.get('open_to', ''), ','.join(data.get('closed_days') or ()),
                 data.get('cost', ''), data.get('rating', ''),
                 int(data.get('visit_min') or 0), data.get('star', ''),
                 data.get('lowest_price', ''), data.get('business_area', ''),
                 data.get('source', ''),
                 datetime.now().isoformat(timespec='seconds')))

    # ---------- 取数 ----------
    def fetch(self, name: str, city: str) -> Optional[dict]:
        """在线取一次（失败返回 None，绝不影响排程）"""
        if not getattr(self.degrade, 'map_ready', lambda: False)():
            return None
        adapter = self.degrade.map()
        fn = getattr(adapter, 'place_detail', None)
        if fn is None:
            return None
        try:
            raw = fn(name, city)
        except Exception as exc:                 # noqa: BLE001
            log.warning('景点情报取数失败 %s：%s', name, exc)
            return None
        if not raw:
            return None
        biz = raw.get('biz_ext') or {}
        open_text = str(biz.get('open_time') or '')
        from_h, from_m = parse_open_time(open_text)
        rating = 0.0
        try:
            rating = float(str(biz.get('rating') or 0) or 0)
        except (TypeError, ValueError):
            rating = 0.0
        kind_text = str(raw.get('type') or '')
        def _first(v):
            if isinstance(v, (list, tuple)):
                return str(v[0]) if v else ''
            return str(v or '')

        data = {
            'kind': kind_text,
            'star': _first(biz.get('star')),
            'lowest_price': _first(biz.get('lowest_price')),
            'business_area': str(raw.get('business_area') or ''),
            'open_from': from_h, 'open_to': from_m,
            'closed_days': ','.join(parse_closed_days(open_text)),
            'cost': str(biz.get('cost') or ''),
            'rating': str(biz.get('rating') or ''),
            'visit_min': estimate_visit_min(kind_text, rating, from_h, from_m),
            'source': 'amap',
        }
        self.save(city, name, data)
        return data

    def get(self, name: str, city: str) -> Optional[dict]:
        got = self.cached(name, city)
        if got is not None:
            return got
        return self.fetch(name, city)

    # ---------- 应用到 Poi ----------
    def apply(self, poi: Optional[Poi]) -> Optional[Poi]:
        """把取到的真实营业/时长信息覆盖到 Poi 上（取不到就保持原样）"""
        if poi is None or not poi.name:
            return poi
        city = poi.city or '南京'
        got = self.get(poi.name, city)
        if not got:
            return poi
        if got.get('open_from') and got.get('open_to'):
            poi.open_from, poi.open_to = got['open_from'], got['open_to']
        if got.get('closed_days'):
            poi.closed_days = tuple(x for x in str(got['closed_days']).split(',') if x)
        if got.get('cost') and not poi.price:
            poi.price = f'¥{got["cost"]}'
        low = str(got.get('lowest_price') or '')
        if low and str(poi.kind) == '住宿':
            day = str(got.get('fetched_at') or '')[:10]
            poi.price = f'¥{float(low):.0f} 起（高德取数 {day}）' if day else f'¥{float(low):.0f} 起'
        if got.get('star') and str(poi.kind) == '住宿':
            poi.star = f'{got["star"]} 星'
        if int(got.get('visit_min') or 0) > 0:
            poi.visit_min = int(got['visit_min'])
        return poi
