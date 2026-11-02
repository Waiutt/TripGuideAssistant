# -*- coding: utf-8 -*-
"""助手「对话即执行」：意图路由 + 选项式确认（D-52）。

要解决的是入口分裂：底部输入框能录入、AI 助手只能聊天。
做法是把"要不要写库"交给**本地规则**判定，并分四类：
  answer  纯问答 —— 绝不写库
  preview 录入   —— 复用批量预览卡（已有"选项式"确认）
  confirm 变更   —— 先给**选项式确认卡**（含执行后效果），确认后才落库
  clarify 不确定 —— 反问，绝不写库
"""
from __future__ import annotations

from datetime import date

import pytest

from nanjingtrip.app import AppContext

D = date(2026, 11, 18)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def build_day(ctx):
    ctx.trip_svc.add_batch(D, [
        {'title': '游览中山陵', 'poi': '中山陵', 'duration_min': 90},
        {'title': '游览夫子庙', 'poi': '夫子庙', 'duration_min': 60},
    ], keep_order=True)


# ------------------------------------------------------------------ 路由
@pytest.mark.parametrize('text,kind', [
    ('明天上午九点去中山陵两小时，再去夫子庙一小时', 'preview'),
    ('中山陵多待了40分钟', 'confirm'),
    ('把夫子庙删掉', 'confirm'),
    ('把夫子庙这段改成步行', 'confirm'),
    ('我今天几点到夫子庙？', 'answer'),
    ('下午的安排合理吗', 'answer'),
    ('去那个地方吧', 'clarify'),
])
def test_route_kinds(ctx, text, kind):
    # 变更类必须先有当天安排，否则系统只能反问（这是正确行为）
    build_day(ctx)
    assert ctx.assistant_svc.route(text, D)['kind'] == kind


def test_route_answer_does_not_write(ctx):
    build_day(ctx)
    rev = ctx.db.revision()
    out = ctx.assistant_svc.route('我今天几点到夫子庙？', D)
    assert out['kind'] == 'answer'
    assert ctx.db.revision() == rev, '问答不应写库'


# ------------------------------------------------------- 变更：先确认再执行
def test_confirm_shows_effect_without_writing(ctx):
    """确认卡要带"执行后会怎样"，且此时**不落库**"""
    build_day(ctx)
    before = [(t.title, t.duration_min) for t in ctx.trip_svc.day_view(D).tasks]
    rev = ctx.db.revision()
    out = ctx.assistant_svc.route('中山陵多待了40分钟', D)
    assert out['kind'] == 'confirm'
    act = out['action']
    assert act['type'] == 'delay'
    assert '中山陵' in act['title'] and act['delay_min'] == 40
    eff = out['effect']
    assert eff['end_before'] and eff['end_after'], f'缺少前后对比：{eff}'
    # 未确认前不得改库
    after = [(t.title, t.duration_min) for t in ctx.trip_svc.day_view(D).tasks]
    assert before == after, '确认前就改了数据'
    assert ctx.db.revision() == rev


def test_apply_after_confirm_writes_and_keeps_undo(ctx):
    """用户点"就这样执行"后才落库，并给出可撤销的版本号"""
    build_day(ctx)
    out = ctx.assistant_svc.route('中山陵多待了40分钟', D)
    res = ctx.assistant_svc.apply(out['action'], D)
    assert res['ok'], res
    trip = ctx.trip_svc.day_view(D)
    zs = next(t for t in trip.tasks if '中山陵' in t.title)
    assert zs.duration_min == 130, f'延误未生效：{zs.duration_min}'
    assert res.get('undo_version'), '缺少撤销所需的"改动前"版本号'
    assert res.get('trip_id'), '缺少撤销所需的 trip_id'
    assert res.get('message')

    # 真的能撤销：回滚到 undo_version 后时长应回到 90
    back = ctx.trip_svc.rollback(int(res['trip_id']), int(res['undo_version']))
    assert back.result is not None, '回滚失败'
    zs2 = next(t for t in ctx.trip_svc.day_view(D).tasks if '中山陵' in t.title)
    assert zs2.duration_min == 90, f'撤销后未恢复：{zs2.duration_min}'


def test_confirm_offers_alternative_options(ctx):
    """塞不下时，确认卡应带上"选项式"备选（缩短/少去等）"""
    ctx.settings.day_start = '08:00'
    ctx.settings.day_end = '12:00'
    ctx.apply_settings(ctx.settings)
    ctx.trip_svc.set_lodging(D, '南京邮电大学三牌楼校区')
    ctx.trip_svc.add_batch(D, [
        {'title': '游览中山陵', 'poi': '中山陵', 'duration_min': 120},
        {'title': '游览牛首山', 'poi': '牛首山', 'duration_min': 120},
    ], keep_order=True)
    out = ctx.assistant_svc.route('中山陵多待了90分钟', D)
    assert out['kind'] == 'confirm'
    opts = out.get('options') or []
    assert opts, '冲突时应给出选项式备选'
    assert all('label' in o and 'action' in o for o in opts), opts


def test_clarify_when_target_unclear(ctx):
    """说得不清楚就反问：给候选而不是瞎改"""
    build_day(ctx)
    rev = ctx.db.revision()
    out = ctx.assistant_svc.route('把那个删掉', D)
    assert out['kind'] == 'clarify'
    assert out.get('message')
    assert ctx.db.revision() == rev


def test_delete_confirm_then_apply(ctx):
    build_day(ctx)
    out = ctx.assistant_svc.route('把夫子庙删掉', D)
    assert out['kind'] == 'confirm' and out['action']['type'] == 'delete'
    res = ctx.assistant_svc.apply(out['action'], D)
    assert res['ok'], res
    titles = [t.title for t in ctx.trip_svc.day_view(D).tasks]
    assert not any('夫子庙' in t for t in titles), titles
def test_ambiguous_segment_asks_which_one(ctx):
    """「这段改成步行」没说哪一段 → 反问并列出当天安排，绝不瞎改"""
    build_day(ctx)
    rev = ctx.db.revision()
    out = ctx.assistant_svc.route('这段改成步行', D)
    assert out['kind'] == 'clarify'
    assert '中山陵' in (out.get('message') or ''), out
    assert ctx.db.revision() == rev
