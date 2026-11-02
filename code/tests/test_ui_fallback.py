# -*- coding: utf-8 -*-
"""界面兜底的服务端契约：会话重置（对应报告 4.10 补充）。"""
import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.web_api import command_payload


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_consult_reset_clears_session(ctx):
    """「重新开始」要真的把旧槽位删掉，否则陈年偏好还会继续生效"""
    first = command_payload(ctx, {'action': 'consult', 'session': 'ui-1',
                                  'text': '明天去南京博物院，酒店你帮我选',
                                  'date': '2026-11-01'})
    assert first['kind'] == 'plan'
    rows = ctx.db.conn.execute('SELECT COUNT(*) AS c FROM consult_session').fetchone()['c']
    assert rows == 1
    out = command_payload(ctx, {'action': 'consult_reset', 'session': 'ui-1'})
    assert out['ok'] is True
    rows = ctx.db.conn.execute('SELECT COUNT(*) AS c FROM consult_session').fetchone()['c']
    assert rows == 0
    # 重开之后不再带着"已定住宿"
    again = command_payload(ctx, {'action': 'consult', 'session': 'ui-1',
                                 'text': '明天去南京博物院', 'date': '2026-11-01'})
    assert again['slots']['hotel'] == ''


def test_consult_reset_without_session_is_harmless(ctx):
    out = command_payload(ctx, {'action': 'consult_reset', 'session': ''})
    assert out['ok'] is True


def test_db_stats_and_maintain_actions(ctx):
    stats = command_payload(ctx, {'action': 'db_stats'})
    assert stats['ok'] and 'transit_leg' in stats and 'wal_bytes' in stats
    out = command_payload(ctx, {'action': 'db_maintain'})
    assert out['ok'] and '数据维护完成' in out['message']
