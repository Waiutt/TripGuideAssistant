# -*- coding: utf-8 -*-
"""自然语言规则解析的等价类测试（对应报告第 4 章 TC-02-*）。"""
from datetime import date

import pytest

from nanjingtrip.adapters.mock import NANJING_POIS, POI_ALIASES
from nanjingtrip.parsing_rules import (extract_item, match_poi, parse_date,
                                       parse_duration, parse_mode, parse_time,
                                       rule_based_parse)

TODAY = date(2026, 10, 27)      # 周二
POI_NAMES = [p['name'] for p in NANJING_POIS]


# ---------------------------------------------------------------- TC-02-01
@pytest.mark.parametrize('text,expected', [
    ('今天去博物院', date(2026, 10, 27)),
    ('明天上午去博物院', date(2026, 10, 28)),
    ('后天去博物院', date(2026, 10, 29)),
    ('大后天去博物院', date(2026, 10, 30)),
])
def test_relative_date(text, expected):
    """TC-02-01 相对日期有效类"""
    assert parse_date(text, TODAY) == expected


# ---------------------------------------------------------------- TC-02-02
@pytest.mark.parametrize('text,expected', [
    ('周五去看展', date(2026, 10, 30)),
    ('下周一开会', date(2026, 11, 2)),
    ('星期天去逛街', date(2026, 11, 1)),
])
def test_weekday_date(text, expected):
    """TC-02-02 星期有效类"""
    assert parse_date(text, TODAY) == expected


# ---------------------------------------------------------------- TC-02-03
def test_explicit_month_day():
    """TC-02-03 月日有效类（已过去的日期顺延到明年）"""
    assert parse_date('11月3日答辩', TODAY) == date(2026, 11, 3)
    assert parse_date('1月5日返校', TODAY) == date(2027, 1, 5)


# ---------------------------------------------------------------- TC-02-04
@pytest.mark.parametrize('text,expected', [
    ('上午九点出发', (9, 0)),
    ('九点半出发', (9, 30)),
    ('下午三点开会', (15, 0)),
    ('晚上7点吃饭', (19, 0)),
    ('9:30 到南站', (9, 30)),
    ('中午吃饭', (12, 0)),
])
def test_time_parsing(text, expected):
    """TC-02-04 时刻有效类（含中文数字、半天制换算）"""
    assert parse_time(text) == expected


# ---------------------------------------------------------------- TC-02-05
@pytest.mark.parametrize('text,expected', [
    ('大概2小时', 120),
    ('大约1小时', 60),
    ('半小时', 30),
    ('1个半小时', 90),
    ('40分钟', 40),
    ('1.5小时', 90),
    ('2h', 120),
    ('半天', 240),
])
def test_duration_parsing(text, expected):
    """TC-02-05 耗时有效类"""
    assert parse_duration(text) == expected


# ---------------------------------------------------------------- TC-02-06
@pytest.mark.parametrize('text', [
    '明天去南京博物院看展',
    '去看展',
])
def test_missing_duration(text):
    """TC-02-06 无耗时（无效类）→ 返回 None 并在 missing 中标注"""
    assert parse_duration(text) is None
    r = rule_based_parse(text, TODAY, POI_NAMES, POI_ALIASES)
    assert '耗时' in r['missing']


# ---------------------------------------------------------------- TC-02-07
def test_missing_poi():
    """TC-02-07 无地点（无效类）→ missing 含地点"""
    r = rule_based_parse('明天上午看展，2小时', TODAY, POI_NAMES, POI_ALIASES)
    assert '地点' in r['missing']


# ---------------------------------------------------------------- TC-02-08
@pytest.mark.parametrize('text,expected', [
    ('坐地铁去新街口', 'transit'),
    ('步行去鼓楼', 'walking'),
    ('打车去南站', 'driving'),
])
def test_mode_parsing(text, expected):
    """TC-02-08 交通方式有效类"""
    assert parse_mode(text) == expected


# ---------------------------------------------------------------- TC-02-09
@pytest.mark.parametrize('text,expected', [
    ('去南京博物院看展', '南京博物院'),
    ('去南博看展', '南京博物院'),
    ('下午去鼓楼医院取报告', '鼓楼医院'),
    ('去新街口吃饭', '新街口'),
])
def test_poi_match(text, expected):
    """TC-02-09 地点匹配（含简称别名）"""
    assert match_poi(text, POI_NAMES, POI_ALIASES) == expected


# ---------------------------------------------------------------- TC-02-10
def test_full_parse_and_item_cleanup():
    """TC-02-10 整句解析：事项名不含时间/地点/耗时残留"""
    r = rule_based_parse('明天下午三点去南京博物院看展，大概2小时', TODAY,
                         POI_NAMES, POI_ALIASES)
    assert r['date'] == '2026-10-28'
    assert r['start_hint'] == '15:00'
    assert r['mention'] == '南京博物院'
    assert r['item'] == '看展'
    assert r['duration_min'] == 120
    assert r['category'] == '展览'
    assert r['missing'] == []
    for token in ('明天', '下午', '三点', '2小时', '南京博物院'):
        assert token not in r['item']


# ---------------------------------------------------------------- TC-02-11
def test_category_guessing():
    """TC-02-11 类别推断有效类"""
    assert rule_based_parse('去鼓楼医院取报告，半小时', TODAY, POI_NAMES,
                            POI_ALIASES)['category'] == '医疗'
    assert rule_based_parse('去新街口吃饭，1小时', TODAY, POI_NAMES,
                            POI_ALIASES)['category'] == '餐饮'
    assert rule_based_parse('去银行办事，1小时', TODAY, POI_NAMES,
                            POI_ALIASES)['category'] == '办事'
