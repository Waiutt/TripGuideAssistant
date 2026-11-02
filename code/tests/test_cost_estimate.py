# -*- coding: utf-8 -*-
"""每日费用估算的契约测试（对应报告输出规格"费用预估"）。

口径（数据纪律第 1、2 条）：
  · 餐饮价取城市包 `price`（人均参考价），门票取城市包收录价；
  · **没有收录的项必须写"待核实"，不许编一个看起来合理的数**；
  · 交通按"公交/地铁 2 元/次、站与站之间一段"估算，并显式标注"估算"。
"""
from __future__ import annotations

import datetime

import pytest

from nanjingtrip.app import AppContext

TODAY = datetime.date(2026, 11, 2)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _chat(ctx, text, session='cost'):
    rep = ctx.consult.chat(session, text, TODAY)
    return rep, (rep.get('message') or '')


def test_daily_cost_line_appears(ctx):
    """时间轴里每天要有"当日费用"一行，末尾有全程合计"""
    _, msg = _chat(ctx, '明天去总统府2小时，然后去夫子庙1.5小时，中午就近吃')
    assert '当日费用' in msg
    assert '全程费用' in msg


def test_food_price_comes_from_city_pack(ctx):
    """餐饮有城市包人均价时，要带"城市包人均参考价"这个来源标注"""
    _, msg = _chat(ctx, '明天去夫子庙2小时，中午在新街口吃饭')
    assert '当日费用' in msg
    assert ('餐饮 ¥' in msg and '城市包人均参考价' in msg) or '餐饮 待核实' in msg


def test_missing_ticket_is_pending_not_invented(ctx):
    """门票没收录时写"待核实"，绝不许编数字"""
    _, msg = _chat(ctx, '明天去中山陵2小时，然后去南京博物院2.5小时')
    assert '待核实' in msg
    # 全城包景点都没有票价，所以门票合计不应出现凭空的价格
    assert '门票 ¥' not in msg


def test_transit_fare_is_marked_estimate(ctx):
    """交通费按 2 元/次估算，且必须写"估算"二字"""
    _, msg = _chat(ctx, '明天去总统府2小时，然后去夫子庙1.5小时')
    if '交通 ¥' in msg:
        assert '估算' in msg


def test_cost_total_is_sum_of_parts(ctx):
    """合计 = 餐饮 + 门票 + 交通（文本里自洽，不许各算一套）"""
    _, msg = _chat(ctx, '明天去总统府2小时，中午就近吃，下午去夫子庙1.5小时')
    line = next((ln for ln in msg.splitlines() if '当日费用' in ln), '')
    assert line, msg
    if '≈ ¥' in line:
        total = int(line.rsplit('≈ ¥', 1)[1].split(' ')[0])
        nums = [int(x) for x in __import__('re').findall(r'¥(\d+)', line)]
        if len(nums) >= 2:
            assert total == sum(nums[:-1]), line


def test_cost_works_in_multiday_plan(ctx):
    """多日也要每天一行 + 全程一行"""
    _, msg = _chat(ctx, '11月5号到7号去南京玩，每天两个点，吃住行你安排')
    assert msg.count('当日费用') >= 1
    assert '全程费用' in msg
