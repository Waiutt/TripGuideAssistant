# -*- coding: utf-8 -*-
"""具体路径详情（含骑行接驳）的测试（对应报告第 4 章 TC-23-*）。"""
from datetime import date, time

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import Poi, Task, TaskStatus

D = date(2026, 10, 28)


def _poi(name, lng, lat, pid):
    return Poi(name=name, district='玄武区', lng=lng, lat=lat,
               provider='mock', provider_poi_id=name, poi_id=pid)


class FakeMap:
    """模拟在线地图：给出公交方案（含站点经纬度）与分步路径"""

    def name(self):
        return '高德地图'

    def route(self, origin, dest, mode):
        return {'duration_min': 30, 'distance_m': 9000}

    def route_steps(self, origin, dest, mode):
        if mode == 'bicycling':
            return {'duration_min': 8, 'distance_m': 2100,
                    'steps': [{'instruction': '向西南骑行 2.1 公里', 'distance_m': 2100,
                               'minutes': 8}]}
        if mode == 'walking':
            return {'duration_min': 21, 'distance_m': 1600,
                    'steps': [{'instruction': '步行 1.6 公里', 'distance_m': 1600,
                               'minutes': 21}]}
        return {'duration_min': 30, 'distance_m': 9000,
                'steps': [{'instruction': '上中山路', 'distance_m': 1200, 'minutes': 4},
                          {'instruction': '沿中山东路直行', 'distance_m': 5000,
                           'minutes': 18}]}

    def transit_plans(self, origin, dest, limit=3):
        return [{
            'duration_min': 46, 'walking_m': 700, 'cost': '4.0',
            'transfers': 1, 'night': False,
            'lines': [{'name': '地铁2号线(经天路--鱼嘴)', 'from_stop': '羊山公园',
                       'to_stop': '金马路', 'stops': 3, 'minutes': 12,
                       'start_time': '0600', 'end_time': '2300',
                       'from_loc': '118.9399,32.1038', 'to_loc': '118.9059,32.0717'},
                      {'name': '地铁4号线(仙林湖--龙江)', 'from_stop': '金马路',
                       'to_stop': '云南路', 'stops': 8, 'minutes': 28,
                       'start_time': '0600', 'end_time': '2300',
                       'from_loc': '118.9063,32.0717', 'to_loc': '118.7747,32.0591'}],
            'legs': [
                {'kind': 'walk', 'distance_m': 300, 'minutes': 4,
                 'from_loc': '118.9400,32.1136', 'to_loc': '118.9399,32.1038'},
                {'kind': 'bus', 'name': '地铁2号线(经天路--鱼嘴)',
                 'from_stop': '羊山公园', 'to_stop': '金马路', 'stops': 3,
                 'minutes': 12, 'distance_m': 6000,
                 'from_loc': '118.9399,32.1038', 'to_loc': '118.9059,32.0717',
                 'start_time': '0600', 'end_time': '2300'},
                {'kind': 'bus', 'name': '地铁4号线(仙林湖--龙江)',
                 'from_stop': '金马路', 'to_stop': '云南路', 'stops': 8,
                 'minutes': 28, 'distance_m': 12000,
                 'from_loc': '118.9063,32.0717', 'to_loc': '118.7747,32.0591',
                 'start_time': '0600', 'end_time': '2300'},
                {'kind': 'walk', 'distance_m': 400, 'minutes': 6,
                 'from_loc': '118.7747,32.0591', 'to_loc': '118.7789,32.0586'},
            ],
        }]


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = False
    c.settings.llm_api_key = 'k'
    c.settings.map_api_key = 'k'
    c.apply_settings(c.settings)
    c.degrade._map = FakeMap()
    # 两项：起点（南邮仙林）→ 终点（鼓楼医院）
    a = _poi('南京邮电大学仙林校区', 118.9402, 32.1136, 1)
    b = _poi('南京鼓楼医院', 118.7789, 32.0586, 2)
    c.trip_repo.ensure_poi(a)
    c.trip_repo.ensure_poi(b)
    c.trip_svc.commit_task(D, Task(title='出发', duration_min=30, poi=a,
                                   is_locked=True, start_ts=time(8, 0),
                                   status=TaskStatus.SCHEDULED), note='a')
    out = c.trip_svc.commit_task(D, Task(title='就诊', duration_min=60, poi=b,
                                         is_locked=True, start_ts=time(10, 0)),
                                 note='b')
    c.target_id = [i.task.task_id for i in out.result.ordered()
                   if i.task.title == '就诊'][0]
    yield c
    c.close()


# ---------------------------------------------------------------- TC-23-01
def test_leg_detail_transit_builds_bike_metro_walk_chain(ctx):
    """TC-23-01 公交方式：拼出「骑行接驳 → 地铁段 → 步行接驳」的完整链路"""
    detail, note = ctx.trip_svc.leg_detail_for(ctx.target_id, 'transit')
    assert detail.get('ok'), note
    segs = detail['segments']
    kinds = [s['kind'] for s in segs]
    assert kinds[0] == 'connector'          # 起点接驳
    assert kinds[-1] == 'connector'         # 末端接驳
    assert 'bus' in kinds
    # 接驳取更快者：骑行 8 分钟 < 步行 21 分钟
    assert segs[0]['mode_cn'] == '骑行'
    assert segs[0]['minutes'] == 8
    assert '地铁2号线' in segs[1]['mode_cn']
    assert segs[1]['operating'].startswith('0600')   # 运营时间已带出
    assert detail['total_min'] == sum(s['minutes'] for s in segs)


