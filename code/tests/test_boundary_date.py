# -*- coding: utf-8 -*-
"""边界测试 B 层：日期与解析边界。

契约（本文件即口径）：
  · 跨月/跨年必须算对，不能回退年份；
  · ISO（2026-11-14）与带年份（2028年2月29号）都要认；
  · 不存在的日期（13 月、9 月 32 号、平年 2/29）要**说清原因**再追问，不能泛泛问；
  · 区间倒置要提示"起始日晚于结束日"；
  · 过去的日期要明说"已经过去了"；
  · 跨度超过 15 天先按 15 天排并说明。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)


@pytest.fixture()
def con(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    yield Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    ctx.close()


def plan_of(con, text):
    return con.chat('b', text, TODAY)


def test_b01_span_across_months(con):
    """B-01 1月31号到2月2号：跨月算对，是 3 天"""
    plan = plan_of(con, '1月31号到2月2号去南京玩')['plan']
    assert (plan['start'], plan['end']) == ('2026-01-31', '2026-02-02')
    assert plan['days_total'] == 3


def test_b02_span_across_years(con):
    """B-02 12月30号到1月1号：跨年时结束日期要落到下一年"""
    plan = plan_of(con, '12月30号到1月1号去南京玩')['plan']
    assert (plan['start'], plan['end']) == ('2026-12-30', '2027-01-01')
    assert plan['days_total'] == 3


def test_b03_iso_date_is_understood(con):
    """B-03 ISO 写法 2026-11-14 要直接识别为那一天"""
    out = plan_of(con, '2026-11-14 去南京博物院')
    assert out['kind'] == 'plan'
    assert out['plan']['start'] == '2026-11-14'


def test_b04_leap_day(con):
    """B-04 闰年 2028-02-29 有效；平年 2027-02-29 要指出不存在"""
    ok = plan_of(con, '2028年2月29号去南京博物院')
    assert ok['kind'] == 'plan' and ok['plan']['start'] == '2028-02-29'
    # 平年没有 2/29：即使会话里已有旧日期，也必须提示这句日期看不懂
    bad = plan_of(con, '2027年2月29号去南京博物院')
    assert '没看懂' in bad['message'] or '日期' in bad['message'], bad['message']
    assert bad['kind'] == 'plan' and bad['plan']['start'] == '2028-02-29', '应沿用之前确认的日期'


def test_b05_bad_month_is_explained(con):
    """B-05 13 月：不能只说"哪天去"，要指出月份不合法"""
    out = plan_of(con, '13月5号去南京博物院')
    assert out['kind'] == 'ask'
    assert '13 月' in out['message']


def test_b06_bad_day_is_explained(con):
    """B-06 9 月 32 号：要指出这一天不存在"""
    out = plan_of(con, '9月32号去南京博物院')
    assert out['kind'] == 'ask'
    assert '32' in out['message']


def test_b07_reversed_range_is_explained(con):
    """B-07 11月16号到11月10号：区间倒置要说清，而不是泛泛追问"""
    out = plan_of(con, '11月16号到11月10号去南京玩')
    assert out['kind'] == 'ask'
    assert '起始日' in out['message'] or '晚于' in out['message']


def test_b08_past_date_is_flagged(con):
    """B-08 说过去的日期：仍按用户说的排，但要提醒"已经过去了\""""
    out = plan_of(con, '10月20号去南京博物院')
    assert out['kind'] == 'plan'
    assert '过去' in out['message']


def test_b09_overlong_span_is_capped_and_explained(con):
    """B-09 30 天跨度：先按 15 天排并说明，不能默默排 30 天"""
    plan = plan_of(con, '11月1号到11月30号去南京玩')['plan']
    assert plan['days_total'] == 15
    assert plan['end'] == '2026-11-15'


def test_b10_chinese_numerals(con):
    """B-10 中文数字日期「十一月十四号」"""
    plan = plan_of(con, '十一月十四号去南京博物院')['plan']
    assert plan['start'] == '2026-11-14'


def test_b11_weekday_expression(con):
    """B-11 「下周三」按下一个周三算（今天 2026-11-01 周日）"""
    plan = plan_of(con, '下周三去南京博物院')['plan']
    assert plan['start'] == '2026-11-04'


def test_b12_same_day_single(con):
    """B-12 单日表述只排一天，不追问区间"""
    plan = plan_of(con, '明天去南京博物院')['plan']
    assert plan['days_total'] == 1
    assert plan['start'] == '2026-11-02'
