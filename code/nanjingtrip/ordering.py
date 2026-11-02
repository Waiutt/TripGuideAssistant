# -*- coding: utf-8 -*-
"""一天的顺序编排（纯函数，只改顺序、不改成员）。

为什么要单独一层
----------------
排序原来混在 `consult.build_plan()` 的选点循环里：既决定"选哪些点"，又顺手决定"什么顺序"。
一改顺序就可能连带改变成员集合（真实事故：调整排序后"同一地点两轮去重"的用例多出一条
"在总统府附近吃午饭"）。所以把排序拆出来，做成**只吃列表、只吐列表**的纯函数：
成员进出一致，顺序可以单独测。

三条规则（都有现实理由）
------------------------
1. **闭馆早的先排**：总统府 17:00 就关，排在最后一天就白去了；
2. **夜游类排最后**：1912、夫子庙、老门东这类街区的价值在晚上，放下午等于白排；
3. **同类内部就近串联**：串成一条线，避免横穿全城来回折返。
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence

#: 夜游/夜市类关键词：这些地方白天去没意思，默认当当天最后一段
EVENING_WORDS = ('夫子庙', '老门东', '1912', '夜市', '秦淮', '熙南里', '夜游', '酒吧')

#: 视为"没有营业时间"的值（城市包里缺失的点位被填成这两个数）
_UNKNOWN_HOURS = ('', None, '23:59', '00:00-23:59')


def is_evening(poi) -> bool:
    name = str(getattr(poi, 'name', '') or '')
    return any(word in name for word in EVENING_WORDS)


def closing_of(poi) -> str:
    """闭馆时刻（HH:MM）。取不到就当"关得最晚"，返回 '99:99' 用于排序"""
    raw = str(getattr(poi, 'open_to', '') or '')
    if raw in _UNKNOWN_HOURS or len(raw) < 4:
        return '99:99'
    return raw


def chain_nearest(group: Sequence, distance: Callable) -> list:
    """组内就近串联：从第一个点出发，每次挑离上一个最近的"""
    if len(group) <= 1:
        return list(group)
    line = [group[0]]
    rest = list(group[1:])
    while rest:
        nxt = min(rest, key=lambda candidate: distance(line[-1], candidate))
        rest.remove(nxt)
        line.append(nxt)
    return line


def order_day(pois: Iterable, distance: Optional[Callable] = None,
              evening: Optional[Callable] = None) -> list:
    """把一个点集合排成一天的顺序。

    · 成员**一个不增一个不减**（顺序是唯一产出）；
    · 白天点按闭馆时刻升序，夜游点整体挪到最后；
    · 每组内部按 `distance(a, b)` 就近串联（不传就保留原相对顺序）。
    """
    items = list(pois or [])
    if len(items) <= 1:
        return items
    is_eve = evening or is_evening
    dist = distance or (lambda a, b: 0)
    day_part = [p for p in items if not is_eve(p)]
    night = [p for p in items if is_eve(p)]
    # 闭馆早的先排；同一时刻保持原相对顺序（sort 是稳定的）
    day_part.sort(key=closing_of)
    return chain_nearest(day_part, dist) + chain_nearest(night, dist)
