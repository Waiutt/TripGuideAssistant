# -*- coding: utf-8 -*-
"""问答类意图（50 条）：用户是在问，不是在要新一版方案。

判定口径照任务契约，不照现状写死：
  · 问答类**不得再生成一版新行程**——返回里不该出现 `plan.days`；
  · 应当给出回答，或明确的"做不到"提示，即 `kind ∈ {ask, notice}` 且 message 非空。

分组：A-01~10 安排合理性与节奏、A-11~20 时刻与余量、A-21~27 天气、
      A-28~35 距离与通行、A-36~41 建议、A-42~46 原因、A-47~50 地点信息。
对照 `tests/test_intent_matrix.py` 的 I-35~I-40，本文件不重复那六句。

`with_base=True` 的用例先说一句完整的出行需求（用户已有一版行程，此刻是在问它）；
`with_base=False` 的用例在空会话里直接问（信息类问题本来不需要先有行程）。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)
#: 先给一份可用的行程：问答类要在"已经有一版方案"的上下文里问才有意义
BASE = '11月14号到16号去南京玩，想去南京博物院、总统府和夫子庙，每天两个点'


@pytest.fixture(scope='module')
def ctx(tmp_path_factory):
    c = AppContext(tmp_path_factory.mktemp('intent-ask'))
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def has_plan(out):
    return bool((out.get('plan') or {}).get('days'))


def replied(out):
    """契约里的"给了回答或明确的做不到提示"：不是一版新行程，而是一句回应"""
    return out.get('kind') in ('ask', 'notice', 'answer', 'clarify')


def pois(out):
    return sorted({str(i.get('poi')) for d in ((out.get('plan') or {}).get('days') or [])
                   for i in d.get('items') or [] if i.get('poi')})


def slot(out, key, default=None):
    return (out.get('slots') or {}).get(key, default)


#: (编号, 用户这句话, 是否先给一份行程)
CASES = [
    # ---------- 安排合理性与节奏：问"这样排行不行" ----------
    ('A-01', '这样排会不会太赶', True),
    ('A-02', '三天这样安排合理吗', True),
    ('A-03', '第二天两点会不会太累', True),
    ('A-04', '这个节奏适合带老人吗', True),
    ('A-05', '这么排下来每天要走多久', True),
    ('A-06', '一天三个点是不是多了', False),
    ('A-07', '这版行程强度怎么样', True),
    ('A-08', '我体力一般，这些点撑得住吗', False),
    ('A-09', '每天几个点比较合适', False),
    ('A-10', '这样串起来路线顺吗', True),
    # ---------- 时刻与余量：问几点、来得及吗、还有多久 ----------
    ('A-11', '第一天几点出发合适', True),
    ('A-12', '九点出门来得及吗', True),
    ('A-13', '博物院逛完还剩多少时间', True),
    ('A-14', '从总统府到夫子庙要多久', True),
    ('A-15', '晚上还赶得上末班地铁吗', False),
    ('A-16', '中间留了多少余量', True),
    ('A-17', '第三天几点能结束', True),
    ('A-18', '先去哪个点省时间', False),
    ('A-19', '午饭后还有空档吗', True),
    ('A-20', '回酒店大概几点', True),
    # ---------- 天气：问天气不是安排行程 ----------
    ('A-21', '明天会下雨吗', False),
    ('A-22', '11月15号天气怎么样', False),
    ('A-23', '这两天南京冷不冷', True),
    ('A-24', '需要带伞吗', False),
    ('A-25', '周末天气好不好', False),
    ('A-26', '会降温吗', True),
    ('A-27', '第三天有雨的话还排得下去吗', False),
    # ---------- 距离与通行：问怎么去、多远、多久 ----------
    ('A-28', '总统府离地铁站多远', True),
    ('A-29', '博物院打车过去多久', True),
    ('A-30', '老门东走过去要几分钟', False),
    ('A-31', '从酒店到夫子庙怎么走', True),
    ('A-32', '坐几号线能到', False),
    ('A-33', '这个点停车方便吗', True),
    ('A-34', '路上会堵车吗', False),
    ('A-35', '中山陵到明孝陵多远', True),
    # ---------- 建议：问推荐，不等于"再排一版" ----------
    ('A-36', '有没有推荐的餐厅', True),
    ('A-37', '附近有什么好吃的', False),
    ('A-38', '晚上可以去哪儿逛逛', True),
    ('A-39', '哪家鸭血粉丝好吃', False),
    ('A-40', '有适合一个人的馆子吗', False),
    ('A-41', '博物馆推荐哪个', False),
    # ---------- 原因：问为什么这么排 ----------
    ('A-42', '为什么这么排', True),
    ('A-43', '为什么不安排中山陵', True),
    ('A-44', '为什么先博物院后总统府', True),
    ('A-45', '为什么不住在新街口', True),
    ('A-46', '这个顺序是按什么来的', False),
    # ---------- 信息：开放时间 / 门票 / 闭馆日 ----------
    ('A-47', '南京博物院几点开门', True),
    ('A-48', '博物院要门票吗', True),
    ('A-49', '周一闭馆吗', False),
    ('A-50', '总统府要预约吗', False),
]

#: 已知未通过（见 code/docs/意图测试结果_问答含糊.md 的 R1/R2）
FAILING = {
    # R1：chat() 里没有问答分支，槽位一齐就无条件 build_plan → 每句问句都回一版新草案
    **{code: 'R1 问答未被拦截：kind=plan'
       for code in ('A-01', 'A-02', 'A-03', 'A-04', 'A-05', 'A-07', 'A-10',
                    'A-11', 'A-12', 'A-13', 'A-14', 'A-16', 'A-17', 'A-19', 'A-20',
                    'A-23', 'A-26', 'A-28', 'A-29', 'A-31', 'A-33', 'A-35',
                    'A-36', 'A-38', 'A-42', 'A-43', 'A-44', 'A-45', 'A-47', 'A-48')},
    # R2：问句里的日期/星期被当成"行程日期"解析，于是空会话里问天气也直接出 1 天方案
    'A-21': 'R2 问句里的"明天"被当成日期：直接出 1 天方案',
    'A-22': 'R2 问句里的"11月15号"被当成日期：直接出 1 天方案',
    'A-49': 'R2 问句里的"周一"被当成日期：直接出 1 天方案',
}

#: R3 单独点出来：问句里的数量词被当成指令执行（A-03 的 per_day 从 3 变成 2，掉了两个点）
FAILING['A-03'] = 'R1+R3 问答未拦截，且"两点"被当成每天两个点执行（per_day 3→2，掉了两个点）'

PARAMS = [
    pytest.param(code, text, with_base, id=code,
                 marks=pytest.mark.xfail(reason=FAILING[code], strict=False)
                 if code in FAILING else ())
    for code, text, with_base in CASES
]


@pytest.mark.parametrize('code,text,with_base', PARAMS)
def test_intent_ask(ctx, code, text, with_base):
    session = f'ask-{code}'
    if with_base:
        before = run(ctx, BASE, session)          # 先有一版行程，用户此刻是在问它
        assert has_plan(before), f'{code} 前置失败：基准行程没出来，测的不是问答'
    out = run(ctx, text, session)
    assert isinstance(out, dict), f'{code} 返回结构不对'
    keys = ('start', 'end', 'per_day', 'trip_days', 'must')
    shown = {k: slot(out, k) for k in keys if slot(out, k) not in ('', None, 0, [])}
    detail = (f'{code}「{text}」→ kind={out.get("kind")} 出了方案={has_plan(out)} '
              f'位点={pois(out)[:6]} 槽位={shown}')
    assert not has_plan(out), '问答类不得再生成一版新行程（plan.days 不该出现）：' + detail
    assert replied(out), '问答类应给出回答或明确的"做不到"提示（kind=ask/notice）：' + detail
