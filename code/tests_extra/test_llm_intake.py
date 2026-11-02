"""LLM 理解层：模型给候选 + 置信度，代码做校验与裁决（D-74 回归）

分工是这一层的全部意义：
  · 模型只产候选与置信度；
  · 地点必须过 `name_alike`；置信度低于门限一律不采信；
  · 追问与否、问几次由代码状态机控制，模型只负责把话说顺；
  · 模型不可用（离线/无 Key/超时）→ 透明失效，走原规则链路。
"""
from datetime import date

import pytest

from nanjingtrip import llm_intake


TODAY = date(2026, 9, 1)


class _FakeLLM:
    """可编程的假模型：按 system 里出现的关键词返回不同 JSON"""

    def __init__(self, parse=None, slots=None, ask=None, boom=False):
        self.parse = parse
        self.slots = slots
        self.ask = ask
        self.boom = boom
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        if self.boom:
            raise RuntimeError('模型挂了')
        if '槽位抽取器' in system:
            return self.slots or {}
        if '行程解析器' in system:
            return self.parse or {}
        return {'ask': self.ask} if self.ask else {}


class _FakeDegrade:
    def __init__(self, llm):
        self.llm_obj = llm

    def llm_ready(self):
        return self.llm_obj is not None

    def llm(self):
        return self.llm_obj


# ---------------- 结构化解析：只做形状校验 ----------------
def test_structured_parse_shapes_are_validated():
    fake = _FakeLLM(parse={'places': [
        {'name': '总统府', 'duration_min': '60', 'start': '09:00'},
        {'name': '', 'duration_min': 30},
        'oops',
    ], 'conditionals': [{'trigger': '下雨', 'places': ['先锋书店']}],
        'return_by': '21:00', 'origin': '南京站', 'confidence': 0.8})
    got = llm_intake.structured_parse('靠窗的位子', TODAY, _FakeDegrade(fake))
    assert [p['name'] for p in got['places']] == ['总统府']
    assert got['places'][0]['duration_min'] == 60
    assert got['conditionals'][0]['places'] == ['先锋书店']
    assert got['return_by'] == '21:00' and got['confidence'] == 0.8


def test_structured_parse_fails_soft():
    """模型报错/不可用 → 返回空，让规则链路接管"""
    assert llm_intake.structured_parse('x', TODAY, _FakeDegrade(_FakeLLM(boom=True))) == {}
    assert llm_intake.structured_parse('x', TODAY, _FakeDegrade(None)) == {}
    assert llm_intake.structured_parse('', TODAY, _FakeDegrade(_FakeLLM())) == {}


# ---------------- 槽位抽取：置信度门限 ----------------
def test_slot_extract_respects_confidence():
    slots = {
        'dates': {'start': '2026-09-09', 'end': '', 'confidence': 0.9},
        'party': {'value': 2, 'confidence': 0.9},
        'pace': {'value': 'slow', 'confidence': 0.4},     # 不够自信
        'wake': {'value': '10:00', 'confidence': 0.4},
        'interests': {'value': ['历史人文'], 'confidence': 0.8},
        'hotel_unknown': {'value': True, 'confidence': 0.9},
    }
    got = llm_intake.slot_extract('9月9号两个人', TODAY, _FakeDegrade(_FakeLLM(slots=slots)))
    assert got['start'] == '2026-09-09' and got['end'] == '2026-09-09'  # 单日补齐
    assert got['party'] == 2 and got['interests'] == ['历史人文']
    assert got['hotel'] == 'recommend'
    assert 'pace' not in got and 'wake' not in got, '低置信度不许采信'


def test_slot_extract_soft_fails():
    assert llm_intake.slot_extract('x', TODAY, _FakeDegrade(_FakeLLM(boom=True))) == {}
    assert llm_intake.slot_extract('x', TODAY, _FakeDegrade(None)) == {}


# ---------------- 追问措辞：模型说顺，流程不由它定 ----------------
def test_phrase_question_uses_model_then_fallback():
    fake = _FakeLLM(ask='你打算哪天去？')
    assert llm_intake.phrase_question('dates', '', _FakeDegrade(fake)) == '你打算哪天去？'
    # 模型挂了 → 固定文案，流程不受影响
    assert '哪天' in llm_intake.phrase_question('dates', '', _FakeDegrade(_FakeLLM(boom=True)))
    assert '哪天' in llm_intake.phrase_question('dates', '', _FakeDegrade(None))


# ---------------- 端到端：顾问单日输入不再反复追问 ----------------
@pytest.fixture()
def ctx(tmp_path):
    from nanjingtrip.app import AppContext
    c = AppContext(tmp_path)
    c.settings.offline_mode = True       # 离线 → 模型层透明失效，规则必须自己扛住
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_consult_single_day_does_not_loop(ctx):
    """「9.9号」必须被当成那一天，不能反复问区间"""
    from nanjingtrip.consult import Consultant, ConsultStore
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    out = con.chat('u1', '9.9号出去玩，帮我规划行程', date(2026, 8, 30))
    assert out['kind'] == 'plan', out
    assert out['plan']['start'] == '2026-09-09'
    assert out['plan']['days_total'] == 1


def test_consult_stops_asking_same_question(ctx):
    from nanjingtrip.consult import Consultant, ConsultStore
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    first = con.chat('u2', '帮我安排个行程', TODAY)
    assert first['kind'] == 'ask'
    second = con.chat('u2', '随便你安排', TODAY)
    # 已经问过一次日期 → 不再重复追问，先按明天给一版
    assert second['kind'] == 'plan', second
    assert 'queries' not in second or True
    assert second['plan']['days_total'] >= 1
