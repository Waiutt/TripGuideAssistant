# -*- coding: utf-8 -*-
"""边界测试 D 层：顾问槽位矩阵（`ConsultSession.FIELDS` 32 个槽位各取 1 正 1 反的重点面）。

断言依据是契约（`code/docs/边界测试计划.md` D 层 + 顾问槽位口径），不是当前实现的行为：

  · 「酒店未知」≠「你帮我选」→ 只追问，不代选；
  · 「别去 X」只影响新草案，**不删**已有数据；「把 X 去掉」才真删；
  · 「11 月中旬」是窗口不是行程，默认先给 3 天；
  · 多轮之间槽位应累积，「重新开始」（服务端 `consult_reset`）后清空。

`xfail(strict=False)` = 与契约不符、疑似缺陷，只标出来并写进
`code/docs/边界测试结果_CD层.md`，**不改实现**；
`PENDING-DECISION` 写在用例 docstring 里，表示"这句口语该不该算某槽位"需要产品口径。
"""
from datetime import date

import pytest

from nanjingtrip import web_api
from nanjingtrip.app import AppContext
from nanjingtrip.consult import ConsultSession, Consultant, ConsultStore

TODAY = date(2026, 11, 1)
#: 基线句：给了明确日期区间，避免"没日期先追问"干扰其它槽位
BASE = '11月14号到11月16号去南京玩'
DAY1 = date(2026, 11, 14)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


@pytest.fixture()
def con(ctx):
    return Consultant(ctx.trip_svc, ConsultStore(ctx.db))


def slots(con, session):
    """当前会话的槽位快照（只取 FIELDS 里的叶子字段，不碰内部对象）"""
    st = con.load(session)
    return {k: getattr(st, k) for k in ConsultSession.FIELDS}


def planned_pois(plan, index=None):
    """草案里出现的所有地点名（None = 全部天）"""
    days = plan.get('days') or []
    rows = days if index is None else days[index:index + 1]
    return [str(i.get('poi') or '') for d in rows for i in d['items']]


def task_names(ctx, day=DAY1):
    return [t.poi.name for t in ctx.trip_svc.day_view(day).tasks if t.poi]


# ============================================== 日期：start / end / trip_days / window
def test_d01_explicit_range_fills_start_and_end(con):
    """D-01 start/end：明确区间按用户说的落槽，天数等于区间"""
    plan = con.chat('s', BASE, TODAY)['plan']
    assert (plan['start'], plan['end']) == ('2026-11-14', '2026-11-16')
    assert plan['days_total'] == 3


def test_d02_single_day_does_not_become_a_range(con):
    """D-02 start/end 反例：只说一天就不是区间（不追问、也不扩成多天）"""
    out = con.chat('s', '11月14号去南京博物院', TODAY)
    assert out['kind'] == 'plan', f'单日表述不该再追问区间：{out["message"]}'
    assert out['plan']['start'] == out['plan']['end'] == '2026-11-14'
    assert out['plan']['days_total'] == 1


def test_d03_trip_days_overrides_long_range(con):
    """D-03 trip_days：「一共3天」压过 12 天区间，终点按起点顺推"""
    out = con.chat('s', '11月14号到11月25号去南京玩，一共3天', TODAY)
    st = slots(con, 's')
    assert st['trip_days'] == 3
    assert (out['plan']['start'], out['plan']['end']) == ('2026-11-14', '2026-11-16')
    assert out['plan']['days_total'] == 3


def test_d04_mid_month_is_a_window_not_an_itinerary(con):
    """D-04 window：「11月中旬」是窗口，先按 3 天排，且草案必须落在窗口内"""
    out = con.chat('s', '11月中旬去南京玩', TODAY)
    st, plan = slots(con, 's'), out['plan']
    assert st['window'] is True
    assert (st['win_start'], st['win_end']) == ('2026-11-11', '2026-11-20'), '窗口起止要原样留档'
    assert plan['days_total'] <= 3, f'窗口 10 天不等于要玩 10 天：{plan["days_total"]}'
    assert '2026-11-11' <= plan['start'] and plan['end'] <= '2026-11-20'
    assert len(plan['days']) == plan['days_total']


