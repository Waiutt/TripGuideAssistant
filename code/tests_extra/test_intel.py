"""取数管线：营业时间/票价/评分/推荐游玩时长必须来自真实数据，不许手填（D-67 回归）

背景：城市包里的 `visit`/`open_from`/`open_to` 是**演示种子**，写死就会过期；
更糟的是批量写入曾一律按 60 分钟兜底，把"南博 2.5 小时"当成 1 小时排进时间轴——
时间轴看着都装下了，现场全崩。所以这里断言三件事：
  ① 高德的 `biz_ext.open_time` 能被解析成真实的营业区间（含"24小时营业"、多时段）；
  ② 推荐游玩时长按类型/评分/营业窗口**估算**并标注来源，且不超过营业窗口的 80%；
  ③ 取到的值会落库缓存、覆盖种子数据，第二次不再联网。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.intel import (PoiIntelService, estimate_visit_min,
                               parse_closed_days, parse_open_time)


DAY = date(2026, 12, 16)


# ---------------- ① 营业时间解析 ----------------
@pytest.mark.parametrize('text,expect', [
    ('09:00-17:00', ('09:00', '17:00')),
    ('24小时营业', ('00:00', '23:59')),
    ('05:30-21:30 07:00-17:00 05:30-07:00', ('05:30', '21:30')),
    ('08:30-17:30;周一闭馆', ('08:30', '17:30')),
    ('', ('', '')),
    ('营业中', ('', '')),
])
def test_parse_open_time(text, expect):
    assert parse_open_time(text) == expect


def test_parse_closed_days():
    assert parse_closed_days('09:00-17:00;周一闭馆') == ('一',)
    assert parse_closed_days('09:00-17:00') == ()


# ---------------- ② 推荐时长估算 ----------------
def test_estimate_by_kind_and_rating():
    museum = estimate_visit_min('科教文化服务;博物馆;博物馆', 4.9, '09:00', '17:00')
    temple = estimate_visit_min('风景名胜;寺庙道观', 4.6, '07:00', '17:30')
    street = estimate_visit_min('风景名胜;特色街区', 4.5, '', '')
    assert museum > temple, (museum, temple)
    assert 40 <= temple <= 120
    assert 30 <= street <= 120


def test_estimate_never_exceeds_business_window():
    """"09:00-11:00"只开两小时的馆，不许按 120 分钟排（上限为窗口的 80%）"""
    v = estimate_visit_min('博物馆', 4.9, '09:00', '11:00')
    assert v <= 96, v
    # 无营业窗口信息时不夹（保持类型/评分口径）
    assert estimate_visit_min('博物馆', 4.9, '', '') > 96


# ---------------- ③ 取数 → 缓存 → 覆盖种子数据 ----------------
class _FakeDegrade:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def map_ready(self):
        return True

    def map(self):
        outer = self

        class _Ad:
            provider = 'amap'

            def place_detail(self, name, city=''):
                outer.calls += 1
                return outer.payload

        return _Ad()


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True          # 其它链路走离线，情报单独伪造
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_fetch_parses_and_caches(ctx):
    payload = {'name': '南京博物院', 'type': '科教文化服务;博物馆;博物馆',
               'biz_ext': {'open_time': '09:00-17:00;周一闭馆', 'cost': '0',
                           'rating': '4.9'}}
    intel = PoiIntelService(ctx.db, _FakeDegrade(payload))
    got = intel.get('南京博物院', '南京')
    assert got['open_from'] == '09:00' and got['open_to'] == '17:00'
    assert str(got['closed_days']).split(',') == ['一']
    assert int(got['visit_min']) > 60, got
    assert got['source'] == 'amap'
    # 第二次直接读缓存，不再调接口
    before = intel.degrade.calls
    again = intel.get('南京博物院', '南京')
    assert again['open_from'] == '09:00'
    assert intel.degrade.calls == before


def test_apply_overrides_pack_seed_data(ctx):
    payload = {'name': '南京博物院', 'type': '科教文化服务;博物馆;博物馆',
               'biz_ext': {'open_time': '10:00-16:00', 'cost': '0', 'rating': '4.9'}}
    intel = PoiIntelService(ctx.db, _FakeDegrade(payload))
    ctx.trip_svc.intel = intel
    ctx.geo_svc.intel = intel
    poi = ctx.trip_svc._poi_named('南京博物院')
    # 城市包里是 09:00-17:00；真实数据说 10:00-16:00，以真实为准
    assert (poi.open_from, poi.open_to) == ('10:00', '16:00')
    assert poi.visit_min and poi.visit_min <= 288


def test_offline_keeps_pack_values(ctx):
    """离线（无 Key / 不可用）时不取数，仍按城市包种子跑，不许崩"""
    class _Offline:
        def map_ready(self):
            return False

        def map(self):
            return None

    intel = PoiIntelService(ctx.db, _Offline())
    assert intel.get('南京博物院', '南京') is None
    ctx.trip_svc.intel = intel
    poi = ctx.trip_svc._poi_named('南京博物院')
    assert poi.open_from and poi.open_to         # 仍是城市包里的值


def test_default_duration_uses_poi_visit(ctx):
    """缺时长时按地点的推荐游玩时长兜底（而不是一律 60 分钟）"""
    out = ctx.trip_svc.preview_text('明天去南京博物院看展', DAY)
    assert out.ok, out.error
    assert out.draft.duration_min == 150
    assert any('推荐' in c.detail for c in (out.notices or []))
