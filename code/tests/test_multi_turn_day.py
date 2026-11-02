# -*- coding: utf-8 -*-
"""多轮对话 · 单天规划（对应报告 4.11 补充）。

真实用法很少一次说完：先报地点，再补起床时间，再说吃什么，接着改主意。
本文件只测**单天**场景下的多轮累积与最终落地，断言分三层：
  槽位是否累积 → 草案是否反映 → 写入后当天数据是否真的对。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)
DAY = date(2026, 11, 2)


@pytest.fixture()
def env(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    yield ctx, con
    ctx.close()


def plan_of(out):
    return out.get('plan') or {}


def stops(plan):
    return [str(i.get('poi') or i.get('title') or '')
            for d in plan.get('days') or [] for i in d.get('items') or []]


def test_multi01_locations_then_wake(env):
    """MR-01 先报地点，再说起床时间：两轮都要留在草案里"""
    ctx, con = env
    con.chat('m1', '明天去总统府和南京博物院', TODAY)
    out = con.chat('m1', '我十点起床', TODAY)
    assert out['slots']['wake'] == '10:00'
    assert plan_of(out).get('wake') == '10:00'
    got = stops(plan_of(out))
    assert any('总统府' in s for s in got) and any('博物院' in s for s in got)


def test_multi02_add_meal_after_plan(env):
    """MR-02 有了草案后说"中午在德基广场吃饭"：吃饭点要进草案"""
    ctx, con = env
    con.chat('m2', '明天去总统府', TODAY)
    out = con.chat('m2', '中午在德基广场吃饭', TODAY)
    got = stops(plan_of(out))
    assert any('德基' in s for s in got), f'吃饭点没进草案：{got}'


def test_multi03_avoid_then_drop(env):
    """MR-03 先说"别去夫子庙"（软），再说"把总统府去掉"（硬）：两轮都生效"""
    ctx, con = env
    con.chat('m3', '明天去总统府、夫子庙和南京博物院', TODAY)
    out1 = con.chat('m3', '别去夫子庙', TODAY)
    assert '夫子庙' in (out1['slots']['avoid'] or [])
    out2 = con.chat('m3', '把总统府去掉', TODAY)
    got = stops(plan_of(out2))
    assert not any('总统府' in s for s in got), f'硬删除后草案仍有总统府：{got}'


def test_multi04_change_per_day(env):
    """MR-04 先说地点，再改"每天两个点"：草案点数要跟着变"""
    ctx, con = env
    con.chat('m4', '明天在南京逛逛', TODAY)
    out = con.chat('m4', '每天别超过两个点', TODAY)
    assert out['slots']['per_day'] == 2
    assert plan_of(out).get('per_day') == 2


def test_multi05_hotel_then_meal(env):
    """MR-05 先说住宿让我选，再说午饭就近：两个槽位互不覆盖"""
    ctx, con = env
    con.chat('m5', '明天去总统府', TODAY)
    con.chat('m5', '住宿你帮我选', TODAY)
    out = con.chat('m5', '午饭就近随便', TODAY)
    assert out['slots']['hotel'] == 'recommend'
    assert out['slots']['meal'] == 'recommend'


def test_multi06_write_after_rounds(env):
    """MR-06 三轮之后确认写入：当天数据与草案一致"""
    ctx, con = env
    con.chat('m6', '明天去总统府和南京博物院', TODAY)
    con.chat('m6', '十点起床', TODAY)
    out = con.chat('m6', '中午在德基广场吃饭', TODAY)
    plan = plan_of(out)
    ok, msg = ctx.trip_svc.apply_proposal(plan)
    assert ok, msg
    tasks = ctx.trip_svc.day_view(DAY).tasks
    names = [t.poi.name for t in tasks if t.poi]
    assert any('总统府' in n for n in names) or any('博物馆' in n or '博物院' in n for n in names)
    starts = [t.start_ts for t in tasks if t.start_ts]
    assert starts and min(starts).strftime('%H:%M') >= '10:00'


def test_multi07_ask_between_rounds(env):
    """MR-07 两轮之间插一个问句：不能把已积累的槽位清掉"""
    ctx, con = env
    con.chat('m7', '明天去总统府', TODAY)
    con.chat('m7', '几点出发比较合适', TODAY)
    out = con.chat('m7', '十点起床', TODAY)
    assert out['slots']['start'] == '2026-11-02'
    assert out['slots']['wake'] == '10:00'


def test_multi08_reset_clears_slots(env):
    """MR-08 重新开始后，旧槽位不带进新一轮"""
    ctx, con = env
    con.chat('m8', '明天去总统府', TODAY)
    from nanjingtrip.web_api import command_payload
    command_payload(ctx, {'action': 'consult_reset', 'session': 'm8'})
    out = con.chat('m8', '夫子庙', TODAY)
    assert not out['slots'].get('start'), '重置后不该还带着旧日期'


def test_multi09_two_rounds_same_place_dedup(env):
    """MR-09 两轮都说同一个地点：草案里不重复"""
    ctx, con = env
    con.chat('m9', '明天去总统府', TODAY)
    out = con.chat('m9', '总统府', TODAY)
    # 只数"游览项"：午饭块标题里出现地点名（"在总统府附近吃午饭"）不算重复排点
    got = [s for s in stops(plan_of(out)) if '总统府' in s and '吃' not in s]
    assert len(got) <= 1, f'同一地点被排了两次：{got}'


def test_multi10_day_end_across_rounds(env):
    """MR-10 第三轮才说"晚上十一点回酒店"：日界要落到 23:00"""
    ctx, con = env
    con.chat('m10', '明天去总统府', TODAY)
    con.chat('m10', '十点起床', TODAY)
    out = con.chat('m10', '晚上十一点回酒店', TODAY)
    assert out['slots']['day_end'] == '23:00', out['slots'].get('day_end')
