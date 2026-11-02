# -*- coding: utf-8 -*-
"""城市包字段完整性（离线）。

守住三件事，避免"加了个点反而把解析带偏"：
  1. 坐标落在中国范围内，且南京/北京各自的点位在自己的城市框里（防止复制粘贴串城）
  2. `open` 必须是合法的 (HH:MM, HH:MM) 且开始早于结束
  3. 别名在全库唯一——别名冲突会让 match_poi 命中错的那个点
"""
from __future__ import annotations

import re

import pytest

from nanjingtrip.cities import CITY_PACKS, get_city

#: 城市大致经纬度框（宽松，只用来抓"串城"这类错误）
BOX = {
    '南京': (118.3, 119.3, 31.2, 32.7),
    '北京': (115.4, 117.6, 39.4, 41.1),
}
HM = re.compile(r'^\d{2}:\d{2}$')


@pytest.mark.parametrize('city', sorted(CITY_PACKS))
def test_coords_inside_city_box(city):
    lng0, lng1, lat0, lat1 = BOX[city]
    for p in get_city(city).poi_table():
        assert lng0 < p['lng'] < lng1 and lat0 < p['lat'] < lat1, \
            f"{city}「{p['name']}」坐标越界：{p['lng']},{p['lat']}"


@pytest.mark.parametrize('city', sorted(CITY_PACKS))
def test_open_hours_format(city):
    for p in get_city(city).poi_table():
        hours = p.get('open') or ()
        if len(hours) != 2:
            continue
        o, c = hours
        assert HM.match(o) and HM.match(c), f"{city}「{p['name']}」营业时间格式不对：{o}-{c}"
        assert o < c, f"{city}「{p['name']}」开门晚于关门：{o}-{c}"


def test_alias_unique_across_packs():
    """别名重复会让解析命中错误的点——必须全库唯一"""
    seen: dict[str, str] = {}
    for city in sorted(CITY_PACKS):
        for p in get_city(city).poi_table():
            for alias in (p.get('aliases') or ()):
                key = f"{city}/{alias}"
                assert key not in seen, f"别名重复：{alias}（{seen[key]} 与 {p['name']}）"
                seen[key] = p['name']


def test_names_unique_within_pack():
    for city in sorted(CITY_PACKS):
        names = [p['name'] for p in get_city(city).poi_table()]
        dup = {n for n in names if names.count(n) > 1}
        assert not dup, f'{city} 存在重名点位：{dup}'
