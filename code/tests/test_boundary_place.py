# -*- coding: utf-8 -*-
"""边界测试 C 层：地点解析失败路径。

断言依据是契约（`code/docs/边界测试计划.md` C 层 + AGENTS.md §3「取不到就留缺口」），
不是当前实现的行为：

  · 「三山街地铁站」「夫子庙（景区）」这类带后缀/括号的说法要能解析出对应地点；
  · 跨城地标（故宫博物院 / 上海外滩 / 西湖）必须拒绝，落到「待补地点」，
    不得被悄悄换成本地景点；
  · 本地同名不能误伤：「明故宫遗址公园」必须正常解析；
  · 认不出的地名 → 批量预览给 needs_poi=True 的待补项，不臆造、不抛异常；
  · 地址串（中山路321号）离线时按待补处理（离线不做地理编码，在线才走）；
  · 空名 / 超长名 / 纯标点不崩。

`xfail(strict=False)` 的用例是与上面的契约不符、疑似缺陷的地方：
只标出来 + 写进 `code/docs/边界测试结果_CD层.md`，**不改实现**。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext

DAY = date(2026, 11, 10)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def items_of(ctx, text):
    """批量预览解析结果（不落库）；只取本层关心的叶子字段"""
    out = ctx.trip_svc.preview_batch(text, DAY)
    return out, list(out.get('items') or [])


# ------------------------------------------------------------------ C-01
def test_c01_subway_suffix_is_resolved(ctx):
    """C-01 「三山街地铁站」要解析出站名，而不是当成认不出的地名"""
    _out, items = items_of(ctx, '三山街地铁站')
    assert items, '至少要产出一条待确认项，不能静默丢弃'
    first = items[0]
    assert '三山街' in (first.get('poi') or ''), f'应解析到三山街站，实际：{first}'
    assert not first.get('needs_poi'), f'解析出来了就不该再标待补：{first}'


# ------------------------------------------------------------------ C-02
def test_c02_paren_suffix_is_resolved(ctx):
    """C-02 「夫子庙（景区）」括号是附注，主体地点要认出来"""
    _out, items = items_of(ctx, '夫子庙（景区）')
    assert items, '至少要产出一条待确认项'
    first = items[0]
    assert '夫子庙' in (first.get('poi') or ''), f'应解析到夫子庙，实际：{first}'
    assert not first.get('needs_poi'), f'解析出来了就不该再标待补：{first}'


# ------------------------------------------------------------------ C-03
@pytest.mark.xfail(reason='疑似缺陷：别名「博物院」被裸子串命中，'
                          '「故宫博物院」被解析成「南京博物院」（跨城误配）', strict=False)
def test_c03_cross_city_palace_museum_is_rejected(ctx):
    """C-03 「故宫博物院」是北京的地标：不得被换成本地博物馆"""
    _out, items = items_of(ctx, '故宫博物院')
    assert items, '至少要产出一条待确认项'
    first = items[0]
    assert not (first.get('poi') or ''), f'不得臆造本地地点，实际：{first}'
    assert first.get('needs_poi'), f'认不出就该是待补项：{first}'


# ------------------------------------------------------------------ C-04
@pytest.mark.parametrize('text', ['上海外滩', '西湖'])
def test_c04_other_cross_city_places_are_pending(ctx, text):
    """C-04 上海外滩 / 西湖：跨城地点一律进待补，不猜、不错配"""
    _out, items = items_of(ctx, text)
    assert items, f'{text} 不能被静默丢掉'
    first = items[0]
    assert not (first.get('poi') or ''), f'{text} 不得被凑成本地地点，实际：{first}'
    assert first.get('needs_poi'), f'{text} 应给 needs_poi 待补项：{first}'


# ------------------------------------------------------------------ C-05
def test_c05_local_lookalike_still_resolves(ctx):
    """C-05 「明故宫遗址公园」是南京本地点：不能被跨城防护误伤"""
    _out, items = items_of(ctx, '明故宫遗址公园')
    first = items[0]
    assert '明故宫' in (first.get('poi') or ''), f'本地同名地点必须解析，实际：{first}'
    assert not first.get('needs_poi'), f'解析出来了就不该是待补项：{first}'


# ------------------------------------------------------------------ C-06
def test_c06_unknown_place_is_pending_not_invented(ctx):
    """C-06 认不出的地名：预览给待补项 + 原因，写入时明确失败，全程不抛异常"""
    out, items = items_of(ctx, '这个地点完全不存在啊啊啊')      # 不许抛异常
    assert items, '认不出的地名也要有下落（待补项），不能静默消失'
    first = items[0]
    assert not (first.get('poi') or ''), f'不得臆造地点，实际：{first}'
    assert first.get('needs_poi') is True
    assert first.get('reason'), '待补项要写清缺什么'
    assert out.get('unparsed') == [], '已经有了待补项，就不该再算作"未解析"丢给用户'
    # 写入侧同样不能糊过去
    ok, msg = ctx.trip_svc.add_batch(
        DAY, [{'poi': '这个地点完全不存在啊啊啊', 'title': 'x', 'duration_min': 60}],
        keep_order=True)
    assert ok is False and '找不到地点' in msg, f'认不出的地名不能算写入成功：{msg}'


# ------------------------------------------------------------------ C-07
def test_c07_address_string_is_pending_offline(ctx):
    """C-07 地址串离线按待补处理：离线不做地理编码，不把门牌号当景点"""
    _out, items = items_of(ctx, '中山路321号')
    assert items, '地址串也要有下落，不能静默丢掉'
    first = items[0]
    assert not (first.get('poi') or ''), f'离线不该把地址猜成某个地点，实际：{first}'
    assert first.get('needs_poi') is True


# ------------------------------------------------------------------ C-08
@pytest.mark.parametrize('text', ['', '   ', '，。！？', '南京' * 40])
def test_c08_degenerate_input_does_not_crash(ctx, text):
    """C-08 空名 / 纯标点 / 超长名：不崩，且不产生臆造地点"""
    out = ctx.trip_svc.preview_batch(text, DAY)              # 不许抛异常
    assert isinstance(out, dict)
    for item in out.get('items') or []:
        if item.get('needs_poi'):
            assert not (item.get('poi') or ''), f'待补项不得自带地点：{item}'


# ------------------------------------------------------------------ C-09
@pytest.mark.xfail(reason='疑似缺陷：「夫子庙（景区）」的预览标题只剩括号残渣「（景区）」',
                   strict=False)
def test_c09_title_is_not_leftover_parens(ctx):
    """C-09 括号说法解析成功后，预览卡标题不能只剩「（景区）」这种残渣"""
    _out, items = items_of(ctx, '夫子庙（景区）')
    title = str(items[0].get('title') or '')
    assert title.strip('（）() '), f'标题不能只有括号内容，实际：{title!r}'
    assert '夫子庙' in title, f'标题要能看出是哪个地方，实际：{title!r}'
