# -*- coding: utf-8 -*-
"""导出（精简版）的契约测试。

口径：
  · 导出是**给人看的一页纸**：只留时刻 / 地点 / 停留 / 当天费用，
    内部标记（【可选】、括号理由、数据来源长说明）一律不进；
  · 一次最多 7 天，**后端也必须校验**（前端按钮拦不住直接敲接口）；
  · 没有安排的日子不导出，不许拿"留白"冒充方案。
"""
from __future__ import annotations

import datetime

import pytest

from nanjingtrip import web_api
from nanjingtrip.app import AppContext
from nanjingtrip.export import MAX_EXPORT_DAYS, brief_markdown, clamp_range

D5, D6, D7 = (datetime.date(2026, 11, 5), datetime.date(2026, 11, 6),
              datetime.date(2026, 11, 7))


def _day(iso, weekday, *items):
    return {'date': iso, 'weekday': weekday, 'items': list(items),
            'cost': {'food': 0, 'ticket': 0, 'ticket_unknown': len(items),
                     'fare': 0, 'total': 0}}


def _item(start, end, title, minutes=60, category='展览', poi=''):
    return {'start': start, 'end': end, 'title': title, 'poi': poi,
            'duration_min': minutes, 'category': category}


# ---------------------------------------------------------------- 渲染
def test_brief_has_only_time_place_stay():
    """精简版表格只有三列：时间 / 安排 / 停留"""
    md = brief_markdown([_day('2026-11-05', '四',
                              _item('10:00', '12:20', '总统府', 140))])
    assert '| 时间 | 安排 | 停留 |' in md
    assert '| 10:00–12:20 | 总统府 | 140 分 |' in md


def test_internal_marks_are_stripped():
    """内部标记（【可选】、括号理由、游览前缀）不许出现在导出里"""
    md = brief_markdown([_day('2026-11-05', '四',
                              _item('10:00', '12:20', '游览夫子庙（景区）', 140))])
    assert '【可选】' not in md and '【固定】' not in md
    assert '游览' not in md
    assert '（景区）' not in md and '(景区)' not in md
    assert '| 10:00–12:20 | 夫子庙 | 140 分 |' in md


def test_empty_day_is_not_exported():
    """没有安排的日子不导出"""
    md = brief_markdown([_day('2026-11-05', '四'),
                         _day('2026-11-06', '五', _item('09:00', '11:00', '中山陵', 120))])
    assert '11月5日' not in md
    assert '11月6日' in md


def test_no_items_at_all_returns_empty():
    assert brief_markdown([]) == ''
    assert brief_markdown([_day('2026-11-05', '四')]) == ''


def test_summary_line_has_days_and_stops():
    md = brief_markdown([_day('2026-11-05', '四', _item('10:00', '11:00', 'A')),
                         _day('2026-11-06', '五', _item('10:00', '11:00', 'B'))])
    assert '合计：2 天 · 2 个点位' in md


def test_transit_items_do_not_show_stay():
    """出发/返回这类交通项不写"停留 20 分"，也不计入点位"""
    md = brief_markdown([_day('2026-11-05', '四',
                              _item('09:00', '09:20', '南京邮电大学', 20, '交通'))])
    assert '停留' in md and '| 09:00–09:20 | 南京邮电大学 |  |' in md
    assert '0 个点位' in md


# ---------------------------------------------------------------- 范围限制
@pytest.mark.parametrize('start,end,ok', [
    ('2026-11-05', '2026-11-05', True),
    ('2026-11-05', '2026-11-11', True),      # 正好 7 天
    ('2026-11-05', '2026-11-12', False),     # 8 天
    ('2026-11-06', '2026-11-05', False),     # 倒置
    ('bad', '2026-11-05', False),
])
def test_clamp_range(start, end, ok):
    assert clamp_range(start, end)[0] is ok


def test_max_export_days_is_seven():
    assert MAX_EXPORT_DAYS == 7


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    c.trip_svc.add_batch(D5, [
        {'kind': 'task', 'title': '总统府', 'poi': '总统府',
         'duration_min': 140, 'category': '展览'},
        {'kind': 'task', 'title': '夫子庙', 'poi': '夫子庙',
         'duration_min': 90, 'category': '展览'}])
    c.trip_svc.add_batch(D6, [
        {'kind': 'task', 'title': '南京博物院', 'poi': '南京博物院',
         'duration_min': 150, 'category': '展览'}])
    yield c
    c.close()


def test_export_action_returns_markdown(ctx):
    out = web_api.command_payload(ctx, {'action': 'export_plan',
                                        'start': '2026-11-05', 'end': '2026-11-06'})
    assert out['ok'] is True, out
    assert out['days'] == 2
    assert '总统府' in out['markdown'] and '南京博物院' in out['markdown']
    assert out['filename'].endswith('.md')


def test_export_action_rejects_more_than_seven_days(ctx):
    out = web_api.command_payload(ctx, {'action': 'export_plan',
                                        'start': '2026-11-05', 'end': '2026-11-20'})
    assert out['ok'] is False
    assert '最多导出 7 天' in out['message']


def test_export_action_reports_empty_range(ctx):
    out = web_api.command_payload(ctx, {'action': 'export_plan',
                                        'start': '2026-11-20', 'end': '2026-11-21'})
    assert out['ok'] is False
    assert '没什么可导出' in out['message']


def test_export_never_leaks_settings(ctx):
    """导出只导行程，不许把配置/Key 带出去"""
    out = web_api.command_payload(ctx, {'action': 'export_plan',
                                        'start': '2026-11-05', 'end': '2026-11-06'})
    md = out['markdown']
    for bad in ('api_key', 'secrets', 'transit_mode', 'buffer_min'):
        assert bad not in md
