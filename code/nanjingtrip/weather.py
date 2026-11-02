# -*- coding: utf-8 -*-
"""天气：预报取数与"湿不湿"的纯逻辑（不联网、可单测）。

诚实边界
--------
* 高德天气接口只给**今天 + 3 天**预报；窗口之外一律返回 None，
  由上层显示"无预报数据"，**不允许默认成晴**。
* 这里只做"读数据 + 判读"，不做任何猜测或补值。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

#: 含这些字样的天气算"湿"（雨/雪/雷/冰雹/冻雨）
_WET_WORDS = ('雨', '雪', '雷', '冰雹', '冻雨')


def parse_casts(payload: dict) -> list[dict]:
    """从高德 weatherInfo 返回里取出 casts 列表（容错：结构不对就返回空）"""
    out: list[dict] = []
    for block in (payload or {}).get('forecasts') or []:
        for c in block.get('casts') or []:
            if isinstance(c, dict) and c.get('date'):
                out.append(c)
    return out


def forecast_for(casts: list[dict], plan_date: date) -> Optional[dict]:
    """取出目标日期的预报；不在窗口内返回 None（不猜）"""
    key = plan_date.isoformat()
    for c in casts or []:
        if str(c.get('date'))[:10] == key:
            return c
    return None


def is_wet(cast: Optional[dict]) -> bool:
    """白天或夜间任一出现雨雪雷即为"湿"——用户晚上也可能还在外面"""
    if not cast:
        return False
    text = f"{cast.get('dayweather') or ''}{cast.get('nightweather') or ''}"
    return any(w in text for w in _WET_WORDS)


def summary(cast: Optional[dict]) -> str:
    """一行可读摘要，例如「中雨转小雨 21/13℃ 西北风 1-3 级」"""
    if not cast:
        return ''
    day = cast.get('dayweather') or ''
    night = cast.get('nightweather') or ''
    weather = day if day == night else f'{day}转{night}'
    t1, t2 = cast.get('daytemp') or '', cast.get('nighttemp') or ''
    temp = f'{t1}/{t2}℃' if t1 and t2 else ''
    wind = ''
    if cast.get('daywind'):
        wind = f"{cast['daywind']}风{cast.get('daypower') or ''}级"
    return ' '.join(x for x in (weather, temp, wind) if x)
