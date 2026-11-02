# -*- coding: utf-8 -*-
"""城市包坐标校准：拿高德 Place 检索核对每个 POI 的经纬度。

背景（缺陷 D-27）：城市包里的经纬度是当初手写的粗略估算，抽查发现有的偏
300–1100 米，于是地图上的打点和实际位置对不上。本工具逐个查高德 Place 检索，
给出「现坐标 / 高德坐标 / 偏差」，确认后再写回 `cities.py`。

用法
----
    python tools/calibrate_poi_coords.py                 # 只出报表（南京+北京）
    python tools/calibrate_poi_coords.py --city 南京      # 只看一个城市
    python tools/calibrate_poi_coords.py --apply         # 写回 cities.py（会先备份）

判定规则（宁可漏改，不可乱改）
------------------------------
* 检索结果的名称与包内名称完全一致 → 采纳；
* 结果名称包含包内名称（或反之）且距离旧坐标 < 3 km → 采纳；
* 其余（搜不到 / 名称不符 / 距离离谱）→ 跳过并标注原因，交人工决定。
"""
from __future__ import annotations

import argparse
import math
import re
import shutil
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'code'))

from nanjingtrip.app import AppContext          # noqa: E402
from nanjingtrip.cities import CITY_PACKS       # noqa: E402

CITIES_SRC = ROOT / 'code' / 'nanjingtrip' / 'cities.py'
THRESHOLD_M = 120          # 偏差小于这个就不动
SANE_M = 3000              # 新旧坐标相距超过这个就认为匹配不可信


def meters(a, b):
    dx = (a[0] - b[0]) * 111320 * math.cos(math.radians(a[1]))
    dy = (a[1] - b[1]) * 110540
    return math.hypot(dx, dy)


def place_search(key, name: str, city: str) -> tuple[tuple[float, float] | None, str]:
    """高德 Place 文本检索，返回 (坐标, 说明)"""
    p = {'key': key, 'keywords': name, 'city': city, 'citylimit': 'true',
         'offset': '5', 'page': '1', 'extensions': 'base'}
    try:
        r = requests.get('https://restapi.amap.com/v3/place/text', params=p,
                         timeout=15).json()
    except Exception as exc:                      # noqa: BLE001
        return None, f'请求失败：{exc}'
    if r.get('status') != '1':
        return None, str(r.get('info'))
    pois = r.get('pois') or []
    if not pois:
        return None, '无结果'
    best = None
    for item in pois:
        try:
            lng, lat = item['location'].split(',')
        except (KeyError, ValueError):
            continue
        nm = item.get('name') or ''
        score = 0
        if nm == name:
            score = 3
        elif name in nm or nm in name:
            score = 2
        elif any(a in nm for a in ()):
            score = 1
        if score <= 0:
            continue
        cand = (float(lng), float(lat), nm, score, item.get('address') or '')
        if best is None or score > best[3]:
            best = cand
    if best is None:
        return None, '名称不匹配'
    return (best[0], best[1]), best[2]


def patch_source(name: str, lng: float, lat: float) -> bool:
    """把 cities.py 里某个 POI 的 lng/lat 换成新值（按 name 定位，只改一处）"""
    text = CITIES_SRC.read_text(encoding='utf-8')
    pat = re.compile(
        r"('name':\s*'" + re.escape(name) + r"'.*?'lng':\s*)"
        r"(-?\d+\.\d+)(,\s*'lat':\s*)(-?\d+\.\d+)", re.S)
    m = pat.search(text)
    if not m:
        return False
    new = f'{m.group(1)}{lng:.5f}{m.group(3)}{lat:.5f}'
    CITIES_SRC.write_text(text[:m.start()] + new + text[m.end():], encoding='utf-8')
    return True


def geocode_addr(key, addr: str, city: str):
    """按门牌地址地理编码（与名称检索互相独立的第二个来源）"""
    if not addr:
        return None
    p = {'key': key, 'address': f'{city}{addr}', 'city': city}
    try:
        r = requests.get('https://restapi.amap.com/v3/geocode/geo', params=p,
                         timeout=15).json()
    except Exception:                             # noqa: BLE001
        return None
    if r.get('status') == '1' and r.get('geocodes'):
        lng, lat = r['geocodes'][0]['location'].split(',')
        return float(lng), float(lat)
    return None


def place_around(key, lng: float, lat: float, keywords: str,
                 radius: int = 3000):
    """周边搜索：以现有（粗）坐标为圆心，在半径内找关键词匹配的点。

    这一步专门解决名称检索失败的情形——连锁分店、机场、景区入口这类 POI，
    全城重名太多，但在"已知大概位置"附近搜就唯一了。
    """
    p = {'key': key, 'location': f'{lng:.6f},{lat:.6f}', 'keywords': keywords,
         'radius': str(max(100, min(50000, radius))), 'offset': '10', 'page': '1'}
    try:
        r = requests.get('https://restapi.amap.com/v3/place/around', params=p,
                         timeout=15).json()
    except Exception:                             # noqa: BLE001
        return []
    if r.get('status') != '1':
        return []
    out = []
    for item in r.get('pois') or []:
        try:
            x, y = item['location'].split(',')
        except (KeyError, ValueError):
            continue
        out.append((float(x), float(y), item.get('name') or ''))
    return out


