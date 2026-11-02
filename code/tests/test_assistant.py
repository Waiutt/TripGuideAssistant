# -*- coding: utf-8 -*-
"""AI 助手问答的测试（对应报告第 4 章 TC-15-*）。

覆盖：离线确定性答复、在线答复与上下文注入、在线失败降级、空输入、空日程。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.models import ChatReply

D = date(2026, 10, 28)


class FakeLLM:
    """记录调用参数的假大模型，用于验证上下文注入与降级"""

    provider = 'fake'

    def __init__(self, reply='好的，已收到你的问题。', raise_exc=None):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls: list[list[dict]] = []

    def name(self) -> str:
        return 'FakeLLM'

    def complete_json(self, system, user, schema):
        return {}

    def chat(self, messages, temperature=0.6):
        self.calls.append(messages)
        if self.raise_exc:
            raise self.raise_exc
        return self.reply


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def _go_online(ctx, fake):
    """把上下文切到在线模式，但用假大模型顶替真实适配器（不发网络请求）。

    地图仍用离线实现：本组用例只验证"问答链路与上下文注入"，
    不应依赖真实地图配额（真实链路见第 4 章 4.9 节的局限说明）。
    """
    from nanjingtrip.adapters.mock import MockMapAdapter
    ctx.settings.offline_mode = False
    ctx.settings.llm_api_key = 'sk-test'
    ctx.settings.map_api_key = 'amap-test'
    ctx.apply_settings(ctx.settings)
    ctx.degrade._llm = fake
    ctx.degrade._map = MockMapAdapter()
    return ctx


# ---------------------------------------------------------------- TC-15-01
def test_offline_answers_with_local_schedule(ctx):
    """TC-15-01 离线模式问行程 → 用本地数据回答，不编造"""
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)
    reply = ctx.assistant_svc.ask('我今天有什么安排？', plan_date=D)
    assert isinstance(reply, ChatReply)
    assert reply.source == 'offline'
    assert '看展' in reply.text and '09:00' in reply.text
    assert '离线模式' in reply.text          # 明确告知当前能力边界


# ---------------------------------------------------------------- TC-15-02
def test_offline_general_question_gives_hint(ctx):
    """TC-15-02 离线模式问通用问题 → 提示需要配置大模型 Key"""
    reply = ctx.assistant_svc.ask('解释一下什么是递归', plan_date=D)
    assert reply.source == 'offline'
    assert '大模型 API Key' in reply.text or 'API Key' in reply.text


# ---------------------------------------------------------------- TC-15-03
def test_online_uses_model_and_injects_context(ctx):
    """TC-15-03 在线模式 → 走大模型，且把本地行程注入上下文"""
    fake = FakeLLM(reply='你今天 09:00 有看展，建议 08:30 出发。')
    _go_online(ctx, fake)
    ctx.trip_svc.add_text('上午九点去南京博物院看展，大概2小时', D)

    reply = ctx.assistant_svc.ask('我今天几点出门合适？', plan_date=D)
    assert reply.source == 'online'
    assert '看展' in reply.text
    assert fake.calls, '应调用大模型'
    prompt = fake.calls[-1][-1]['content']
    assert '【本地行程】' in prompt and '看展' in prompt
    assert '我今天几点出门合适？' in prompt


# ---------------------------------------------------------------- TC-15-04
def test_online_failure_degrades_to_offline(ctx):
    """TC-15-04 在线调用失败 → 降级为离线答复且 ok=False，不抛异常"""
    fake = FakeLLM(raise_exc=RuntimeError('timeout'))
    _go_online(ctx, fake)
    reply = ctx.assistant_svc.ask('随便聊聊', plan_date=D)
    assert reply.ok is False
    assert reply.source == 'error'
    assert '在线问答失败' in reply.text


# ---------------------------------------------------------------- TC-15-05
def test_empty_question(ctx):
    """TC-15-05 空问题 → 直接提示，不调用模型"""
    reply = ctx.assistant_svc.ask('   ', plan_date=D)
    assert reply.ok is False
    assert '请输入' in reply.text


# ---------------------------------------------------------------- TC-15-06
def test_day_brief_on_empty_day(ctx):
    """TC-15-06 无安排的一天 → 日报为空提示，不报错"""
    brief = ctx.assistant_svc.day_brief(D)
    assert '暂无安排' in brief


# ---------------------------------------------------------------- TC-15-07
def test_history_is_passed_through(ctx):
    """TC-15-07 多轮对话历史被送给模型（最多保留 6 条）"""
    fake = FakeLLM()
    _go_online(ctx, fake)
    history = [{'role': 'user', 'content': f'问题{i}'} for i in range(10)]
    ctx.assistant_svc.ask('最后一个问题', history=history, plan_date=D)
    sent = fake.calls[-1]
    assert len(sent) <= 7                     # 6 条历史 + 本次提问
    assert sent[-1]['role'] == 'user'
    assert '最后一个问题' in sent[-1]['content']


# ---------------------------------------------------------------- TC-15-08
def test_ask_with_precomputed_context_avoids_db(ctx):
    """TC-15-08 传入预计算上下文时不得访问数据库（工作线程里不能用 sqlite 连接）"""
    fake = FakeLLM(reply='好，按你说的排。')
    _go_online(ctx, fake)

    def _boom(_d):
        raise AssertionError('工作线程中不应访问数据库')

    ctx.assistant_svc.trip_svc.day_view = _boom     # 一旦被调用就失败
    reply = ctx.assistant_svc.ask('我今天有什么安排？', plan_date=D,
                                  context='10月28日共 1 项安排：09:00-11:00 看展')
    assert reply.source == 'online' and reply.ok
    assert '10月28日共 1 项安排' in fake.calls[-1][-1]['content']

    # 离线模式同样不能碰数据库
    ctx.settings.offline_mode = True
    ctx.degrade.refresh(ctx.settings)
    offline = ctx.assistant_svc.ask('我今天有什么安排？', plan_date=D,
                                    context='10月28日共 2 项安排')
    assert offline.source == 'offline'
    assert '10月28日共 2 项安排' in offline.text


# ---------------------------------------------------------------- TC-02-13
def test_llm_reported_missing_is_not_trusted(ctx):
    """TC-02-13 模型自报的缺失项不采信：把可选项（交通方式）算成缺失时不得误判失败"""
    class OverstrictLLM(FakeLLM):
        def complete_json(self, system, user, schema):
            return {'date': '2026-10-28', 'start_hint': '15:00',
                    'mention': '南京博物院', 'item': '看展', 'duration_min': 120,
                    'mode_hint': None, 'category': '展览', 'confidence': 0.95,
                    'missing': ['交通方式']}

    fake = OverstrictLLM()
    _go_online(ctx, fake)
    intent = ctx.parse_svc.parse('明天下午三点去南京博物院看展，大概2小时', D)
    assert intent.missing == [], '交通方式是可选项，不应进入缺失清单'

    out = ctx.trip_svc.preview_text('明天下午三点去南京博物院看展，大概2小时', D)
    assert out.ok is True, out.error
    assert out.draft is not None and out.draft.poi is not None


# ---------------------------------------------------------------- TC-02-12
def test_llm_wrong_date_is_overridden_by_rules(ctx):
    """TC-02-12 在线解析把"明天"算错年份时，以规则算出的日期为准"""
    class DateHallucinatingLLM(FakeLLM):
        def complete_json(self, system, user, schema):
            self.calls.append([{'role': 'system', 'content': system}])
            return {'date': '2025-06-13',       # 明显错误的日期
                    'start_hint': '15:00', 'mention': '南京博物院',
                    'item': '看展', 'duration_min': 120, 'mode_hint': None,
                    'category': '展览', 'confidence': 0.95, 'missing': []}

    fake = DateHallucinatingLLM()
    _go_online(ctx, fake)
    intent = ctx.parse_svc.parse('明天下午三点去南京博物院看展，大概2小时', D)
    assert intent.date == date(2026, 10, 29), f'"明天"应解析为 {D} 的次日'
    assert intent.duration_min == 120 and intent.mention == '南京博物院'
    # 系统提示里必须带上了"今天"的日期，模型才有机会算对
    assert '2026-10-28' in fake.calls[0][0]['content']
