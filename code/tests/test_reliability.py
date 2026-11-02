# -*- coding: utf-8 -*-
"""可靠性测试：侧效应、输入守卫、坏数据容错（对应报告 4.10 的补充）。

这类用例不测"功能对不对"，而测"系统在异常与边界上会不会做出越界的事"：
改一次行程不该改全局配置、超长输入不该把服务拖死、坏会话不该让顾问崩溃。
"""
import json
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import MAX_TEXT_LEN, Consultant, ConsultStore

TODAY = date(2026, 10, 31)
TEXT = ('11月14号到11月16号在南京玩，很懒，10点起床，晚上23点后回酒店，'
        '想去南京博物院、夫子庙、总统府，中午在德基广场吃饭')


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.settings.day_start = '08:00'
    c.settings.day_end = '22:00'
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_apply_proposal_does_not_rewrite_global_settings(ctx):
    """确认一份草案不该偷偷改全局默认日界（草案里的 10:00/23:00 只作用于这次写入）"""
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('s1', TEXT, TODAY)['plan']
    assert plan['wake'] == '10:00' and plan['day_end'] == '23:00'
    before = (ctx.settings.day_start, ctx.settings.day_end)
    ok, _msg = ctx.trip_svc.apply_proposal(plan)
    assert ok
    assert (ctx.settings.day_start, ctx.settings.day_end) == before


def test_apply_proposal_still_starts_the_day_late(ctx):
    """侧效应去掉后，行为必须保持：当天确实从 10:00 之后开始排"""
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    plan = con.chat('s1', TEXT, TODAY)['plan']
    assert ctx.trip_svc.apply_proposal(plan)[0]
    day = date.fromisoformat(plan['days'][0]['date'])
    trip = ctx.trip_svc.day_view(day)
    starts = [t.start_ts for t in trip.tasks if t.start_ts]
    assert starts, '应当排进了事项'
    assert min(starts).strftime('%H:%M') >= '10:00'


def test_add_batch_does_not_resave_settings_when_unchanged(ctx, tmp_path):
    """同一组参数反复写入不该反复改写配置文件"""
    cfg = tmp_path / 'config.json'
    items = [{'poi': '南京博物院', 'title': '游览南京博物院', 'duration_min': 120}]
    assert ctx.trip_svc.add_batch(date(2026, 11, 2), items, keep_order=True)[0]
    after_first = cfg.read_bytes() if cfg.exists() else b''
    assert ctx.trip_svc.add_batch(date(2026, 11, 3), items, keep_order=True)[0]
    after_second = cfg.read_bytes() if cfg.exists() else b''
    assert after_first == after_second


def test_overlong_text_is_refused_with_advice(ctx):
    """超长输入要立刻拒掉并给建议，而不是把解析链路拖住"""
    long_text = '明天去南京博物院看展2小时，' * 200
    out = Consultant(ctx.trip_svc, ConsultStore(ctx.db)).chat('long', long_text, TODAY)
    assert out['kind'] == 'notice'
    assert str(MAX_TEXT_LEN) in out['message']
    batch = ctx.trip_svc.preview_batch(long_text, date(2026, 11, 1))
    assert batch['ok'] is False and '太长' in (batch.get('message') or '')


def test_corrupted_session_payload_does_not_crash(ctx):
    """会话表里存了坏数据，顾问要能当新会话继续，而不是抛异常"""
    ctx.db.conn.execute(
        'INSERT INTO consult_session(session, payload, updated_at) VALUES(?,?,?)',
        ('broken', 'this is not json', ''))
    ctx.db.conn.commit()
    out = Consultant(ctx.trip_svc, ConsultStore(ctx.db)).chat('broken', TEXT, TODAY)
    assert out.get('plan') and out['plan']['days']


def test_unknown_slots_in_payload_are_ignored(ctx):
    """老版本/未来版本的槽位混进来，不该影响当前解析"""
    payload = json.dumps({'v': 2, 'start': '2026-11-14', 'end': '2026-11-16',
                          'unknown_future_slot': {'a': 1}, 'hotel': 'recommend'})
    ctx.db.conn.execute(
        'INSERT INTO consult_session(session, payload, updated_at) VALUES(?,?,?)',
        ('future', payload, ''))
    ctx.db.conn.commit()
    out = Consultant(ctx.trip_svc, ConsultStore(ctx.db)).chat('future', '就这样', TODAY)
    assert out.get('plan'), out
    assert out['plan']['hotel_pick'], '老会话里已定的住宿仍要生效'
