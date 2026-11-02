"""多日行程提案（"9/10–9/16 在南京玩，比较懒，起得晚，两个人，酒店未知"）

这类请求是**行程顾问**，不是录入：用户只给区间和偏好，地点/酒店/每天怎么分都要系统拿主意。
以前整段喂给录入路径只得到 0 条（被当成交代表述丢掉），助手也只会回一句问答。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.geo import poi_distance_km
from nanjingtrip.proposal import (ItineraryPlanner, parse_date_range, parse_pace,
                                  parse_party)


TEXT = ('我要在9月10号-9月16号在南京进行旅游，帮我推荐规划具体的行程，'
        '我比较懒，起床起得晚，然后两个人，酒店未知，一切都没有头绪')
TODAY = date(2026, 9, 1)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True          # 提案只用城市包，离线即可
    c.apply_settings(c.settings)
    yield c
    c.close()


# ---------------- 偏好解析 ----------------
def test_parse_date_range():
    got = parse_date_range('我要在9月10号-9月16号在南京', TODAY)
    assert got == (date(2026, 9, 10), date(2026, 9, 16))
    assert parse_date_range('9月30日至10月3日', TODAY) == (date(2026, 9, 30),
                                                          date(2026, 10, 3))
    assert parse_date_range('随便玩玩', TODAY) is None


def test_parse_party_and_pace():
    assert parse_party('然后两个人') == 2
    assert parse_party('一家三口') == 3
    assert parse_party('我自己去') == 1
    assert parse_pace('我比较懒，起床起得晚') == ('slow', '10:00')
    assert parse_pace('我早起，早点出发')[0] == 'normal'
    assert parse_pace('特种兵式') == ('fast', '08:30')


# ---------------- 提案本体 ----------------
def test_proposal_covers_whole_range(ctx):
    plan = ItineraryPlanner(ctx.trip_svc).propose(TEXT, TODAY)
    assert plan['ok'], plan
    dates = [d['date'] for d in plan['days']]
    assert dates[0] == '2026-09-10' and dates[-1] == '2026-09-16'
    assert len(dates) == 7, dates
    # 每天都有安排，且每天不超过"懒散"节奏的景点数
    for d in plan['days']:
        assert d['items'], d
        assert d['sights'] <= 2, d
    # 偏好被听进去了
    assert plan['pace'] == 'slow' and plan['wake'] == '10:00' and plan['party'] == 2


def test_proposal_picks_real_landmarks(ctx):
    """推荐要挑口碑点位，而不是随便凑数"""
    plan = ItineraryPlanner(ctx.trip_svc).propose(TEXT, TODAY)
    names = ' '.join(stop for d in plan['days'] for stop in d['stops'])
    for kw in ('中山陵', '南京博物院', '总统府', '夫子庙'):
        assert kw in names, (kw, names)


def test_proposal_skips_closed_days(ctx):
    """周一闭馆的点不许排进周一（9-14 是周一）"""
    plan = ItineraryPlanner(ctx.trip_svc).propose(TEXT, TODAY)
    monday = next(d for d in plan['days'] if d['date'] == '2026-09-14')
    for it in monday['items']:
        poi = ctx.trip_svc._poi_named(it['poi'])
        if poi is None:
            continue
        assert '一' not in tuple(poi.closed_days or ()), (it['poi'], poi.closed_days)


def test_proposal_meal_is_nearby(ctx):
    """吃饭点要挨着当天景点，而不是随机抓一家"""
    plan = ItineraryPlanner(ctx.trip_svc).propose(TEXT, TODAY)
    planner = ItineraryPlanner(ctx.trip_svc)
    for d in plan['days']:
        sights = [ctx.trip_svc._poi_named(i['poi']) for i in d['items']
                  if i['category'] != '餐饮']
        meal = next((ctx.trip_svc._poi_named(i['poi']) for i in d['items']
                     if i['category'] == '餐饮'), None)
        if not sights or meal is None:
            continue
        near = min(poi_distance_km(meal, s) for s in sights if s)
        assert near <= 8.0, (d['date'], meal.name, near)


def test_assistant_routes_to_consultant(ctx):
    """助手不该把"帮我推荐行程"当普通问答：现在走**多轮顾问**（信息够就先给草案）"""
    out = ctx.assistant_svc.route(TEXT, TODAY)
    assert out['kind'] == 'consult', out
    assert (out.get('plan') or {}).get('days'), out


def test_apply_proposal_writes_every_day(ctx):
    plan = ItineraryPlanner(ctx.trip_svc).propose(TEXT, TODAY)
    hotel = plan['days'][0]['items'][0]['poi']      # 随便给个锚点
    ok, msg = ctx.trip_svc.apply_proposal(plan, hotel=hotel)
    assert ok, msg
    for d in plan['days']:
        view = ctx.trip_svc.day_view(date.fromisoformat(d['date']))
        assert view.tasks, f'{d["date"]} 没写进去'
    # "起得晚"必须落成当天的可排起点，但**不许**改全局默认设置（D-86）
    assert ctx.settings.day_start == '08:00', '写入草案不该改写全局日界'
    first_day = date.fromisoformat(plan['days'][0]['date'])
    ts = [t.start_ts for t in ctx.trip_svc.day_view(first_day).tasks if t.start_ts]
    assert ts and min(ts).strftime('%H:%M') >= '10:00', '当天仍要从 10:00 之后起排'
