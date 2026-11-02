"""包外地点：既不能漏，也不能"硬凑"（D-73 回归）

真实模拟时暴露的两类错误：
  · 「火星基地南京分基地」被凑成「南京基地」、「玄武区珠江路653号」被凑成「南京玄武外国语学校」
    —— 主路径拿了高德的模糊命中就直接用，没有名称校验；
  · 纯地址没有走地理编码（`geocode/geo`），而是拿门牌号当 POI 关键词去搜。

正确行为：
  · 名字对得上（含"南京晨光1865创意产业园"这种前后缀差异）→ 采用；
  · 名字对不上 → 不采信，交给调用方生成"待补地点"；
  · 明显是地址（含 号 + 路/街/道…）→ 走地理编码，落成"地址点位"；
  · 跨城的地址/地点 → BR-01 拒绝。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import Poi


DAY = date(2026, 12, 23)


class _FakeMap:
    """假高德：既能返回 POI 检索结果，也能返回地理编码结果"""

    provider = 'amap'

    def __init__(self, pois=None, geo=None):
        self.pois = pois or {}
        self.geo = geo or {}
        self.calls: list[tuple[str, str]] = []

    def search_poi(self, keyword, city=''):
        self.calls.append(('search', keyword))
        return list(self.pois.get(keyword, []))

    def geocode(self, address, city=''):
        self.calls.append(('geocode', address))
        return self.geo.get(address)

    def place_detail(self, keyword, city=''):
        return None

    def weather(self, adcode):
        return []


class _FakeDegrade:
    def __init__(self, mp):
        self.mp = mp

    def map_ready(self):
        return True

    def map(self):
        return self.mp

    def city(self):
        return '南京'


def _poi(name, district='玄武区', lng=118.79, lat=32.05):
    return {'name': name, 'address': '南京', 'district': district,
            'lng': lng, 'lat': lat, 'provider_poi_id': 'B0TEST', 'score': 90}


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _with_map(ctx, mp):
    ctx.geo_svc.degrade = _FakeDegrade(mp)
    return ctx.geo_svc


# ---------------- 名称校验：不许硬凑 ----------------
def test_garbage_name_is_rejected(ctx):
    """「火星基地南京分基地」不能被凑成「南京基地」"""
    svc = _with_map(ctx, _FakeMap(pois={'火星基地南京分基地': [_poi('南京基地')]}))
    assert svc.resolve('火星基地南京分基地') == []


def test_suffix_difference_is_accepted(ctx):
    """「南京晨光1865创意产业园」应命中同名的园区（只是多了"南京"前缀）"""
    svc = _with_map(ctx, _FakeMap(pois={
        '南京晨光1865创意产业园': [_poi('晨光1865创意产业园', '秦淮区')]}))
    got = svc.resolve('南京晨光1865创意产业园')
    assert got and got[0].name == '晨光1865创意产业园'


def test_short_prefix_difference_is_accepted(ctx):
    """「河西金鹰世界」→「金鹰世界」这种少一个片区前缀的，要认"""
    svc = _with_map(ctx, _FakeMap(pois={'河西金鹰世界': [_poi('金鹰世界', '建邺区')]}))
    got = svc.resolve('河西金鹰世界')
    assert got and got[0].name == '金鹰世界'


def test_unrelated_poi_not_used_for_address_text(ctx):
    """门牌号不能被当成"某个学校"的别名"""
    svc = _with_map(ctx, _FakeMap(
        pois={'玄武区珠江路653号': [_poi('南京玄武外国语学校')]},
        geo={'玄武区珠江路653号': {'lng': 118.79, 'lat': 32.05,
                                   'address': '江苏省南京市玄武区珠江路653号',
                                   'district': '玄武区'}}))
    got = svc.resolve('玄武区珠江路653号')
    assert got, '地址应该走地理编码而不是空手而归'
    assert '学校' not in got[0].name
    assert '珠江路653号' in (got[0].name + got[0].address)


# ---------------- 地址走地理编码 ----------------
def test_address_uses_geocode(ctx):
    mp = _FakeMap(geo={'玄武区珠江路653号': {
        'lng': 118.795, 'lat': 32.048, 'address': '江苏省南京市玄武区珠江路653号',
        'district': '玄武区'}})
    svc = _with_map(ctx, mp)
    got = svc.resolve('玄武区珠江路653号')
    assert got and abs(got[0].lng - 118.795) < 1e-6
    assert ('geocode', '玄武区珠江路653号') in mp.calls


def test_cross_city_address_is_rejected(ctx):
    """跑到别的城市的地址要按 BR-01 拒绝，而不是塞进南京行程"""
    svc = _with_map(ctx, _FakeMap(geo={'北京市东城区景山前街4号': {
        'lng': 116.397, 'lat': 39.918, 'address': '北京市东城区景山前街4号',
        'district': '东城区'}}))
    assert svc.resolve('北京市东城区景山前街4号') == []


def test_offline_address_returns_nothing(ctx):
    """离线时不猜：地址解析交给待补条目"""
    mp = _FakeMap(geo={})

    class _Offline(_FakeDegrade):
        def map_ready(self):
            return False

    ctx.geo_svc.degrade = _Offline(mp)
    assert ctx.geo_svc.resolve('玄武区珠江路653号') == []


# ---------------- 落库/排程侧：不采信就走待补 ----------------
def test_preview_turns_garbage_into_todo(ctx):
    svc = _with_map(ctx, _FakeMap(pois={'火星基地南京分基地': [_poi('南京基地')]}))
    ctx.trip_svc.geo_svc = svc
    out = ctx.trip_svc.preview_batch('火星基地南京分基地 1小时', DAY)
    items = out['items']
    assert items, out
    it = items[0]
    assert it['kind'] == 'todo' and it.get('needs_poi'), it
