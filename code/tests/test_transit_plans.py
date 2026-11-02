# -*- coding: utf-8 -*-
"""公交换乘方案详情的测试（对应报告第 4 章 TC-17-*）。

用构造的响应验证解析逻辑，不打真实接口，保证可重复。
"""
import json
from datetime import date

import pytest

from nanjingtrip.adapters.amap import AmapAdapter
from nanjingtrip.app import AppContext

D = date(2026, 10, 28)

FAKE_TRANSIT = {
    'status': '1',
    'info': 'OK',
    'route': {
        'transits': [
            {
                'duration': '1980', 'cost': '3.0', 'nightflag': '0',
                'segments': [
                    {'walking': {'distance': '320', 'duration': '240'}},
                    {'bus': {'buslines': [{
                        'name': '地铁2号线(油坊桥--经天路)',
                        'departure_stop': {'name': '明故宫'},
                        'arrival_stop': {'name': '新街口'},
                        'via_num': '3', 'duration': '600', 'distance': '4200',
                        'start_time': '05:30', 'end_time': '22:45'}]}},
                    {'walking': {'distance': '180', 'duration': '150'}},
                    {'bus': {'buslines': [{
                        'name': '地铁1号线(迈皋桥--中国药科大学)',
                        'departure_stop': {'name': '新街口'},
                        'arrival_stop': {'name': '鼓楼'},
                        'via_num': '2', 'duration': '420', 'distance': '2600',
                        'start_time': '05:45', 'end_time': '23:00'}]}},
                    {'walking': {'distance': '260', 'duration': '200'}},
                ],
            },
            {
                'duration': '2400', 'cost': '2.0', 'nightflag': '1',
                'segments': [
                    {'walking': {'distance': '500', 'duration': '400'}},
                    {'bus': {'buslines': [{
                        'name': '公交34路',
                        'departure_stop': {'name': '中山门'},
                        'arrival_stop': {'name': '鼓楼公园'},
                        'via_num': '9', 'duration': '1500', 'distance': '8600',
                        'start_time': '06:00', 'end_time': '21:30'}]}},
                ],
            },
        ]
    },
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


# ---------------------------------------------------------------- TC-17-01
def test_transit_plans_parsing(monkeypatch):
    """TC-17-01 解析换乘方案：线路、上下车站、站数、首末班时间、步行距离"""
    import nanjingtrip.adapters.amap as amap_mod

    monkeypatch.setattr(amap_mod.requests, 'get',
                        lambda *a, **k: _Resp(FAKE_TRANSIT))
    a = AmapAdapter('fake-key', city='南京')
    plans = a.transit_plans((118.83, 32.04), (118.78, 32.06))

    assert len(plans) == 2
    p1, p2 = plans
    assert p1['duration_min'] == 33          # 1980 秒
    assert p1['transfers'] == 1              # 两段线路
    assert p1['walking_m'] == 320 + 180 + 260
    assert p1['cost'] == '3.0'
    assert p1['lines'][0]['name'].startswith('地铁2号线')
    assert p1['lines'][0]['from_stop'] == '明故宫'
    assert p1['lines'][0]['to_stop'] == '新街口'
    assert p1['lines'][0]['stops'] == 3
    assert p1['lines'][0]['start_time'] == '05:30'
    assert p1['lines'][0]['end_time'] == '22:45'
    assert p2['transfers'] == 0 and p2['night'] is True


# ---------------------------------------------------------------- TC-17-02
def test_transit_plans_requires_key():
    """TC-17-02 未配置密钥时明确报错，不返回空方案冒充成功"""
    from nanjingtrip.adapters.amap import MapError

    a = AmapAdapter('')
    with pytest.raises(MapError):
        a.transit_plans((118.8, 32.0), (118.7, 32.0))


# ---------------------------------------------------------------- TC-17-03
def test_transit_plans_offline_message(tmp_path):
    """TC-17-03 离线模式：说明需联网，不臆造线路数据"""
    from nanjingtrip.models import Poi

    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    plans, note = c.transit_svc.transit_plans(
        Poi(name='A', district='玄武区', lng=118.8, lat=32.0, poi_id=1),
        Poi(name='B', district='鼓楼区', lng=118.7, lat=32.0, poi_id=2))
    assert plans == []
    assert '离线' in note and '联网' in note
    c.close()


# ---------------------------------------------------------------- TC-17-04
def test_plans_for_task_needs_previous_place(tmp_path):
    """TC-17-04 当天第一项没有前置地点 → 明确说明，不乱查"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    out = c.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    task = out.result.ordered()[0].task
    plans, note = c.trip_svc.transit_plans_for(task.task_id)
    assert plans == [] and '第一项' in note
    c.close()


# ---------------------------------------------------------------- TC-18-01
def test_headway_guessing_and_hhmm_parsing():
    """TC-18-01 线路类型→经验发车间隔；运营时间兼容 '0600' 与 '06:00'"""
    from nanjingtrip.traffic import (_parse_hhmm, expected_wait,
                                     guess_headway)

    assert guess_headway('地铁2号线(经天路--鱼嘴)') == (3, 6)
    assert guess_headway('公交34路') == (8, 15)
    assert guess_headway('34路(中山陵停车场--中山码头)') == (8, 15)   # "N路"识别为公交
    assert guess_headway('机场大巴专线') == (10, 20)                   # 无法识别用默认
    assert _parse_hhmm('0600') == 6 * 60
    assert _parse_hhmm('06:00') == 6 * 60
    assert _parse_hhmm('2300') == 23 * 60
    assert _parse_hhmm('') is None
    assert expected_wait((3, 6)) == 2        # 约半间隔的一半
    assert expected_wait((8, 15)) == 6


# ---------------------------------------------------------------- TC-18-02
def test_next_bus_and_annotate_plans():
    """TC-18-02 推算下一班与期望等待，并标注为估算"""
    from nanjingtrip.traffic import annotate_plans, next_bus

    # 08:00 出发，地铁首班 06:00、间隔 3-6 取平均 5 → 06:00 起每 5 分钟一班
    bus_min, note = next_bus(8 * 60, 6 * 60, 23 * 60, (3, 6))
    assert bus_min == 8 * 60 and '推算' in note

    # 08:02 出发 → 下一班 08:05
    bus_min2, _ = next_bus(8 * 60 + 2, 6 * 60, 23 * 60, (3, 6))
    assert bus_min2 == 8 * 60 + 5

    plans = [{
        'duration_min': 33, 'walking_m': 900, 'cost': '4.0', 'transfers': 1,
        'night': False,
        'lines': [
            {'name': '34路', 'from_stop': 'A', 'to_stop': 'B', 'stops': 5,
             'minutes': 6, 'start_time': '0600', 'end_time': '2130'},
            {'name': '地铁2号线', 'from_stop': 'B', 'to_stop': 'C', 'stops': 3,
             'minutes': 10, 'start_time': '06:00', 'end_time': '23:00'},
        ],
    }]
    out = annotate_plans(plans, 8 * 60)[0]
    assert len(out['lines']) == 2
    for line in out['lines']:
        assert 'next_bus_label' in line and line['next_bus_label'] != '—'
        assert 'estimate_note' in line
        assert line['headway_range'] in ((3, 6), (8, 15))
    assert out['estimate_wait_min'] >= 0
    assert out['estimate_total_min'] == out['duration_min'] + out['estimate_wait_min']


# ---------------------------------------------------------------- TC-18-03
def test_plans_are_labelled_as_estimate(tmp_path, monkeypatch):
    """TC-18-03 方案详情必须声明"班次为推算、非实时"，不得冒充实时数据"""
    from nanjingtrip.models import Poi, Task, TaskStatus
    from nanjingtrip.traffic import annotate_plans

    c = AppContext(tmp_path)
    c.settings.offline_mode = False
    c.settings.llm_api_key = 'k'
    c.settings.map_api_key = 'k'
    c.apply_settings(c.settings)

    class FakeMap:
        def name(self):
            return '高德地图'

        def search_poi(self, keyword, city='南京'):
            return []

        def route(self, origin, dest, mode):
            return {'duration_min': 20, 'distance_m': 5000}

        def transit_plans(self, origin, dest, limit=3):
            return [{'duration_min': 25, 'walking_m': 400, 'cost': '3.0',
                     'transfers': 0, 'night': False,
                     'lines': [{'name': '地铁2号线', 'from_stop': 'A',
                                'to_stop': 'B', 'stops': 4, 'minutes': 12,
                                'start_time': '0600', 'end_time': '2300'}]}]

    c.degrade._map = FakeMap()
    a = Poi(name='A', district='玄武区', lng=118.8, lat=32.0, poi_id=1)
    b = Poi(name='B', district='鼓楼区', lng=118.7, lat=32.0, poi_id=2)
    c.trip_repo.ensure_poi(a)
    c.trip_repo.ensure_poi(b)

    # 两项都通过 commit_task 落库，保证"前一项有地点"这一前提成立
    c.trip_svc.commit_task(D, Task(title='出发', duration_min=30, poi=a,
                                   is_locked=True,
                                   start_ts=__import__('datetime').time(9, 0),
                                   status=TaskStatus.SCHEDULED), note='测试1')
    c.trip_svc.commit_task(D, Task(title='到达', duration_min=30, poi=b,
                                   is_locked=True,
                                   start_ts=__import__('datetime').time(10, 30)),
                           note='测试2')
    target = [t for t in c.trip_svc.day_view(D).tasks if t.title == '到达'][0]
    plans, note = c.trip_svc.transit_plans_for(target.task_id)
    assert plans, note
    assert '推算' in note and '非实时' in note
    assert plans[0]['lines'][0]['next_bus_label'] != '—'
    c.close()
