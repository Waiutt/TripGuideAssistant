# -*- coding: utf-8 -*-
"""意图层（`intent.py`）的契约测试：判断"想干什么" + 把话切成动作片段。

这一层存在的理由写在 `intent.py` 顶部：以前六个正则互相抢片段，
「去新街口吃日料」被拆成"去新街口找人 + 吃饭（日料）"，真正的南京理工反而丢了。
下面的用例把"一句话里有几件事、每件事的动词/地名/时长归谁"钉成断言。
"""
from __future__ import annotations

import datetime

import pytest

from nanjingtrip import intent
from nanjingtrip.app import AppContext

TODAY = datetime.date(2026, 11, 1)


# ---------------------------------------------------------------- 分类
@pytest.mark.parametrize('text,kind', [
    ('这样安排合理吗', 'ask'),
    ('总统府门票多少', 'ask'),
    ('把夫子庙去掉', 'prune'),
    ('老门东我不想去了', 'prune'),
    ('第二天太满了，少排一个', 'modify'),
    ('11月中旬去南京玩，帮我规划一下', 'plan'),
    ('明天去总统府看展，2小时', 'entry'),
    ('', 'clarify'),
])
def test_classify_kind(text, kind):
    """这句话想干什么：问句/删除/修改/要方案/录入，别互相冒充"""
    assert intent.classify(text)['kind'] == kind


def test_question_beats_plan_word():
    """「这样安排合理吗」里有「安排」二字，但句尾是问法 → 必须先回答"""
    got = intent.classify('这样安排合理吗')
    assert got['kind'] == 'ask', got


def test_plan_with_timing_question_still_plans():
    """「帮我规划…推荐什么时候出发」是要方案，不能因为含「什么时候」就当问答"""
    assert intent.classify('帮我规划明天的行程，推荐什么时候出发')['kind'] == 'plan'


# ---------------------------------------------------------------- 切片
def _segments(text):
    return intent.analyze(text)['segments']


def test_one_sentence_multiple_actions():
    """「从南邮去新街口吃日料，然后去南京理工找朋友1.5小时，再返校」= 出发 + 吃饭 + 找人 + 回程"""
    raw = ('我11月3号要从南京邮电大学去新街口吃日料，给我推荐一家，'
           '然后吃完去南京理工找朋友，大约耗时1.5小时，然后再返校')
    segs = _segments(raw)
    verbs = [s['verb'] for s in segs]
    assert verbs.count('eat') == 1, verbs
    assert verbs.count('meet') == 1, verbs
    assert verbs.count('return') == 1, verbs
    # 出发地只抓到"南京邮电大学"，不能把后面整句当出发地
    origins = [s.get('origin') for s in segs if s.get('origin')]
    assert origins and origins[0] == '南京邮电大学', origins


def test_eat_place_is_the_area_not_a_person():
    """「去新街口吃日料」是吃饭：片区是新街口，菜系是日料，不是「去新街口找人」"""
    segs = _segments('去新街口吃日料')
    eat = [s for s in segs if s['verb'] == 'eat'][0]
    assert eat['need'] == '日料'
    assert '找' not in eat['text']


def test_meet_keeps_its_own_place():
    """「去南京理工找朋友」的地名是南京理工，不是上一句里的新街口"""
    segs = _segments('去新街口吃日料，然后去南京理工找朋友')
    meet = [s for s in segs if s['verb'] == 'meet']
    assert meet and meet[0]['place'] == '南京理工', meet


def test_duration_attaches_to_previous_action():
    """「大约耗时1.5小时」被标点切成独立一段，时长要归给上一件事（90 分钟）"""
    segs = _segments('去南京理工找朋友，大约耗时1.5小时')
    meet = [s for s in segs if s['verb'] == 'meet'][0]
    assert meet['duration_min'] == 90


@pytest.mark.parametrize('text,minutes', [
    ('1.5小时', 90), ('半小时', 30), ('40分钟', 40), ('2小时', 120),
    ('两小时', 120), ('3个半小时', 210), ('看展', 0),
])
def test_parse_duration(text, minutes):
    assert intent.parse_duration_min(text) == minutes


def test_return_needs_no_place():
    """「再返校」只有动词：地点由出发地提供，不该凭空造一个地名"""
    segs = _segments('然后再返校')
    ret = [s for s in segs if s['verb'] == 'return']
    assert ret and ret[0]['place'] == ''


def test_noise_is_not_a_place():
    """「给我推荐一家」「帮我规划一下」里没有地名"""
    for text in ('给我推荐一家', '帮我规划一下'):
        for seg in _segments(text):
            assert seg['place'] in ('', ), seg


# ---------------------------------------------------------------- 接进顾问
@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_consult_uses_intent_layer(ctx):
    """顾问侧：一句话里三件事各就各位——吃饭按菜系推荐、找人 1.5 小时、当天往返"""
    raw = ('我11月3号要从南京邮电大学去新街口吃日料，给我推荐一家，'
           '然后吃完去南京理工找朋友，大约耗时1.5小时，然后再返校')
    rep = ctx.consult.chat('t1', raw, TODAY)
    slots = rep['slots']
    assert slots['intent_kind'] == 'plan'
    assert slots['anchor_place'] and '南京邮电' in slots['anchor_place']
    assert slots['home_return'] is True
    assert slots['taste'] == '日料' and slots['meal'] == 'recommend'
    assert slots['meal_near'] == '新街口'
    assert any('南京理工' in m for m in slots['must'])
    assert '新街口' not in slots['must']          # 吃饭的片区不是必去景点
    meet = [e for e in slots['extras'] if '找人' in e['title']]
    assert meet and meet[0]['duration_min'] == 90


@pytest.mark.parametrize('raw,name,ok', [
    ('明天去总统府看展', '总统府', True),
    ('明天去总统府看展', '然后精品瓷器(明发新城中心4幢店)', False),
    ('11月中旬去南京玩，有一天去南邮找朋友吃饭', '南京邮电大学仙林校区', True),
])
def test_spoken_in(ctx, raw, name, ok):
    """必去项的原话校验：包内全名能用简称（南邮）认出来，幻觉项必须认不出"""
    assert ctx.consult._spoken_in(raw, name) is ok