@pytest.mark.xfail(reason='疑似缺陷：窗口会话第二轮说「多加一天」被 date_span 夹住，'
                          '天数不变（文案却承诺「想多玩几天说一句就行」）', strict=False)
def test_d05_window_plus_one_day_works_in_follow_up_round(con):
    """D-05 窗口会话里第二轮说「多加一天」：行程长度要真的 +1"""
    first = con.chat('s', '11月中旬去南京玩', TODAY)['plan']
    second = con.chat('s', '多加一天', TODAY)['plan']
    assert second['days_total'] == first['days_total'] + 1, \
        f'「多加一天」没生效：{first["days_total"]} → {second["days_total"]}'


# ============================================== 人数：party
def test_d06_party_is_read_and_carried_into_plan(con):
    """D-06 party：人数要落槽并进草案（不是只在槽位里待着）"""
    plan = con.chat('s', '11月14号去南京玩，4个人', TODAY)['plan']
    assert slots(con, 's')['party'] == 4
    assert plan['party'] == 4


def test_d07_party_increment_is_an_open_question(con):
    """D-07 party 反例：「再加一个人」怎么算 —— PENDING-DECISION，不断言方向"""
    con.chat('s', '11月14号去南京玩，两个人', TODAY)
    con.chat('s', '再加一个人', TODAY)
    party = slots(con, 's')['party']
    # 只钉住"不崩、是合法人数"；累加与否属产品口径（见报告 PENDING-DECISION）
    assert isinstance(party, int) and 1 <= party <= 9, f'人数槽位必须是 1–9 的整数：{party}'


# ============================================== 节奏与作息：pace / wake / day_end
@pytest.mark.parametrize('text,pace,wake', [
    ('很懒，一般10点起床', 'slow', '10:00'),      # 正：懒散
    ('我是特种兵，赶时间', 'fast', '08:30'),      # 反：赶时间
])
def test_d08_pace_and_wake(con, text, pace, wake):
    """D-08 pace/wake：懒散与赶时间要落成不同节奏 + 对应起床时刻"""
    con.chat('s', BASE, TODAY)
    con.chat('s', text, TODAY)
    st = slots(con, 's')
    assert st['pace'] == pace
    assert st['wake'] == wake


def test_d09_late_return_is_day_end_not_arrival(con):
    """D-09 day_end：「23点后回酒店」是收尾时间，不能被当成"抵达南京"这类事务要素"""
    out = con.chat('s', BASE + '，晚上23点后回酒店', TODAY)
    st = slots(con, 's')
    assert st['day_end'] == '23:00'
    assert st['practical'] is False, '回酒店时间不是事务行程'
    assert st['arrival'] == '', '不能被解析成"抵达某站"'
    assert out['plan']['day_end'] == '23:00'


# ============================================== 住宿：hotel
def test_d10_unknown_hotel_only_asks(con):
    """D-10 hotel 反例：「酒店未知」只追问，不代选（≠「你帮我选」）"""
    con.chat('s', BASE, TODAY)
    out = con.chat('s', '酒店未知', TODAY)
    st = slots(con, 's')
    assert st['hotel'] == '', '未定就是未定，不能替他填一个'
    asks = ' '.join(q.get('ask') or '' for q in out.get('questions') or [])
    assert '住哪' in asks, f'住宿未定要追问一次：{asks}'
    assert out['plan']['hotel_pick'] == '', '没委托就不能先挑好一家'


def test_d11_handing_over_the_choice_picks_a_hotel(con):
    """D-11 hotel 正例：「你帮我选」才代选，并且说明可换"""
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '住宿你帮我选吧', TODAY)['plan']
    assert slots(con, 's')['hotel'] == 'recommend'
    assert plan['hotel_pick'], '让我挑就要挑出一家具体的'
    notes = ' '.join(plan.get('notes') or [])
    assert '可以换' in notes or '可改' in notes, f'代选必须标明可改：{notes}'


def test_d12_no_hotel_answer_is_an_open_question(con):
    """D-12 hotel：「不用酒店（当天往返）」怎么算 —— PENDING-DECISION，只钉"不臆造\""""
    con.chat('s', BASE, TODAY)
    con.chat('s', '不用酒店（当天往返）', TODAY)
    hotel = slots(con, 's')['hotel']
    # 该选项怎么落槽需要产品口径（见报告 PENDING-DECISION）；这里只保证不臆造具体酒店
    assert hotel in ('', 'recommend'), f'不能凭空落成某家酒店：{hotel}'


