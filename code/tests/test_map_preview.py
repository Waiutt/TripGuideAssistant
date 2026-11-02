# -*- coding: utf-8 -*-
"""位置示意图（高德静态地图）的测试（对应报告第 4 章 TC-21-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.adapters.amap import AmapAdapter
from nanjingtrip.app import AppContext
from nanjingtrip.models import Poi, Task, TaskStatus, TransitMode

D = date(2026, 10, 28)
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


class _Resp:
    def __init__(self, content: bytes, ctype='image/png', status=200):
        self.content = content
        self.headers = {'Content-Type': ctype}
        self.status_code = status


# ---------------------------------------------------------------- TC-21-01
def test_static_map_base_builds_params(monkeypatch):
    """TC-21-01 底图请求：中心点、zoom、尺寸正确；不再尝试 markers/paths"""
    import nanjingtrip.adapters.amap as amap_mod

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured['url'] = url
        captured['params'] = params
        return _Resp(PNG_MAGIC + b'rest')

    monkeypatch.setattr(amap_mod.requests, 'get', fake_get)
    a = AmapAdapter('fake-key')
    data = a.static_map_base((118.82, 32.04), size=(900, 380), zoom=13)
    assert data.startswith(PNG_MAGIC)
    assert '/staticmap' in captured['url']
    p = captured['params']
    assert p['location'] == '118.820000,32.040000'
    assert p['zoom'] == 13
    assert p['size'] == '900*380'
    assert p['scale'] == 1
    # 实测该接口的 paths 参数恒报 20003，故不再发送
    assert 'paths' not in p and 'markers' not in p


# ---------------------------------------------------------------- TC-21-02
def test_static_map_rejects_non_image(monkeypatch):
    """TC-21-02 返回非图片（如鉴权错误 JSON）→ 明确报错，不当作图片用"""
    import nanjingtrip.adapters.amap as amap_mod
    from nanjingtrip.adapters.amap import MapError

    monkeypatch.setattr(amap_mod.requests, 'get',
                        lambda *a, **k: _Resp(b'{"status":"0"}',
                                              ctype='application/json'))
    a = AmapAdapter('fake-key')
    with pytest.raises(MapError):
        a.static_map((118.8, 32.0), [(118.8, 32.0)])


# ---------------------------------------------------------------- TC-21-03
def test_day_map_offline_message(tmp_path):
    """TC-21-03 离线模式：说明需联网，不返回假图"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    c.trip_svc.add_text('上午九点去南京博物院看展，大概1小时', D)
    data, note = c.trip_svc.day_map_image(D)
    assert data == b''
    assert '离线' in note
    c.close()


# ---------------------------------------------------------------- TC-21-04
def test_day_map_empty_day(tmp_path):
    """TC-21-04 当天没有带地点的安排 → 明确说明"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    data, note = c.trip_svc.day_map_image(D)
    assert data == b'' and '还没有' in note
    c.close()


# ---------------------------------------------------------------- TC-21-05
def test_day_map_points_follow_time_order(tmp_path):
    """TC-21-05 连线点顺序 = 当天时间顺序（不是录入顺序），且点集读取不涉网络"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)

    late = Poi(name='P晚', district='鼓楼区', lng=118.70, lat=32.00, poi_id=1)
    early = Poi(name='P早', district='玄武区', lng=118.90, lat=32.10, poi_id=2)
    c.trip_repo.ensure_poi(late)
    c.trip_repo.ensure_poi(early)
    c.trip_svc.commit_task(D, Task(title='晚', duration_min=30, poi=late,
                                   is_locked=True, start_ts=time(15, 0)),
                           note='t1')
    c.trip_svc.commit_task(D, Task(title='早', duration_min=30, poi=early,
                                   is_locked=True, start_ts=time(9, 0)),
                           note='t2')

    points, note = c.trip_svc.day_map_points(D)
    assert note == ''
    assert points[0]['title'] == '早'          # 9:00 的地点在前
    assert (points[0]['lng'], points[0]['lat']) == (118.90, 32.10)
    assert points[1]['title'] == '晚'
    assert points[0]['time'] == '09:00'
    c.close()


