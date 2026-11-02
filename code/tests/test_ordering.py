# -*- coding: utf-8 -*-
"""一天顺序编排（`ordering.order_day`）的契约测试。

这一层只负责顺序：**成员一个不增一个不减**。所以既有两条硬断言：
  · 打乱输入顺序后，输出的集合与输入完全相同；
  · 闭馆早的排前面、夜游类排最后、同类内部就近串联。
"""
from __future__ import annotations

from nanjingtrip.ordering import (EVENING_WORDS, chain_nearest, closing_of,
                                  is_evening, order_day)


class P:
    """最小 POI 替身（这一层只读 name / open_to）"""

    def __init__(self, name, open_to='', xy=(0, 0)):
        self.name = name
        self.open_to = open_to
        self.xy = xy

    def __repr__(self):
        return f'P({self.name})'


def _dist(a, b):
    return ((a.xy[0] - b.xy[0]) ** 2 + (a.xy[1] - b.xy[1]) ** 2) ** 0.5


# ---------------------------------------------------------------- 基本不变式
def test_members_are_preserved():
    group = [P('总统府', '17:00'), P('1912街区'), P('颐和路民国公馆区'), P('夫子庙')]
    got = order_day(group, distance=_dist)
    assert sorted(p.name for p in got) == sorted(p.name for p in group)


def test_empty_and_single():
    assert order_day([]) == []
    one = [P('总统府', '17:00')]
    assert order_day(one) == one


def test_no_distance_callback_keeps_relative_order():
    group = [P('A'), P('B'), P('C')]
    assert [p.name for p in order_day(group)] == ['A', 'B', 'C']


# ---------------------------------------------------------------- 规则一：闭馆早的先排
def test_earlier_closing_goes_first():
    group = [P('全天开放的点'), P('总统府', '17:00'), P('南京博物院', '17:00')]
    got = [p.name for p in order_day(group)]
    assert got[-1] == '全天开放的点' or got[:2] == ['总统府', '南京博物院']


def test_unknown_hours_sort_last_among_day_part():
    """没有营业时间的点排在"有闭馆时刻"的点之后（关得最晚）"""
    got = [p.name for p in order_day([P('未知'), P('总统府', '17:00')])]
    assert got == ['总统府', '未知'], got


def test_closing_of_normalizes_fake_hours():
    assert closing_of(P('x', '')) == '99:99'
    assert closing_of(P('x', '23:59')) == '99:99'
    assert closing_of(P('x', '17:00')) == '17:00'


# ---------------------------------------------------------------- 规则二：夜游排最后
def test_evening_spots_go_last():
    got = [p.name for p in order_day([P('夫子庙'), P('总统府', '17:00')])]
    assert got[-1] == '夫子庙', got


def test_is_evening_matches_words():
    assert is_evening(P('1912街区'))
    assert is_evening(P('老门东'))
    assert not is_evening(P('总统府'))
    assert '秦淮' in EVENING_WORDS


# ---------------------------------------------------------------- 规则三：就近串联
def test_chain_nearest_picks_nearby_next():
    a, b, c = P('A', xy=(0, 0)), P('B', xy=(0, 1)), P('C', xy=(9, 9))
    assert [p.name for p in chain_nearest([a, c, b], _dist)] == ['A', 'B', 'C']


def test_chain_keeps_all_members():
    group = [P('A', xy=(0, 0)), P('B', xy=(1, 0)), P('C', xy=(2, 0))]
    assert len(chain_nearest(group, _dist)) == 3