# ---------------------------------------------------------------- TC-23-02
def test_leg_detail_driving_lists_steps(ctx):
    """TC-23-02 驾车方式：给出分步导航指令"""
    detail, note = ctx.trip_svc.leg_detail_for(ctx.target_id, 'driving')
    assert detail.get('ok'), note
    texts = [s['text'] for s in detail['segments']]
    assert any('中山路' in t for t in texts)
    assert detail['mode_cn'] == '驾车'
    assert '导航步骤' in detail['note']


# ---------------------------------------------------------------- TC-23-03
def test_leg_detail_bicycling_mode(ctx):
    """TC-23-03 骑行方式：走 v4 骑行路径并返回指令"""
    detail, note = ctx.trip_svc.leg_detail_for(ctx.target_id, 'bicycling')
    assert detail.get('ok'), note
    assert detail['mode_cn'] == '骑行'
    assert detail['segments'][0]['minutes'] == 8
    assert '骑行' in detail['segments'][0]['text']


# ---------------------------------------------------------------- TC-23-04
def test_leg_detail_offline_message(tmp_path):
    """TC-23-04 离线模式：明确说明需联网，不编造路径"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    a = _poi('A', 118.9, 32.1, 1)
    b = _poi('B', 118.8, 32.05, 2)
    c.trip_repo.ensure_poi(a)
    c.trip_repo.ensure_poi(b)
    c.trip_svc.commit_task(D, Task(title='A', duration_min=30, poi=a,
                                   is_locked=True, start_ts=time(9, 0)), note='a')
    out = c.trip_svc.commit_task(D, Task(title='B', duration_min=30, poi=b,
                                         is_locked=True, start_ts=time(11, 0)),
                                 note='b')
    tid = [i.task.task_id for i in out.result.ordered()
           if i.task.title == 'B'][0]
    detail, note = c.trip_svc.leg_detail_for(tid)
    assert detail == {}
    assert '离线' in note
    c.close()


# ---------------------------------------------------------------- TC-23-05
def test_bicycling_is_a_first_class_mode(ctx):
    """TC-23-05 骑行是独立交通方式：能参与多方式对比与逐段设置"""
    from nanjingtrip.models import TransitMode
    assert TransitMode('bicycling').cn == '骑行'
    est = ctx.transit_svc.estimate_all(
        ctx.trip_repo.get_poi(1), ctx.trip_repo.get_poi(2))
    assert 'bicycling' in est


# ---------------------------------------------------------------- TC-23-06
def test_connector_modes_are_user_chosen_not_auto_fastest(ctx):
    """TC-23-06 接驳方式按用户选择，不因为骑行更快就自动替换步行"""
    detail, note = ctx.trip_svc.leg_detail_for(
        ctx.target_id, 'transit', access='walking', egress='walking')
    assert detail.get('ok'), note
    conns = [s for s in detail['segments'] if s['kind'] == 'connector']
    assert len(conns) == 2
    assert all(c['mode_cn'] == '步行' for c in conns), \
        '指定步行时不得替换成骑行（即使骑行更快）'
    assert conns[0]['minutes'] == 21          # FakeMap 的步行时长
    # 换成骑行后应变成骑行时长
    d2, _ = ctx.trip_svc.leg_detail_for(
        ctx.target_id, 'transit', access='bicycling', egress='bicycling')
    conns2 = [s for s in d2['segments'] if s['kind'] == 'connector']
    assert all(c['mode_cn'] == '骑行' for c in conns2)
    assert conns2[0]['minutes'] == 8


# ---------------------------------------------------------------- TC-23-07
def test_connector_can_be_disabled(ctx):
    """TC-23-07 可以关闭接驳段（例如自己走过去，不显示接驳）"""
    detail, note = ctx.trip_svc.leg_detail_for(
        ctx.target_id, 'transit', access='', egress='')
    assert detail.get('ok'), note
    assert not [s for s in detail['segments'] if s['kind'] == 'connector']
    assert any(s['kind'] == 'bus' for s in detail['segments'])


# ---------------------------------------------------------------- TC-23-08
def test_default_is_bike_access_walk_egress(ctx):
    """TC-23-08 默认接驳为"起点骑行、终点步行"（常见共享单车接驳）"""
    detail, _ = ctx.trip_svc.leg_detail_for(ctx.target_id, 'transit')
    conns = [s for s in detail['segments'] if s['kind'] == 'connector']
    assert conns and conns[0]['mode_cn'] == '骑行'
    assert conns[-1]['mode_cn'] == '步行'
    assert detail.get('access') == 'bicycling'
    assert detail.get('egress') == 'walking'
