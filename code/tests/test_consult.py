# -*- coding: utf-8 -*-
"""行程顾问多轮会话测试（对应报告第 4 章 TC-08-*）。

只测确定性规则：窗口型日期、周末约束、住宿追问与懒人节奏。
测试全程离线，不触网、不依赖模型输出。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

#: 固定"今天"，避免用例结果随真实日期漂移
TODAY = date(2026, 10, 31)

TEXT_WINDOW = ('我想11月份中旬找空挡在南京市区旅游，但是我很懒。一般10点起床，'
               '晚上可以23点后到酒店，你给我规划一下大家比较推荐的景点然后规划一下，'
               '但是呢，其中有一天我要去栖霞区南京邮电大学找朋友中午吃饭聚餐，'
               '你给我规划后我来约朋友，他工作日没空，你得给我推荐一下什么时候去')


@pytest.fixture()
def consultant(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    yield Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    ctx.close()


def _days(plan):
    return plan.get('days') or []


def test_month_window_is_flagged_with_bounds(consultant):
    """TC-08-01 「11月中旬」记成窗口，并保留原始窗口起止"""
    st = consultant.absorb(consultant.load('t'), TEXT_WINDOW, TODAY)
    assert st.window is True
    assert (st.win_start, st.win_end) == ('2026-11-11', '2026-11-20')


def test_window_is_not_filled_to_ten_days(consultant):
    """TC-08-02 窗口 10 天不等于玩 10 天：先给 3 天草案，且日期落在窗口内"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    assert plan['days_total'] <= 3
    assert '2026-11-11' <= plan['start'] and plan['end'] <= '2026-11-20'
    assert len(_days(plan)) == plan['days_total']


def test_explicit_range_keeps_its_length(consultant):
    """TC-08-03 用户给明确区间时按他的天数排，不做 3 天截断"""
    plan = consultant.chat('t', '11月11号到11月16号去南京玩，帮我规划', TODAY)['plan']
    assert plan['days_total'] == 6
    assert (plan['start'], plan['end']) == ('2026-11-11', '2026-11-16')


def test_friend_free_on_weekend_moves_that_day(consultant):
    """TC-08-04 朋友只在周末有空 → 去南邮那天排到周六/周日"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    friend_days = [d for d in _days(plan)
                   if any('南京邮电' in (i.get('poi') or '') for i in d['items'])]
    assert friend_days, '点名的南邮必须排进行程'
    assert friend_days[0]['weekday'] in ('六', '日')
    assert '周六' in plan['message'] or '周日' in plan['message']


def test_lazy_pace_and_late_return_are_recorded(consultant):
    """TC-08-05 「很懒，10 点起，23 点后回酒店」落成槽位"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    assert plan['pace'] == 'slow'
    assert plan['wake'] == '10:00'
    assert plan['day_end'] == '23:00'


def test_hotel_is_asked_not_assumed(consultant):
    """TC-08-06 只是提到"回酒店"不等于让我选：先追问住哪"""
    out = consultant.chat('t', TEXT_WINDOW, TODAY)
    assert out['slots']['hotel'] == ''
    asks = ' '.join(q.get('ask') or '' for q in out.get('questions') or [])
    assert '住哪' in asks


def test_hotel_picked_only_when_user_asks(consultant):
    """TC-08-07 用户说「你帮我选」才替他定住宿，并给出具体酒店"""
    consultant.chat('t', TEXT_WINDOW, TODAY)
    out = consultant.chat('t', '住宿你帮我选吧', TODAY)
    plan = out['plan']
    assert plan['hotel'] == 'recommend'
    assert plan['hotel_pick']
    assert not [q for q in out.get('questions') or [] if '住哪' in (q.get('ask') or '')]


def test_window_survives_follow_up_round(consultant):
    """TC-08-08 第二轮补充信息不改掉原始窗口起止"""
    consultant.chat('t', TEXT_WINDOW, TODAY)
    st = consultant.absorb(consultant.load('t'), '住宿你帮我选吧', TODAY)
    assert (st.win_start, st.win_end) == ('2026-11-11', '2026-11-20')


def test_late_return_to_hotel_is_not_an_arrival(consultant):
    """TC-08-09 「23 点后到酒店」是住宿偏好，不是"抵达南京"这类事务要素"""
    out = consultant.chat('t', TEXT_WINDOW, TODAY)
    assert out['slots']['practical'] is False
    assert not out['slots']['arrival']

