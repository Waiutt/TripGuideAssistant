# -*- coding: utf-8 -*-
"""生成进度（B'：主请求不动、另发只读请求轮询）的契约测试。

口径：
  · 进度只报"阶段名 + 粗粒度百分比"，不伪装精确进度；
  · **结束时必须清空**（前端不会停在 90% 转圈）；
  · 生成抛错也要清空；
  · 没在生成的会话查到空进度，不能报错。
"""
from __future__ import annotations

import datetime

import pytest

from nanjingtrip import consult as consult_mod
from nanjingtrip.app import AppContext

TODAY = datetime.date(2026, 11, 2)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_progress_is_cleared_after_chat(ctx):
    """生成结束（无论成败）进度都要清空——保证进度条有终点"""
    ctx.consult.chat("p1", "明天去总统府2小时", TODAY)
    assert consult_mod.get_progress("p1") == {}


def test_progress_cleared_even_on_error(ctx, monkeypatch):
    """中途抛错也要清空（否则前端永远停在那个阶段）"""
    def boom(*a, **k):
        raise RuntimeError("故意失败")
    monkeypatch.setattr(consult_mod.Consultant, "build_plan", boom)
    with pytest.raises(RuntimeError):
        ctx.consult.chat("p2", "明天去总统府2小时", TODAY)
    assert consult_mod.get_progress("p2") == {}


def test_unknown_session_returns_empty():
    """没在生成的会话：返回空 dict，不抛异常"""
    assert consult_mod.get_progress("never-started") == {}


def test_stage_names_are_human_readable():
    """阶段名要能给人看（前端直接显示），不能是内部代号"""
    for stage, pct in (consult_mod.STAGE_START, consult_mod.STAGE_PICK,
                       consult_mod.STAGE_FETCH, consult_mod.STAGE_RENDER):
        assert isinstance(stage, str) and len(stage) >= 4
        assert 0 < pct < 100


def test_progress_visible_during_generation(ctx, monkeypatch):
    """生成过程中能查到阶段（模拟：在 build_plan 里读一次进度）"""
    seen = {}

    def spy(self, st):
        seen.update(consult_mod.get_progress("p3"))
        return orig(self, st)

    orig = consult_mod.Consultant.build_plan
    monkeypatch.setattr(consult_mod.Consultant, "build_plan", spy)
    ctx.consult.chat("p3", "明天去总统府2小时", TODAY)
    assert seen.get("stage"), seen
    assert 0 < seen.get("pct", 0) < 100
