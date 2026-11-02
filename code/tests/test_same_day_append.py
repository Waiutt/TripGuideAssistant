# -*- coding: utf-8 -*-
"""同日追加语义（对应报告 4.10 补充）：已有规划不会被覆盖，也不会重复。"""
import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore
from nanjingtrip.web_api import command_payload

DAY = '2026-11-05'


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _seed(ctx):
    from datetime import date
    ok, _msg = ctx.trip_svc.add_batch(date.fromisoformat(DAY), [
        {'poi': '南京博物院', 'title': '游览南京博物院', 'duration_min': 120},
        {'poi': '夫子庙', 'title': '逛夫子庙', 'duration_min': 90},
    ], origin='金陵饭店', keep_order=True)
    assert ok


def test_reset_does_not_touch_existing_plans(ctx):
    """「重新开始」只清会话偏好，绝不能动已经排好的行程与住宿锚点"""
    from datetime import date
    _seed(ctx)
    command_payload(ctx, {'action': 'consult_reset', 'session': 'ui'})
    trip = ctx.trip_svc.day_view(date.fromisoformat(DAY))
    assert len(trip.tasks) == 2
    assert trip.lodging is not None and '金陵' in trip.lodging.name


def test_new_round_appends_instead_of_overwriting(ctx):
    """同一天再聊一轮：旧安排保留、新地点追加、同名地点不重复"""
    from datetime import date
    _seed(ctx)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号在南京玩，想去总统府和夫子庙，'
                          '中午在德基广场吃饭', date(2026, 11, 1))['plan']
    assert plan['existing_days'] == [DAY], '草案要标出这天已有安排'
    assert plan['days'][0]['existing'] == 2
    ok, msg = ctx.trip_svc.apply_proposal(plan)
    assert ok
    assert '保留' in msg and '追加' in msg, msg
    trip = ctx.trip_svc.day_view(date.fromisoformat(DAY))
    names = [t.poi.name for t in trip.tasks if t.poi]
    assert len(names) > 2, '新地点应被追加进来'
    assert names.count('夫子庙') == 1, '同名地点不得重复'
    assert trip.lodging is not None, '住宿锚点不能被清掉'


def test_duplicate_only_write_reports_skipped(ctx):
    """全是重复地点的重复提交：按"已排过"处理，不产生第二份"""
    from datetime import date
    _seed(ctx)
    ok, msg = ctx.trip_svc.add_batch(date.fromisoformat(DAY), [
        {'poi': '夫子庙', 'title': '逛夫子庙', 'duration_min': 90},
    ], keep_order=True)
    assert ok and '已经排过' in msg
    trip = ctx.trip_svc.day_view(date.fromisoformat(DAY))
    assert len(trip.tasks) == 2

def test_consult_sees_existing_items_with_times(ctx):
    """顾问要"看得见"当天已排的安排（名称 + 时刻），而不是只知道有几项"""
    from datetime import date
    _seed(ctx)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号在南京玩，想去总统府',
                    date(2026, 11, 1))['plan']
    day = plan['days'][0]
    names = [it['poi'] for it in day['existing_items']]
    assert '南京博物院' in names and '夫子庙' in names
    assert all(it['start'] for it in day['existing_items']), '已有安排要带时刻'
    assert any('已有' in n for n in plan['notes']), '说明里要交代当天已有安排'


def test_soft_no_keeps_data_hard_no_removes_it(ctx):
    """『别去X』只影响新草案；『把X去掉』才真的删掉已有安排"""
    from datetime import date
    _seed(ctx)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号玩，别去夫子庙', date(2026, 11, 1))
    assert len(ctx.trip_svc.day_view(date.fromisoformat(DAY)).tasks) == 2, '软排除不应动数据'
    out = con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号这天把夫子庙去掉', date(2026, 11, 1))
    trips = ctx.trip_svc.day_view(date.fromisoformat(DAY))
    assert [t.poi.name for t in trips.tasks if t.poi] == ['南京博物院']
    assert '删掉' in out['message'], '要明说删掉了什么'


def test_existing_places_are_not_re_recommended(ctx):
    """已在行程里的地点，不会在草案里又被推荐一遍"""
    from datetime import date
    _seed(ctx)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号玩，加点别的', date(2026, 11, 1))['plan']
    stops = [str(i.get('poi') or '') for i in plan['days'][0]['items']]
    assert '南京博物院' not in stops and '夫子庙' not in stops


def test_existing_items_count_against_daily_quota(ctx):
    """已有 2 项时，草案新增不该再塞满一整组（配额要扣掉已有项）"""
    from datetime import date
    _seed(ctx)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('ui', f'{DAY[5:7]}月{int(DAY[8:])}号玩，再加个总统府', date(2026, 11, 1))['plan']
    sights = [i for i in plan['days'][0]['items']
              if i.get('poi') and str(i.get('category')) != '餐饮']
    assert len(sights) <= 2, f'已有 2 项时新增景点应很少，实际 {len(sights)}'
    assert any('总统府' in str(i.get('poi') or '') for i in plan['days'][0]['items'])
