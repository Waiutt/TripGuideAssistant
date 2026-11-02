# -*- coding: utf-8 -*-
"""离线适配器（Mock）：内置城市包 POI 与规则解析，保证断网可完整演示（FR-13）。

POI 数据不再写在本模块，而是来自 :mod:`nanjingtrip.cities` 的城市包，
因此"换城市"对离线适配器同样只是一次参数变化。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

from ..cities import DEFAULT_CITY, get_city
from ..parsing_rules import rule_based_parse

# 兼容旧引用：南京市离线 POI 表与别名表
NANJING_POIS: list[dict] = get_city('南京').poi_table()
POI_ALIASES: dict[str, str] = get_city('南京').aliases()

# 不同交通方式的平均速度（km/h）与道路绕行系数
_MODE_SPEED = {'driving': 24.0, 'transit': 16.0, 'walking': 4.8,
               'bicycling': 13.0}
_MODE_DETOUR = {'driving': 1.35, 'transit': 1.30, 'walking': 1.15,
                'bicycling': 1.20}


def haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（公里）"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class MockMapAdapter:
    """离线地图适配器：POI 检索（子串/别名匹配）+ 距离估算路径规划"""

    provider = 'mock'

    def __init__(self, city: str = DEFAULT_CITY):
        self.city = city or DEFAULT_CITY

    def name(self) -> str:
        return f'离线内置 POI（{self.city}）'

    def geocode(self, address: str, city: str = '') -> Optional[dict]:
        """离线不做地理编码：地址交给"待补地点"，不猜"""
        return None

    def weather(self, adcode: str) -> list[dict]:
        """离线没有预报数据：返回空，由上层显示"无预报数据"，绝不猜成晴"""
        return []

    def search_poi(self, keyword: str, city: str = '') -> list[dict]:
        keyword = (keyword or '').strip()
        if not keyword:
            return []
        table = get_city(city or self.city).poi_table()
        hits = []
        for p in table:
            score = 0
            if keyword == p['name']:
                score = 100
            elif keyword in p['name']:
                score = 80
            elif p['name'] in keyword:
                score = 70
            elif any(keyword in a or a in keyword
                     for a in p.get('aliases', ())):
                score = 90
            if score:
                hits.append({**p, 'score': score})
        hits.sort(key=lambda x: -x['score'])
        return hits[:5]

    def route(self, origin: tuple[float, float], dest: tuple[float, float],
              mode: str) -> Optional[dict]:
        if not origin or not dest:
            return None
        dist = haversine_km(origin[0], origin[1], dest[0], dest[1])
        detour = _MODE_DETOUR.get(mode, 1.3)
        speed = _MODE_SPEED.get(mode, 20.0)
        road_km = dist * detour
        minutes = max(3, int(round(road_km / speed * 60)))
        return {'duration_min': minutes, 'distance_m': int(road_km * 1000)}

    def resolve_route_only(self, a: dict, b: dict, mode: str) -> Optional[dict]:
        return self.route((a['lng'], a['lat']), (b['lng'], b['lat']), mode)


class MockLLMAdapter:
    """离线大模型适配器：用规则解析模拟 LLM 的结构化输出"""

    provider = 'mock'

    def __init__(self, today: Optional[date] = None,
                 city: str = DEFAULT_CITY):
        self._today = today
        self.city = city or DEFAULT_CITY

    def name(self) -> str:
        return f'离线规则解析（{self.city}）'

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        today = self._today or date.today()
        pack = get_city(self.city)
        return rule_based_parse(user, today, pack.names(), pack.aliases())

    def chat(self, messages: list[dict], temperature: float = 0.6) -> str:
        """离线问答：仅能做确定性回应，通用问答需配置在线大模型。

        真正的离线答复由 AssistantService 依据本地行程数据生成，
        这里只作为端口契约的兜底实现，保证接口形状一致。
        """
        last = ''
        for m in reversed(messages):
            if m.get('role') == 'user':
                last = m.get('content', '')
                break
        if any(k in last for k in ('安排', '行程', '计划')):
            return ('【离线模式】我可以读取本地行程并告诉你已有的安排；'
                    '若要我用自然语言自由问答，请在「设置」中填入大模型 API Key。')
        return ('【离线模式】通用问答需要在线大模型支持。当前仍可使用右侧'
                '「行程录入」用一句话安排行程，系统会自动排程并存入本地数据库。')

