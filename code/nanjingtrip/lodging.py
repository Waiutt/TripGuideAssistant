# -*- coding: utf-8 -*-
"""过夜检测与住宿推荐：行程跨夜时**主动**给出"住哪儿"的选项。

为什么单独一个模块
------------------
"住"不是一件普通事项：它由**整天的形状**决定（末项结束多晚、次日还要不要早起出门），
而不由某一条录入决定。因此把它做成基于排程结果的**派生建议**，
用户不必先选酒店再排行程——系统先排，排完告诉他"这天收尾太晚、明天从这儿出发更顺，
建议住这几家"，再由他一句话确认。

判定"需要住宿"的三种情形
------------------------
1. **收尾太晚**：末项结束 ≥ ``LATE_END_MIN``（默认 21:30），且次日仍有安排；
2. **明显跨夜**：末项结束时刻把人推到深夜（≥ 23:00），无论次日有无安排；
3. **异地行程**：住宿锚点尚未设定，而当日行程地点与"常住城市"不同城
   （由调用方传入 ``away_from_home``）。

已在当天设好住宿锚点时不重复推荐。
"""
from __future__ import annotations

from typing import Iterable, Optional

from .cities import CityPack
from .geo import distance_km
from .models import (Conflict, Poi, ScheduleResult, Suggestion,
                     hhmm)

# 收尾晚于这个时刻（分钟）且次日还有安排 → 建议住宿
LATE_END_MIN = 21 * 60 + 30
# 收尾晚于这个时刻 → 无论次日如何都建议住宿（深夜回不去）
VERY_LATE_MIN = 23 * 60


# ======================================================================
# 何时需要住宿
# ======================================================================
def detect_reason(result: ScheduleResult, next_has_items: bool,
                  lodging: Optional[Poi] = None,
                  away_from_home: bool = False) -> str:
    """返回需要住宿的原因；不需要则返回空串"""
    if lodging is not None:
        return ''                       # 已经设好落脚点
    if not result.items:
        return ''
    end_min = max(it.end_min for it in result.items)
    if away_from_home and (next_has_items or end_min >= LATE_END_MIN):
        return '异地行程，且当天有安排，需要落脚点'
    if end_min >= VERY_LATE_MIN:
        return f'末项 {hhmm(end_min)} 才结束，深夜出行不便'
    if end_min >= LATE_END_MIN and next_has_items:
        return (f'末项 {hhmm(end_min)} 结束、次日还有安排，'
                '住一晚比来回赶更划算')
    return ''


# ======================================================================
# 推荐哪些住宿
# ======================================================================
def _anchor_points(result: ScheduleResult,
                   next_result: Optional[ScheduleResult]) -> list[Poi]:
    """取"今日末项"与"次日首项"作为选酒店的锚点（都是出行的实际起终点）"""
    pts: list[Poi] = []
    if result.items:
        last = max(result.items, key=lambda i: i.end_min)
        if last.task.poi is not None:
            pts.append(last.task.poi)
    if next_result is not None and next_result.items:
        first = min(next_result.items, key=lambda i: i.start_min)
        if first.task.poi is not None:
            pts.append(first.task.poi)
    return pts


def recommend(pack: CityPack, anchors: Iterable[Poi], limit: int = 3,
              max_km: float = 8.0) -> list[dict]:
    """按到各锚点的平均距离推荐住宿（城市包里 kind='住宿' 的 POI）

    :return: [{'poi': Poi, 'distance_km': float, 'reason': str}, ...]
    """
    pts = [p for p in anchors if p is not None]
    hotels = [Poi.from_pack(p, pack.name) for p in pack.pois
              if p.get('kind') == '住宿']
    if not hotels:
        return []
    scored = []
    for h in hotels:
        ds = [(distance_km(h.lng, h.lat, p.lng, p.lat), p) for p in pts]
        dist = sum(d for d, _ in ds) / len(ds) if ds else 0.0
        scored.append((dist, h, ds))
    # 平均距离优先；距离打平时价位便宜的排前面（住得近且省钱）
    scored.sort(key=lambda x: (round(x[0], 3), x[1].price))
    out: list[dict] = []
    for dist, h, ds in scored:
        if dist > max_km and out:
            break
        out.append({'poi': h, 'distance_km': round(dist, 2),
                    'reason': _reason(h, ds)})
        if len(out) >= limit:
            break
    return out


def _reason(hotel: Poi, ds: list) -> str:
    """把"和谁近"说清楚：只报平均距离会让几家酒店看起来一样远"""
    if not ds:
        return hotel.brief()
    nearest = sorted(ds, key=lambda x: x[0])
    d0, p0 = nearest[0]
    if d0 < 0.3:
        return f'就在「{p0.name}」旁边（约 {max(50, int(d0 * 1000))} 米）'
    if len(nearest) == 1:
        return f'距「{p0.name}」约 {d0:.1f} 公里'
    d1, p1 = nearest[1]
    return f'距「{p0.name}」约 {d0:.1f} 公里、距「{p1.name}」约 {d1:.1f} 公里'



# ======================================================================
# 生成提示与选项（含"不住"等其他选择）
# ======================================================================
def build_notice(reason: str, result: ScheduleResult, recs: list[dict],
                 city: str) -> Conflict:
    """把"需要住宿"包装成一条软提示，附候选酒店与其它选择"""
    last = max(result.items, key=lambda i: i.end_min) if result.items else None
    head = f'{reason}（{city}）' if city else reason
    if recs:
        opts = '；'.join(
            f'{r["poi"].name}（{r["poi"].star or "住宿"}，'
            f'{r["poi"].price or "价格未录入"}，{r["reason"]}）'
            for r in recs)
        detail = f'{head}。可选住宿：{opts}'
    else:
        detail = f'{head}。内置数据里暂无合适的住宿候选，可在线搜索后再录入。'

    c = Conflict(kind='lodging_suggest', detail=detail, task_title='住宿')
    for r in recs:
        c.suggestions.append(Suggestion(
            kind='set_lodging',
            text=f'住「{r["poi"].name}」（{r["reason"]}）',
            patch={'lodging': r['poi'].name}))
    if last is not None:
        c.suggestions.append(Suggestion(
            kind='move_next_day',
            text=f'把「{last.task.title}」挪到次日，当天按时收尾（就不必住宿）',
            patch={'move_next_day': True, 'title': last.task.title}))
        shorter = max(15, last.task.duration_min - 30)
        c.suggestions.append(Suggestion(
            kind='shorten',
            text=f'把「{last.task.title}」压缩到 {shorter} 分钟，提早收尾',
            patch={'duration_min': shorter, 'title': last.task.title}))
    c.suggestions.append(Suggestion(
        kind='no_lodging',
        text='当天往返，不安排住宿',
        patch={'lodging': ''}))
    return c


def overnight_notice(pack: CityPack, result: ScheduleResult,
                     next_result: Optional[ScheduleResult],
                     lodging: Optional[Poi] = None,
                     away_from_home: bool = False,
                     limit: int = 3) -> Optional[Conflict]:
    """一站式：判定 + 推荐 + 组装提示；不需要住宿时返回 None"""
    next_has = bool(next_result is not None and next_result.items)
    reason = detect_reason(result, next_has, lodging, away_from_home)
    if not reason:
        return None
    recs = recommend(pack, _anchor_points(result, next_result), limit=limit)
    return build_notice(reason, result, recs, pack.name)