def brand_key(name: str) -> str:
    """从"小厨娘淮扬菜新街口店"里取一个够用的搜索词：去掉括号与"X店"后缀。"""
    base = re.sub(r'[（(].*?[)）]', '', name)
    base = re.sub(r'(新街口|夫子庙|太平南路|湖南路|德基|前门|王府井|三里屯|'
                  r'崇文门|鼓楼|团结湖|簋街|护国寺|北京南站|故宫)店$', '', base)
    base = re.sub(r'店$', '', base)
    return base[:8] or name[:6]


def similar(a: str, b: str) -> int:
    """两个名字的最长公共子串长度（粗判"是不是同一个地方"）"""
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def acceptable(name: str, nm: str) -> bool:
    """周边搜索结果的收敛规则：宁可漏改，不可乱改。

    * 停业/关闭的点位直接丢掉；
    * 只接受「同名」或「包内名字被包含且只多两个字」的结果，
      避免把坐标吸到子地点上（如"禄口国际机场公务机楼""奥体中心出口"
      "明孝陵景区-明孝陵"这类）。
    """
    if any(k in nm for k in ('暂停', '关闭', '停业', '已拆除')):
        return False
    if nm == name:
        return True
    return name in nm and len(nm) - len(name) <= 2


def run(city: str, key: str, apply: bool) -> tuple[int, int, int]:
    pack = CITY_PACKS[city]
    pois = pack.pois
    print(f'\n===== {city}（{len(pois)} 个 POI）=====')
    print('{:<18}{:<24}{:<24}{:>7}  {}'.format('名称', '现坐标', '校准坐标', '偏差', '依据'))
    fixed = skipped = same = 0
    for poi in pois:
        name = poi['name']
        old = (poi['lng'], poi['lat'])
        c1, note = place_search(key, name, city)
        time.sleep(0.22)                          # 个人 Key 限流：3 次/秒
        new = None
        basis = ''
        if c1:
            c2 = geocode_addr(key, poi.get('address', ''), city)
            agree = meters(c1, c2) if c2 else None
            if agree is not None and agree <= 300:
                new, basis = c1, f'双源一致（地址源差 {agree:.0f} m）'
            elif note == name and meters(old, c1) < 800:
                new, basis = c1, f'名称精确匹配（地址源{"无" if c2 is None else "不一致"}）'
            else:
                basis = ''                        # 证据不足，落到周边搜索
        if new is None:
            # 第三道策略：以现有坐标为圆心做周边搜索（连锁分店、机场、景区靠它）
            for kw in (name, brand_key(name)):
                cands = place_around(key, old[0], old[1], kw)
                time.sleep(0.22)
                good = [(meters(old, (x, y)), (x, y), nm) for x, y, nm in cands
                        if acceptable(name, nm)]
                if good:
                    d, new, nm = min(good)
                    basis = f'周边搜索「{kw}」→ {nm}'
                    break
        if new is None:
            print('{:<18}{:<24}{:<24}{:>7}  跳过（名称检索与周边搜索都无匹配）'.format(
                name, f'{old[0]:.5f},{old[1]:.5f}', '—', '—'))
            skipped += 1
            continue
        d = meters(old, new)
        if d < THRESHOLD_M:
            print('{:<18}{:<24}{:<24}{:>6.0f}m  一致'.format(
                name, f'{old[0]:.5f},{old[1]:.5f}', f'{new[0]:.5f},{new[1]:.5f}', d))
            same += 1
            continue
        if d > SANE_M:
            print('{:<18}{:<24}{:<24}{:>6.0f}m  跳过（距离过大，疑同名异地：{}）'.format(
                name, f'{old[0]:.5f},{old[1]:.5f}', f'{new[0]:.5f},{new[1]:.5f}', d, basis))
            skipped += 1
            continue
        mark = '已写回' if apply and patch_source(name, *new) else '待确认'
        print('{:<18}{:<24}{:<24}{:>6.0f}m  {} ← {}'.format(
            name, f'{old[0]:.5f},{old[1]:.5f}', f'{new[0]:.5f},{new[1]:.5f}', d, mark, basis))
        fixed += 1
    print(f'-- {city}：需修正 {fixed}，本来就准 {same}，跳过 {skipped}')
    return fixed, same, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='城市包坐标校准')
    ap.add_argument('--city', default=None, help='只处理某个城市')
    ap.add_argument('--apply', action='store_true', help='写回 cities.py（先备份）')
    a = ap.parse_args(argv)

    ctx = AppContext()
    key = ctx.settings.map_api_key
    if not key:
        print('没有地图 Key（%LOCALAPPDATA%\\NanjingTrip\\secrets.json），无法校准')
        return 2
    if a.apply:
        bak = CITIES_SRC.with_suffix('.py.bak')
        shutil.copy2(CITIES_SRC, bak)
        print(f'已备份到 {bak}')

    cities = [a.city] if a.city else list(CITY_PACKS)
    total = [0, 0, 0]
    for c in cities:
        f, s, k = run(c, key, a.apply)
        total = [total[0] + f, total[1] + s, total[2] + k]
    print(f'\n合计：需修正 {total[0]}，本来就准 {total[1]}，跳过 {total[2]}')
    ctx.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
