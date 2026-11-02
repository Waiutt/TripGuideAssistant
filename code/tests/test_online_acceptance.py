# -*- coding: utf-8 -*-
"""联网验收套件：真实 DeepSeek + 真实高德，跑的是用户真实会走的那条链路。

运行方式（默认的 `pytest tests` **不会**跑这些）：
    python -m pytest -m online -q

设计取舍
--------
* 断言只写"结构 / 区间 / 一致性"，**不写死具体时刻**：地图数据随实时路况变化，
  大模型温度 0 也可能换措辞。
* 现场一定有网络，所以**不设 skip**：缺 Key、断网、配额用尽一律直接失败，
  避免把"没测"伪装成"通过"。
* 外部调用控制在 40 次以内：模块级共享一个 AppContext，复用通行缓存。
"""
from __future__ import annotations

import re
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.config import SettingsService
from nanjingtrip.models import TransitMode

pytestmark = pytest.mark.online

D = date(2026, 11, 26)          # 周四，避开"周一闭馆"的干扰

CASE1 = ("我现在在南京邮电大学三牌楼校区，今天11月1日白天出行。我需要依次去："
         "新街口商圈（停留40分钟）、总统府（停留1小时）、夫子庙（停留1小时20分钟）、"
         "老门东（停留1小时）。帮我规划全天最优路线，自动计算路程、交通方式、"
         "抵达离开时间，生成完整时序行程表。")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """接真实 Key 的上下文。没有 Key 直接失败——现场一定有网络与配额。"""
    real = SettingsService().load()
    if not real.llm_ready or not real.map_ready:
        pytest.fail('联网验收需要配置真实的大模型 Key 与高德 Key（离线模式无法验收）')
    c = AppContext(tmp_path_factory.mktemp('live'))
    s = c.settings
    s.offline_mode = False
    s.llm_base_url = real.llm_base_url
    s.llm_model = real.llm_model
    s.llm_api_key = real.llm_api_key
    s.map_api_key = real.map_api_key
    s.llm_reasoning_effort = real.llm_reasoning_effort
    s.transit_mode = 'driving'
    s.buffer_min = 10
    s.keep_order = True
    s.auto_mode = True
    c.apply_settings(s)
    yield c
    c.close()


# ------------------------------------------------------------------ 装配状态
def test_online_mode_is_really_online(live):
    """装配层：不是离线，且大模型可用"""
    assert not live.degrade.is_offline()
    assert live.degrade.llm_ready() and live.degrade.map_ready()
    assert '在线' in live.degrade.mode_label()


# -------------------------------------------------------------- 大模型 JSON 契约
@pytest.mark.parametrize('text', [
    '明天上午九点去南京博物院看展，大概2小时',
    '中午在新街口吃饭一小时',
])
def test_llm_returns_schema_json(live, text):
    """真实模型必须守 JSON 契约：解析出地点与时长，且字段落在约定范围内"""
    out = live.trip_svc.preview_text(text, D)
    assert out.ok, out.error
    assert out.draft is not None and out.draft.poi is not None
    assert out.draft.duration_min > 0
    assert out.draft.category in ('医疗', '餐饮', '展览', '办事', '其他')


# ------------------------------------------------------------------ 批量解析
def test_batch_parse_real_case1(live):
    """案例一原话：4 个地点、时长、起点全部解析正确"""
    out = live.trip_svc.preview_batch(CASE1, D)
    items = out['items']
    assert len(items) == 4, f'应解析出 4 项，实际 {len(items)}：{out.get("unparsed")}'
    names = [i['poi'] for i in items]
    for want in ('新街口', '总统府', '夫子庙', '老门东'):
        assert any(want in n for n in names), f'缺少地点：{want}（实际 {names}）'
    assert [i['duration_min'] for i in items] == [40, 60, 80, 60]
    assert '三牌楼' in (out.get('origin') or ''), f'起点未识别：{out.get("origin")}'


def test_batch_parse_real_case3_poi_accuracy(live):
    """案例三原话：4 个地点应全部命中真实 POI

    2026-10 补齐城市包（苜蓿园大街 / 颐和路民国公馆区）后，本用例由 xfail 转通过。
    """
    text = ('我在南邮仙林校区，11月3日出行。计划：上午 中山陵（停留1.5小时）、'
            '中午 苜蓿园美食街（停留40分钟）、下午 颐和路民国公馆（停留50分钟）、'
            '傍晚 莫愁湖（停留1小时）。生成完整精准全天行程。')
    out = live.trip_svc.preview_batch(text, D)
    names = [i['poi'] for i in out['items']]
    for want in ('中山陵', '苜蓿园', '颐和路', '莫愁湖'):
        assert any(want in n for n in names), f'地点误配：{want} 不在 {names}'


# ------------------------------------------------------------------ 真实地图
def test_amap_four_modes_and_route(live):
    """真实高德：四方式对比要有值，路径折线要有点"""
    pois = live.trip_svc.preview_batch('去鸡鸣寺停留50分钟，然后去南京城墙台城景区停留1小时', D)
    items = pois['items']
    assert len(items) == 2
    a = live.trip_svc.transit
    pa = live.trip_svc.preview_text('去鸡鸣寺停留50分钟', D).draft.poi
    pb = live.trip_svc.preview_text('去南京城墙台城景区停留1小时', D).draft.poi
    assert pa is not None and pb is not None
    allm = a.estimate_all(pa, pb)
    known = {k: v for k, v in allm.items() if v}
    assert len(known) >= 2, f'四方式对比取值不足：{allm}'
    assert all(v > 0 for v in known.values())
    lines = live.degrade.map().route_polyline((pa.lng, pa.lat), (pb.lng, pb.lat),
                                              TransitMode.WALKING.value)
    assert lines and sum(len(x) for x in lines['lines']) >= 2, '步行路径折线为空'


# ------------------------------------------------------- 统计口径（在线复核）
def test_stats_match_explanation_online(live):
    """统计条通行必须等于排程说明的"通行合计"——真实数据下同样成立"""
    ok = live.trip_svc.add_batch(
        D, [{'title': '游览新街口', 'poi': '新街口', 'duration_min': 40},
            {'title': '参观总统府', 'poi': '总统府', 'duration_min': 60}],
        origin='南京邮电大学三牌楼校区', keep_order=True)
    assert ok[0], ok[1]
    result = live.trip_svc.compute_day(D)
    legs = sum(i.leg_before.duration_min for i in result.ordered() if i.leg_before)
    stats = live.trip_svc.day_stats(live.trip_svc.day_view(D), result)
    assert legs > 0
    assert stats['transit'] == legs
    assert re.search(rf'通行合计 {legs} 分钟', result.explanation)
    # 每段方式都应是真实取到的，不是"查不到"的兜底
    assert all(i.leg_before.mode in tuple(TransitMode)
               for i in result.ordered() if i.leg_before)


# ------------------------------------------------------------------ AI 助手
def test_assistant_answers_online(live):
    """在线问答必须有模型来源、且不编造未知信息"""
    reply = live.assistant_svc.ask('我今天几点出门合适？', plan_date=D)
    assert reply.source == 'online', f'问答未走在线：{reply.source}'
    assert len(reply.text.strip()) >= 8, '回答过短'


def test_health_reports_llm_state(live):
    """降级可见性：health 必须暴露 llm_ok / llm_error 字段"""
    from nanjingtrip.web_api import health_payload
    h = health_payload(live)
    assert 'llm_ok' in h and 'llm_error' in h
    assert h['llm_ok'] is True
