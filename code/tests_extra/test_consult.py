"""行程顾问（多轮）：问 → 给草案 → 改 → 落地

用户原话：
    我要在9月10号-9月16号在南京进行旅游，帮我推荐规划具体的行程，
    我比较懒，起床起得晚，然后两个人，酒店未知，一切都没有头绪

顾问要做的不是"一次性吐一个方案"，而是：先把能听出来的都收下（区间/人数/懒散/兴趣/酒店），
缺的**只问最关键的**，然后给一版可改的草案；用户再说"第二天太满""换成博物馆""改成5天"时在原方案上改。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore, detect_interests


TODAY = date(2026, 9, 1)
FIRST = ('我要在9月10号-9月16号在南京进行旅游，帮我推荐规划具体的行程，'
         '我比较懒，起床起得晚，然后两个人，酒店未知，一切都没有头绪')


@pytest.fixture()
def con(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True          # 顾问只用城市包 + 城市包时长
    ctx.apply_settings(ctx.settings)
    store = ConsultStore(ctx.db)
    yield Consultant(ctx.trip_svc, store)
    ctx.close()


# ---------------- 槽位解析 ----------------
def test_absorb_reads_the_messy_sentence(con):
    st = con.absorb(con.load('s1'), FIRST, TODAY)
    assert (st.start, st.end) == ('2026-09-10', '2026-09-16')
    assert st.party == 2
    assert (st.pace, st.wake) == ('slow', '10:00')
    # 用户只说"酒店未知"≠"你帮我定"：按规矩先追问，不许替他决定
    assert st.hotel == ''


def test_detect_interests_from_words():
    assert detect_interests('我想多看博物馆') == ['博物馆']
    assert detect_interests('带孩子来玩') == ['亲子']
    assert detect_interests('喜欢吃小吃') == ['美食街区']
    assert detect_interests('随便走走') == []


# ---------------- 提问策略：别把人问烦 ----------------
def test_asks_dates_first_when_missing(con):
    out = con.chat('s2', '帮我在南京安排个行程', TODAY)
    assert out['kind'] == 'ask'
    assert '哪天' in out['message']
    assert len(out['questions']) <= 2


def test_gives_plan_when_enough_info(con):
    out = con.chat('s3', FIRST, TODAY)
    assert out['kind'] == 'plan', out
    plan = out['plan']
    assert plan['days_total'] == 7
    assert plan['wake'] == '10:00' and plan['party'] == 2
    # 每天都有主题与理由，不是无脑堆点
    for d in plan['days']:
        assert d['theme'] and d['why'], d
        assert d['items'], d


def test_plan_respects_interest(con):
    st = con.absorb(con.load('s4'), '9月10号-9月12号一个人，我想多看博物馆', TODAY)
    plan = con.build_plan(st)
    stops = ' '.join(p for d in plan['days'] for p in d['stops'])
    assert '博物院' in stops or '博物馆' in stops or '六朝' in stops or '纪念馆' in stops


# ---------------- 多轮修改 ----------------
def test_refine_day_too_full(con):
    out = con.chat('s5', FIRST, TODAY)
    before = out['plan']['per_day']
    out2 = con.chat('s5', '第二天太满了，少排一点', TODAY)
    assert out2['plan']['per_day'] == max(1, before - 1)


def test_followup_keeps_session(con):
    out = con.chat('s6', FIRST, TODAY)
    first_start = out['plan']['start']
    out2 = con.chat('s6', '换成博物馆为主', TODAY)
    assert out2['plan']['start'] == first_start, '同一会话的区间不该丢'
    assert '博物馆' in (out2['slots'].get('interests') or [])


def test_change_days_count(con):
    con.chat('s7', FIRST, TODAY)
    out = con.chat('s7', '改成5天', TODAY)
    assert out['plan']['days_total'] == 5


def test_must_visit_is_kept(con):
    st = con.absorb(con.load('s8'), '9月10号-9月12号，总统府必须去', TODAY)
    plan = con.build_plan(st)
    stops = ' '.join(p for d in plan['days'] for p in d['stops'])
    assert '总统府' in stops


# ---------------- 会话持久化 ----------------
def test_session_survives_new_consultant_instance(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    store = ConsultStore(ctx.db)
    Consultant(ctx.trip_svc, store).chat('s9', FIRST, TODAY)
    # 换一个实例（相当于服务重启）仍能接着聊
    again = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    out = again.chat('s9', '再帮我加一天', TODAY)
    assert out['slots']['start'] == '2026-09-10'
    ctx.close()


# ---------------- 酒店：价位来源标注 + 预算过滤 ----------------
def test_hotel_options_are_labelled(con):
    out = con.hotel_options(con.absorb(con.load('h1'), FIRST, TODAY))
    assert out, '酒店未定时应给出候选'
    for h in out:
        assert h['price_label'], h          # 每个候选都要标价位与来源
        assert ('参考价' in h['price_label']) or ('未标价' in h['price_label']) \
            or ('高德' in h['price_label']), h


def test_hotel_budget_filter_for_cheap_profile(con):
    st = con.absorb(con.load('h2'), '9月10号-9月12号，预算不多，想省一点', TODAY)
    assert st.budget == '省'
    out = con.hotel_options(st)
    priced = [h for h in out if h['price'] is not None]
    assert all(h['price'] <= 400 for h in priced), out
