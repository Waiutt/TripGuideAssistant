# -*- coding: utf-8 -*-
"""Agent 能力卡（AGENT.md + SKILLS）测试（对应报告第 4 章 TC-09-*）。

能力卡的价值在于"声明不会和代码脱节"：这里就把这条守住——
字段齐、实现存在、清单渲染得出来、文档与卡不打架。
"""
import pytest

from nanjingtrip.agent import (AGENT_DOC, api_payload, catalog_text,
                               load_cards, prompt_block)
from nanjingtrip.agent.skills import REQUIRED_FIELDS, _parse_front_matter
from nanjingtrip.app import AppContext
from nanjingtrip.web_api import command_payload

MIN_SKILLS = 8


def test_all_cards_load_and_are_unique():
    """TC-09-01 能力卡能全部读出，名字唯一且按文件名稳定排序"""
    cards = load_cards()
    names = [c.name for c in cards]
    assert len(cards) >= MIN_SKILLS
    assert len(set(names)) == len(names)
    assert names == sorted(names)


def test_cards_declare_required_fields():
    """TC-09-02 每张卡都带齐触发条件/输入/输出/边界"""
    for card in load_cards():
        for field_name in REQUIRED_FIELDS:
            assert getattr(card, field_name), f'{card.name} 缺 {field_name}'
        assert card.body, f'{card.name} 正文为空（卡片应说明怎么用）'


def test_every_card_points_to_real_code():
    """TC-09-03 卡上的 impl 必须能解析成可调用的真实代码"""
    for card in load_cards():
        assert callable(card.resolve_impl()), f'{card.name} 的 impl 不可调用'


def test_network_flag_is_a_bool():
    """TC-09-04 needs_network 解析成布尔值（true/false 不能都当真）"""
    flags = {c.name: c.needs_network for c in load_cards()}
    assert flags['ask_clarify'] is False
    assert all(isinstance(v, bool) for v in flags.values())


def test_catalog_text_fits_prompt_budget():
    """TC-09-05 能力清单能塞进 prompt：含全部能力名且有长度上限"""
    text = catalog_text()
    cards = load_cards()
    assert len(text) <= 1200
    for card in cards:
        assert card.name in text


def test_agent_doc_lists_every_skill():
    """TC-09-06 AGENT.md 与能力卡不打架：每张卡都在文档里出现"""
    doc = AGENT_DOC.read_text(encoding='utf-8')
    for card in load_cards():
        assert card.name in doc or card.title in doc
    assert '不编' in doc or '证据' in doc


def test_prompt_block_combines_rules_and_skills():
    """TC-09-07 给模型的上下文 = 行为规矩 + 能力清单"""
    block = prompt_block()
    assert '硬规矩' in block
    assert '能调用的能力' in block
    assert 'plan_trip' in block


def test_bad_card_is_rejected(tmp_path):
    """TC-09-08 写坏的卡（缺字段/多出不认识的行）要当场报错，不能静默跳过"""
    (tmp_path / 'broken.md').write_text('---\nname: broken\n---\n正文', encoding='utf-8')
    with pytest.raises(ValueError):
        load_cards(tmp_path)
    (tmp_path / 'broken.md').write_text('---\nname: x\ntitle: y\n???\n---\n', encoding='utf-8')
    with pytest.raises(ValueError):
        load_cards(tmp_path)


def test_front_matter_parses_lists():
    """TC-09-09 头部解析支持 key: 值 与 key: 加 - 列表两种写法"""
    fields, body = _parse_front_matter(
        '---\nname: demo\ntitle: 演示\nwhen_to_use:\n  - 甲\n  - 乙\n---\n正文')
    assert fields['name'] == 'demo'
    assert fields['when_to_use'] == ['甲', '乙']
    assert body == '正文'


def test_agent_skills_action_is_read_only(tmp_path):
    """TC-09-10 /api/command 的 agent_skills 只读：返回目录且不改变业务数据"""
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    try:
        out = command_payload(ctx, {'action': 'agent_skills'})
        assert out['ok'] is True
        assert out['count'] >= MIN_SKILLS
        assert len(out['skills']) == out['count']
        assert {'name', 'title', 'impl', 'limits'} <= set(out['skills'][0])
        assert out['rev'] == ctx.db.revision()
    finally:
        ctx.close()


def test_api_payload_has_no_runtime_objects():
    """TC-09-11 目录是纯数据（能直接 JSON 序列化给界面）"""
    import json
    payload = api_payload()
    text = json.dumps(payload, ensure_ascii=False)
    assert '"skills"' in text and 'plan_trip' in text
