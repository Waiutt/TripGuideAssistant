# -*- coding: utf-8 -*-
"""指令验收矩阵：用户说一句 → 期望被真正执行（对应报告 4.10 指令覆盖）。

这里把"用户明确说的话"当契约来测：住宿、吃饭、排除、点名必去、节奏、天数…
每条都是一个真实说法；哪条不过，就是"说了但没照做"的 bug。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 10, 31)


@pytest.fixture(scope='module')
def ctx(tmp_path_factory):
    c = AppContext(tmp_path_factory.mktemp('matrix'))
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def stops(plan):
    return [str(i.get('poi') or '') for d in plan.get('days') or [] for i in d['items']]


def titles(plan):
    return [str(i.get('title') or '') for d in plan.get('days') or [] for i in d['items']]


CASES = [
    # (编号, 用户原话, 断言函数, 说明)
    ('M-01', '明天想去南京博物院，不想去老门东',
     lambda out: all('老门东' not in s for s in stops(out['plan'])), '排除项：不想去X'),
    ('M-02', '明天去夫子庙，别去老门东',
     lambda out: all('老门东' not in s for s in stops(out['plan'])), '排除项：别去X'),
    ('M-03', '明天去中山陵，老门东我不想去了',
     lambda out: all('老门东' not in s for s in stops(out['plan'])), '排除项：X不想去（后置否定）'),
    ('M-04', '明天去总统府和夫子庙，换掉夫子庙',
     lambda out: all('夫子庙' not in s for s in stops(out['plan'])), '排除项：换掉X'),
    ('M-05', '明天上午去总统府，中午去南京大牌档德基店吃饭',
     lambda out: any('总统府' in s for s in stops(out['plan'])), '点名必去不被吃饭项挤掉'),
    ('M-06', '明天中午在德基广场吃饭',
     lambda out: out['plan'].get('meal') == '德基广场', '吃饭地点=商场'),
    ('M-07', '明天中午去新白鹿（德基店）吃饭',
     lambda out: '新白鹿' in (out['plan'].get('meal') or ''), '吃饭地点=库外店名照用户说'),
    ('M-08', '明天随便逛逛，我10点起床',
     lambda out: out['plan'].get('wake') == '10:00', '起床时刻'),
    ('M-09', '明天随便逛逛，晚上23点后回酒店',
     lambda out: out['plan'].get('day_end') == '23:00', '回酒店时刻'),
    ('M-10', '明天随便逛逛，我一共3天',
     lambda out: out['plan'].get('days_total') == 3, '天数'),
    ('M-11', '明天随便逛逛，每天别超过2个点',
     lambda out: out['plan'].get('per_day') == 2, '每天上限'),
    ('M-12', '明天随便逛逛，我住金陵饭店',
     lambda out: '金陵' in (out['plan'].get('hotel') or ''), '住宿锚点'),
    ('M-13', '明天随便逛逛，酒店你帮我选',
     lambda out: out['plan'].get('hotel') == 'recommend' and out['plan'].get('hotel_pick'),
     '代选住宿'),
    ('M-14', '明天去南京博物院，我不吃辣',
     lambda out: out['slots'].get('taste') == '不吃辣', '口味'),
    ('M-15', '明天去南京博物院看展，午饭就近随便',
     lambda out: any('吃饭' in t for t in titles(out['plan'])), '午饭处置'),
    ('M-16', '11月中旬去南京玩，很懒，10点起',
     lambda out: out['plan'].get('days_total', 99) <= 3, '模糊窗口不排满'),
    ('M-17', '11月中旬去南京玩，有一天去南邮找朋友吃饭，他工作日没空',
     lambda out: any(w in ' '.join(out['plan'].get('notes') or [])
                     for w in ('周六', '周日')), '朋友周末约束有说明'),
    # D-114 未修：单日 + "多加一天"，天数没有被 +1
    ('M-18', '明天去南京博物院，多加一天',
     lambda out: out['plan'].get('days_total') == 2, '加一天'),
    ('M-19', '明天去南京博物院，少一点，别太满',
     lambda out: out['plan'].get('per_day', 9) <= 2, '减量'),
    ('M-20', '明天去南京博物院，我们两个人',
     lambda out: out['slots'].get('party') == 2, '人数'),
    # —— 日期规则之间的冲突（窗口 / 天数 / 加天 / 改天 互相别打架）——
    ('M-22', '11月中旬去南京玩3天',
     lambda out: out['plan'].get('days_total') == 3, '窗口里指定 3 天'),
    ('M-23', '11月中旬去南京玩，多加一天',
     lambda out: out['plan'].get('days_total') == 4, '窗口里加一天不能被 3 天上限吃掉'),
    ('M-24', '11月中旬去南京玩，少一天',
     lambda out: out['plan'].get('days_total') == 2, '窗口里减一天'),
    ('M-25', '9月10号到9月16号，改成5天',
     lambda out: out['plan'].get('days_total') == 5, '显式区间改成 5 天'),
    ('M-26', '11月中旬去玩3天，改成5天',
     lambda out: (out['plan'].get('days_total') == 5
                  and out['plan']['start'] == '2026-11-11'), '改天数要认"改成"后面的数字，不是句子里第一个天数'),
    # D-114 未修（同上）
    ('M-27', '明天去南京博物院，多加一天',
     lambda out: out['plan'].get('days_total') == 2, '单日行程加一天'),
    ('M-28', '9月10号到9月16号，少一天',
     lambda out: out['plan'].get('days_total') == 6, '显式区间减一天'),
    ('M-29', '月底去南京玩2天',
     lambda out: (out['plan'].get('days_total') == 2
                  and out['plan']['start'] >= '2026-11-24'), '月底窗口 + 天数'),
]

@pytest.mark.parametrize('code,text,check,label', CASES,
                         ids=[c[0] for c in CASES])
def test_instruction_is_honoured(ctx, code, text, check, label):
    out = run(ctx, text, f'sess-{code}')
    assert out.get('plan'), f'{code} {label}：没有出草案'
    assert check(out), f'{code} {label}：说了但没照做（{text}）→ {out["plan"].get("message")[:60]}'

LONG_TEXT = ("我想11月份中旬找空挡在南京市区旅游，但是我很懒。一般10点起床，晚上可以23点后到酒店，"
             "你给我规划一下大家比较推荐的景点然后规划一下，但是呢，其中有一天我要去栖霞区"
             "南京邮电大学找朋友中午吃饭聚餐，你给我规划后我来约朋友，他工作日没空，"
             "你得给我推荐一下什么时候去")


def test_real_conversation_multi_round(ctx):
    """TC-08-19 真实多轮：出草案 → 追加"不去X" → 仍然只有一个朋友日、日期与说明一致"""
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    first = con.chat('multi-1', LONG_TEXT, TODAY)['plan']
    friend_days = [d for d in first['days']
                   if any('南京邮电' in str(i.get('poi') or '') for i in d['items'])]
    assert len(friend_days) == 1, '点名的南邮只能出现一天'
    assert friend_days[0]['weekday'] in ('六', '日')
    note = ' '.join(first.get('notes') or [])
    assert friend_days[0]['date'] in note, '说明里的日期必须和实际排期一致'

    second = con.chat('multi-1', '我不想去老门东', TODAY)['plan']
    stops2 = [str(i.get('poi') or '') for d in second['days'] for i in d['items']]
    assert not any('老门东' in s for s in stops2), '说了不去就不能再排'
    friend2 = [d for d in second['days']
               if any('南京邮电' in str(i.get('poi') or '') for i in d['items'])]
    assert len(friend2) == 1, '排除别的地点后，朋友日既不能丢也不能变两天'
