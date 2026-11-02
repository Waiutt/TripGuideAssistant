"""三处"能排但不够聪明"的优化（D-64/D-65/D-66 回归）

D-64 闭馆日改期只 +1 天    ：「挪到明天」如果明天也闭馆，等于白挪。
D-65 装不下只报 no_slot   ：12 个地名一天排不下时，只告诉你"排不进"，
                            不告诉你"按优先级该舍哪个"。
D-66 条件分支两套都排进去 ：「下雨就改去博物院」被当成第 5、第 6 个景点，
                            结果雨天/晴天两套方案全塞在同一天。
"""
from datetime import date, time

import pytest

from nanjingtrip import engine as engine_mod
from nanjingtrip.app import AppContext
from nanjingtrip.models import Task


# 2026-12-07 是周一：南京博物院、雨花台烈士陵园当天闭馆
MONDAY = date(2026, 12, 7)
DAY = date(2026, 12, 8)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _add(ctx, day, title, poi, minutes=60):
    svc = ctx.trip_svc
    ok, msg = svc.add_batch(day, [{'kind': 'task', 'title': title, 'poi': poi,
                                   'duration_min': minutes, 'category': '其他'}],
                            keep_order=True)
    assert ok, msg


# ---------------- D-64：闭馆日改期要跳到下一个开放日 ----------------
def test_move_next_day_skips_closed_day(ctx):
    _add(ctx, MONDAY, '看展', '南京博物院')          # 周一闭馆
    trip = ctx.trip_repo.get_day(MONDAY, ctx.city)
    task = trip.tasks[0]
    sug = type('S', (), {'patch': {'move_next_day': True, 'title': '看展'}})()
    out = ctx.trip_svc.apply_suggestion(sug, MONDAY)
    assert out.ok, out.error
    # 周二（12-08）不闭馆，应该落在 12-08，而不是"明天也闭馆"的 12-14
    assert ctx.trip_repo.get_day(MONDAY, ctx.city) is None or \
        not ctx.trip_repo.get_day(MONDAY, ctx.city).tasks, '原日期没清掉'
    nxt_trip = ctx.trip_repo.get_day(date(2026, 12, 8), ctx.city)
    assert nxt_trip is not None, '没落到下一个开放日（周二）'
    assert [t.title for t in nxt_trip.tasks] == ['看展']


# ---------------- D-65：装不下时按优先级取舍 ----------------
def test_fit_day_suggests_drops_and_simulation_fits(ctx):
    places = ['中山陵', '玄武湖公园', '鸡鸣寺', '南京城墙台城景区', '总统府',
              '新街口', '夫子庙', '老门东', '雨花台烈士陵园', '大报恩寺遗址公园',
              '先锋书店五台山店', '南京博物院']
    ok, msg = ctx.trip_svc.add_batch(
        DAY, [{'kind': 'task', 'title': p, 'poi': p, 'duration_min': 60,
               'category': '其他'} for p in places], keep_order=True)
    assert ok, msg
    before = ctx.trip_svc.day_view(DAY)
    opts = ctx.trip_svc.options(lodging=before.lodging, plan_date=DAY)
    res = engine_mod.schedule(before.tasks, opts, ctx.trip_svc.transit.as_fn())
    assert res.unscheduled, '这一组本来就该排不下，测试前提不成立'

    plan = ctx.trip_svc.fit_day(DAY)
    assert plan['ok'] is True
    assert plan['drop'], '排不下却没有给出取舍建议'
    assert plan['kept'], '不许把所有项都丢掉'
    # 建议必须"真的排得下"：按建议删掉后再模拟一次
    keep_titles = set(plan['kept'])
    trial = [t for t in before.tasks if t.title in keep_titles]
    res2 = engine_mod.schedule(trial, opts, ctx.trip_svc.transit.as_fn())
    assert not res2.unscheduled, [t.title for t in res2.unscheduled]


def test_fit_day_keeps_marked_must_visits(ctx):
    "\"必须去\"的项在取舍里优先保留"
    ok, _ = ctx.trip_svc.add_batch(
        DAY, [{'kind': 'task', 'title': f'点{i}', 'poi': p, 'duration_min': 60,
               'category': '其他', 'raw_text': ('必须去' if p == '总统府' else '')}
              for i, p in enumerate(['中山陵', '玄武湖公园', '鸡鸣寺', '总统府',
                                     '新街口', '夫子庙', '老门东', '雨花台烈士陵园',
                                     '大报恩寺遗址公园', '先锋书店五台山店',
                                     '南京博物院', '南京城墙台城景区'])],
        keep_order=True)
    plan = ctx.trip_svc.fit_day(DAY)
    assert plan['ok']
    # 保留/舍弃按"地点"判断（标题是占位的"点0…点11"）
    by_title = {t.title: t for t in ctx.trip_svc.day_view(DAY).tasks}
    kept_pois = [(by_title[x].poi.name if by_title[x].poi else '') for x in plan['kept']]
    assert any('总统府' in p for p in kept_pois), (plan['kept'], kept_pois)
    assert plan['drop'], plan


# ---------------- D-66：条件分支算"备选"，不许当景点排 ----------------
def test_conditional_plan_is_separated(ctx):
    text = '先去玄武湖1小时，下午去总统府1小时。如果下雨就改去南京博物院和先锋书店。'
    out = ctx.trip_svc.preview_batch(text, DAY)
    items = out['items']
    places = ' '.join((i.get('poi') or '') for i in items)
    assert '玄武湖' in places and '总统府' in places
    # 雨天方案里的两个点不许混进当天行程
    assert '博物' not in places and '先锋' not in places, items
    cond = out.get('conditional') or []
    assert cond, out
    assert '雨' in cond[0]['trigger']
    joined = ' '.join(cond[0]['places'])
    assert '博物' in joined and '先锋' in joined