# ============================================== 正餐：meal / taste
def test_d13_casual_meal_answer_still_names_a_restaurant(con):
    """D-13 meal：「就近随便」= 由系统挑，但必须挑出具体店名"""
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '午饭就近随便', TODAY)['plan']
    assert slots(con, 's')['meal'] == 'recommend'
    named = [p for p in planned_pois(plan) if p]
    assert named, '让他挑就要给出具体餐厅'


def test_d14_named_restaurant_wins(con):
    """D-14 meal 反例：点名某家店就以他为准，不许换成"最近的店\""""
    con.chat('s', BASE, TODAY)
    con.chat('s', '中午去南京大牌档德基店吃饭', TODAY)
    assert slots(con, 's')['meal'] == '南京大牌档德基店'


def test_d15_taste_sentence_is_not_a_restaurant_name(con):
    """D-15 taste：「清真」是口味，不该被当成点名餐厅"""
    con.chat('s', BASE, TODAY)
    out = con.chat('s', '我想吃清真的', TODAY)
    st = slots(con, 's')
    assert st['taste'] == '清真'
    assert st['meal'] != '清真的', f'口味词被写成了店名：{st["meal"]!r}'
    assert out['plan']['meal_options'], '口味已知时要给得出候选'


def test_d16_no_spicy_taste_filters_candidates(con):
    """D-16 taste 反例：「不吃辣」要真的过滤掉辣的候选"""
    con.chat('s', BASE + '，我不吃辣', TODAY)
    plan = con.chat('s', '就这样', TODAY)['plan']
    assert slots(con, 's')['taste'] == '不吃辣'
    bad = ('麻辣', '辣', '川', '渝')
    names = [m['name'] for m in plan['meal_options']]
    assert names, '要有餐饮候选'
    assert not [n for n in names if any(w in n for w in bad)], f'不该推辣的店：{names}'


# ============================================== 点名与排除：must / avoid / drop
def test_d17_must_go_lands_in_the_draft(con):
    """D-17 must：「必须去 X」进必去清单，且真的排进草案"""
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '必须去南京博物院', TODAY)['plan']
    assert '南京博物院' in slots(con, 's')['must']
    assert '南京博物院' in planned_pois(plan, 0), f'必去项要排进第一天：{planned_pois(plan)}'


def test_d18_city_name_is_not_a_must_go_poi(con):
    """D-18 must 反例：句中的城市名不是"点名必去"的景点"""
    plan = con.chat('s', BASE, TODAY)['plan']
    must = plan['must']
    assert '南京站' not in must, f'城市名被当成了必去景点：{must}'


def test_d19_soft_no_keeps_existing_data(con, ctx):
    """D-19 avoid：「别去 X」只影响新草案，已有的当天数据不能被删"""
    ctx.trip_svc.add_batch(DAY1, [{'poi': '夫子庙', 'title': '游览夫子庙',
                                   'duration_min': 90}], keep_order=True)
    assert task_names(ctx) == ['夫子庙']
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '别去夫子庙', TODAY)['plan']
    assert slots(con, 's')['avoid'] == ['夫子庙']
    assert slots(con, 's')['drop'] == [], '「别去」不是"动数据"的指令'
    assert task_names(ctx) == ['夫子庙'], '「别去」不能删掉已经排好的安排'
    assert '夫子庙' not in planned_pois(plan), '但新草案里不能再推荐它'


def test_d20_hard_no_actually_deletes(con, ctx):
    """D-20 drop：「把 X 去掉」才真的动数据，并明确告知删了什么"""
    ctx.trip_svc.add_batch(DAY1, [{'poi': '夫子庙', 'title': '游览夫子庙',
                                   'duration_min': 90}], keep_order=True)
    con.chat('s', BASE, TODAY)
    out = con.chat('s', '把夫子庙去掉', TODAY)
    assert task_names(ctx) == [], '「去掉」必须真的从行程里删掉'
    assert '删掉' in out['message'], f'删除要明说，不能静默：{out["message"]}'