# ---------------------------------------------------------------- TC-21-06
def test_static_map_bytes_is_db_free(tmp_path):
    """TC-21-06 线程里用的取图方法不得访问数据库（否则 sqlite 跨线程报错）"""
    from nanjingtrip.models import Poi

    c = AppContext(tmp_path)
    c.settings.offline_mode = False
    c.settings.map_api_key = 'k'
    c.settings.llm_api_key = 'k'
    c.apply_settings(c.settings)

    class FakeMap:
        def name(self):
            return '高德地图'

        def static_map_base(self, center, size=(900, 380), zoom=12, scale=1):
            return PNG_MAGIC + b'ok'

    c.degrade._map = FakeMap()

    def _boom(*_a, **_k):
        raise AssertionError('工作线程中不应访问数据库')

    c.trip_svc.day_view = _boom           # 一旦被调用即失败
    data, note = c.trip_svc.static_map_bytes((118.75, 32.0), size=(900, 380),
                                             zoom=12)
    assert data.startswith(PNG_MAGIC)
    assert note == ''
    c.close()


# ---------------------------------------------------------------- TC-21-07
def test_geo_projection_and_fit_zoom():
    """TC-21-07 墨卡托投影与自适应 zoom：中心点落在画面正中，点越散 zoom 越小"""
    from nanjingtrip.geo import bbox_center, fit_zoom, project

    size = (900, 380)
    center = (118.78, 32.04)
    cx, cy = project(center[0], center[1], center, 12, size)
    assert abs(cx - size[0] / 2) < 0.001 and abs(cy - size[1] / 2) < 0.001

    # 同一点系（相距约 1 公里）应允许更大 zoom
    near = [(118.78, 32.04), (118.79, 32.05)]
    far = [(118.78, 32.04), (118.95, 32.12)]
    assert fit_zoom(near, size) > fit_zoom(far, size)

    c = bbox_center(far)
    assert c[0] == (118.78 + 118.95) / 2 and c[1] == (32.04 + 32.12) / 2


# ---------------------------------------------------------------- TC-21-08
def test_day_map_image_uses_fit_zoom(tmp_path):
    """TC-21-08 day_map_image 会用自适应 zoom 请求底图"""
    from datetime import time

    from nanjingtrip.models import Poi, Task

    c = AppContext(tmp_path)
    c.settings.offline_mode = False
    c.settings.map_api_key = 'k'
    c.settings.llm_api_key = 'k'
    c.apply_settings(c.settings)
    seen = {}

    class FakeMap:
        def name(self):
            return '高德地图'

        def route(self, origin, dest, mode):
            return {'duration_min': 10, 'distance_m': 2000}

        def static_map_base(self, center, size=(900, 380), zoom=12, scale=1):
            seen['zoom'] = zoom
            seen['size'] = size
            return PNG_MAGIC + b'x'

    c.degrade._map = FakeMap()
    p1 = Poi(name='A', district='玄武区', lng=118.80, lat=32.04, poi_id=1)
    p2 = Poi(name='B', district='鼓楼区', lng=118.79, lat=32.05, poi_id=2)
    c.trip_repo.ensure_poi(p1)
    c.trip_repo.ensure_poi(p2)
    c.trip_svc.commit_task(D, Task(title='A', duration_min=30, poi=p1,
                                   is_locked=True, start_ts=time(9, 0)), note='a')
    c.trip_svc.commit_task(D, Task(title='B', duration_min=30, poi=p2,
                                   is_locked=True, start_ts=time(10, 0)), note='b')

    data, note = c.trip_svc.day_map_image(D)
    assert data.startswith(PNG_MAGIC)
    assert 9 <= seen['zoom'] <= 16
    c.close()