def test_old_session_with_auto_hotel_is_reset(consultant, tmp_path):
    """TC-08-10 老会话里被模型擅自填的 hotel=recommend 要作废，并重新追问"""
    consultant.store.put('legacy', {'start': '2026-11-14', 'end': '2026-11-16',
                                   'hotel': 'recommend', 'asked': [], 'window': True,
                                   'win_start': '2026-11-11', 'win_end': '2026-11-20'})
    out = consultant.chat('legacy', '就这样吧', TODAY)
    assert 'hotel' in [q['key'] for q in out.get('questions') or []]
    assert not (out['plan'].get('hotel_pick') or '')


def test_hotel_question_carries_real_options(consultant):
    """TC-08-11 住宿追问必须带可点候选（不能只让用户自己打名字）"""
    out = consultant.chat('t', TEXT_WINDOW, TODAY)
    q = next(q for q in out['questions'] if q['key'] == 'hotel')
    assert len(q['options']) >= 4
    assert '你帮我选' in q['options']
    assert any('酒店' in o or '饭店' in o for o in q['options'])
    assert any(h['name'] in q['options'] for h in out['plan']['hotel_options'])


def test_picking_option_sets_hotel(consultant):
    """TC-08-12 点候选里的酒店名就等于定了住宿锚点"""
    out = consultant.chat('t', TEXT_WINDOW, TODAY)
    name = out['plan']['hotel_options'][0]['name']
    plan = consultant.chat('t', name, TODAY)['plan']
    assert plan['hotel_pick'] == name
    assert plan['hotel_options'] == []


def test_picked_hotel_is_announced_as_editable(consultant):
    """TC-08-13 我替他挑的酒店要在说明里点明"可以换"，不能像既成事实"""
    consultant.chat('t', TEXT_WINDOW, TODAY)
    plan = consultant.chat('t', '你帮我选吧', TODAY)['plan']
    assert plan['hotel'] == 'recommend'
    assert any('可以换' in n or '可改' in n for n in plan['notes'])


def test_meal_is_asked_before_deciding(consultant):
    """TC-08-14 吃哪儿也要问：追问里出现 meal，且给出可点选项"""
    out = consultant.chat('t', TEXT_WINDOW, TODAY)
    keys = [q['key'] for q in out.get('questions') or []]
    assert 'meal' in keys
    q = next(x for x in out['questions'] if x['key'] == 'meal')
    assert len(q['options']) >= 3
    assert '就近随便' in q['options']


def test_unasked_meal_is_a_pending_block_not_a_venue(consultant):
    """TC-08-15 用户没说吃什么时，午饭是"就近吃"的时间块，不替他点名餐厅"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    lunch = [i for d in plan['days'] for i in d['items']
             if '吃午饭' in str(i.get('title') or '')]
    assert lunch, '每天应保留一顿午饭时间'
    assert all(not i.get('poi') for i in lunch)
    assert plan['meal'] == ''


def test_meal_options_are_near_the_day_area(consultant):
    """TC-08-16 餐饮候选按当天活动区域给，不跨全城凑评分榜"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    first = next(i['poi'] for i in plan['days'][0]['items'] if i.get('poi'))
    assert '南京邮电' in first
    assert plan['meal_options'], '应有餐饮候选'
    assert all(h['district'] == '栖霞区' for h in plan['meal_options'])


def test_picking_a_restaurant_fills_that_lunch(consultant):
    """TC-08-17 点候选餐厅 → 那顿午饭换成具体店，其余天仍待定"""
    plan = consultant.chat('t', TEXT_WINDOW, TODAY)['plan']
    name = plan['meal_options'][0]['name']
    plan2 = consultant.chat('t', name, TODAY)['plan']
    assert plan2['meal'] == name
    filled = [i for d in plan2['days'] for i in d['items'] if i.get('poi') == name]
    assert filled, '点的那家店要排进草案'
    assert plan2['meal_options'] == []


def test_casual_meal_answer_lets_consultant_choose(consultant):
    """TC-08-18 说「就近随便」→ 由系统按位置挑，仍要有具体店名"""
    consultant.chat('t', TEXT_WINDOW, TODAY)
    plan = consultant.chat('t', '午饭就近随便', TODAY)['plan']
    assert plan['meal'] == 'recommend'
    named = [i['poi'] for d in plan['days'] for i in d['items']
             if '吃饭' in str(i.get('title') or '') and i.get('poi')]
    assert named, '让他挑就要挑出一家具体的'