# ============================================== 临时事项：extras
@pytest.mark.parametrize('text,expected', [
    ('我突然想起来要有40分钟去南京南接同学', 40),   # 正：有"去/接"+时长
    ('我有40分钟', 0),                              # 反：只有时长，不是一件事
])
def test_d21_extras_only_when_there_is_a_real_errand(con, text, expected):
    """D-21 extras：带"去哪儿办事 + 时长"才记成临时事项"""
    con.chat('s', BASE, TODAY)
    con.chat('s', text, TODAY)
    extras = slots(con, 's')['extras']
    if expected:
        assert extras, f'这句话应该记成一条临时事项：{extras}'
        assert extras[0]['duration_min'] == expected
    else:
        assert extras == [], f'只有时长不是一件事，不该记 extras：{extras}'


# ============================================== 务实行程：practical
def test_d22_arrival_is_recorded_as_fixed_item(con):
    """D-22 改口径（不区分场景）：到达是"当日固定项"，不再切换成事务模板

    原口径要求"到达→整单事务行程、不含观光点"，那种模式判断会把
    「每天回学校」这类正常说法误判成事务（D-115）。现在到达只作为固定项记录，
    是否补点由"剩余容量 + 顺路"决定。
    """
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '我10点到南京南站', TODAY)['plan']
    st = slots(con, 's')
    assert st['arrival'] == '南京南站', '到达站要记下来'
    assert st['luggage'] is True, '到站后要先去放行李'
    assert plan.get('days'), '仍要给出方案（不是空草案）'
    assert st['practical'] is True, '仍保留统计标记' 


@pytest.mark.xfail(reason='疑似缺陷：事务草案在住宿未定时把 plan["hotel"] 置成 recommend '
                          '（等于"我替你选了"），同时还在追问住哪儿', strict=False)
def test_d23_practical_does_not_claim_a_hotel_choice(con):
    """D-23 practical × hotel：用户没委托选酒店，草案就不能声称"我帮你选\""""
    con.chat('s', BASE, TODAY)
    plan = con.chat('s', '我10点到南京南站', TODAY)['plan']
    assert slots(con, 's')['hotel'] == '', '住宿槽位仍是未定'
    assert plan['hotel'] != 'recommend', f'未定 ≠ 我替你选：{plan["hotel"]!r}'
    assert plan['hotel_pick'] == '', f'不能悄悄先挑一家：{plan["hotel_pick"]!r}'


# ============================================== 朋友档期：friend_weekend
def test_d24_friend_weekend_constraint(con):
    """D-24 friend_weekend：「朋友工作日没空」要落槽（约会那天才能挪到周末）"""
    con.chat('s', BASE, TODAY)
    con.chat('s', '我朋友工作日没空，只有周末有空', TODAY)
    assert slots(con, 's')['friend_weekend'] is True


# ============================================== 跨轮累积与重新开始
def test_d25_slots_accumulate_across_rounds(con):
    """D-25 多轮累积：三轮补充的信息要同时留住，不能后一轮冲掉前一轮"""
    con.chat('s', BASE, TODAY)
    con.chat('s', '很懒，一般10点起床', TODAY)
    con.chat('s', '住宿你帮我选吧', TODAY)
    st = slots(con, 's')
    assert (st['start'], st['end']) == ('2026-11-14', '2026-11-16'), '日期不能被后一轮冲掉'
    assert (st['pace'], st['wake']) == ('slow', '10:00'), '节奏不能被后一轮冲掉'
    assert st['hotel'] == 'recommend', '住宿选择不能被后一轮冲掉'


def test_d26_consult_reset_clears_every_slot(con, ctx):
    """D-26 重新开始：服务端 consult_reset 之后槽位必须清空，旧偏好不能带过来"""
    con.chat('r', BASE + '，很懒', TODAY)
    con.chat('r', '住宿你帮我选吧', TODAY)
    assert slots(con, 'r')['start'] and slots(con, 'r')['pace'] == 'slow'
    out = web_api.command_payload(ctx, {'action': 'consult_reset', 'session': 'r'})
    assert out.get('ok') is True
    after = slots(con, 'r')
    assert after['start'] == '' and after['end'] == '', f'日期没清干净：{after["start"]}'
    assert after['pace'] == '' and after['hotel'] == '', '偏好没清干净'
    assert after['must'] == [] and after['avoid'] == []
