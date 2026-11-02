# -*- coding: utf-8 -*-
"""离线演示数据种子：一键生成一天的真实排程，便于答辩演示与报告截图（FR-13）。

演示话术随城市包切换：在南京时填南京的一天，切到北京则填北京的一天
（含一顿饭与一晚住宿，正好演示"吃/住/玩/行"四类）。
"""
from __future__ import annotations

from datetime import date, timedelta

from .cities import DEFAULT_CITY, get_city

# 每座城市一天的演示话术：(相对天数, 一句话)
DEMO_TEXTS_BY_CITY: dict[str, list[tuple[int, str]]] = {
    '南京': [
        (0, '上午九点去南京博物院看展，大概2小时'),
        (0, '中午去新街口吃饭，大约1小时'),
        (0, '下午去鼓楼医院取报告，半小时'),
        (0, '晚上去夫子庙逛逛，1个半小时'),
        (1, '上午去玄武湖公园散步，大约1小时'),
        (1, '下午去先锋书店看书，2小时'),
    ],
    '北京': [
        (0, '上午八点半去故宫博物院看展，大概3小时'),
        (0, '中午去四季民福烤鸭店故宫店吃饭，1个半小时'),
        (0, '下午去中国国家博物馆看展，3小时'),
        (0, '晚上去什刹海逛逛，1个半小时'),
        (1, '上午去天坛公园逛逛，2小时'),
        (1, '中午去姚记炒肝鼓楼店吃饭，1小时'),
        (1, '下午去798艺术区看展，2小时'),
    ],
}

# 兼容旧引用：默认城市的演示话术
DEMO_TEXTS: list[tuple[int, str]] = DEMO_TEXTS_BY_CITY[DEFAULT_CITY]

# 每座城市的演示住宿（设在第一天，演示"住"的锚点）
DEMO_LODGING: dict[str, str] = {
    '南京': '金陵饭店',
    '北京': '王府井希尔顿酒店',
}


def demo_texts(city: str = '') -> list[tuple[int, str]]:
    return DEMO_TEXTS_BY_CITY.get(get_city(city).name, DEMO_TEXTS)


def seed_demo(ctx, base_date: date | None = None) -> list[str]:
    """写入演示行程（按当前城市），返回每条的排程结果摘要"""
    base = base_date or date.today()
    city = get_city(ctx.city).name
    logs: list[str] = []

    hotel = DEMO_LODGING.get(city, '')
    if hotel:
        first = base
        out = ctx.trip_svc.set_lodging(first, hotel)
        logs.append(f'{first} {"✓" if out.ok else "✗"} 住宿：{hotel}'
                    + ('' if out.ok else f' → {out.error}'))

    for offset, text in demo_texts(city):
        d = base + timedelta(days=offset)
        outcome = ctx.trip_svc.add_text(text, d)
        if outcome.ok:
            logs.append(f'{d} ✓ {text} → {outcome.explanation}')
        else:
            logs.append(f'{d} ✗ {text} → {outcome.error}')
    return logs


__maintenance__ = {clear_demo}   # 手工维护入口：需要清空演示数据时调用
