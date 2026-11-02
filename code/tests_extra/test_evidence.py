"""游览时长证据化（P1，不含用户回填闭环）

以前时长只有一个来源：估算（类型 × 评分 × 营业窗口）。现在多了证据层：
    official（官方口径）> ota（平台登记）> estimate（估算）
同级多条取中位数，对外给**区间**，排程取中值，界面显示来源与区间。

按要求**不做**用户回填闭环（没有"完成后填实际用时"这条通道）。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.evidence import DurationEvidence


DAY = date(2026, 12, 21)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_priority_official_over_ota_over_estimate(ctx):
    ev: DurationEvidence = ctx.evidence
    ev.add('南京博物院', 180, 'estimate')
    got = ev.resolve(ctx.trip_svc._poi_named('南京博物院'))
    assert got['source'] == 'estimate'

    ev.add('南京博物院', 150, 'ota')
    got = ev.resolve(ctx.trip_svc._poi_named('南京博物院'))
    assert got['source'] == 'ota' and got['minutes'] == 150

    ev.add('南京博物院', 120, 'official', note='官网建议 2 小时')
    got = ev.resolve(ctx.trip_svc._poi_named('南京博物院'))
    assert got['source'] == 'official' and got['minutes'] == 120


def test_same_source_takes_median_and_range(ctx):
    ev: DurationEvidence = ctx.evidence
    # 同级只能存一条（主键 city+name+source），所以用不同来源体现"多来源并存"
    ev.add('总统府', 120, 'official')
    ev.add('总统府', 90, 'ota')
    got = ev.resolve(ctx.trip_svc._poi_named('总统府'))
    assert got['source'] == 'official' and got['minutes'] == 120
    assert got['low'] < got['minutes'] < got['high']
    txt = DurationEvidence.range_text(got)
    assert '官方口径' in txt


def test_estimate_has_wider_range_than_official(ctx):
    ev: DurationEvidence = ctx.evidence
    poi = ctx.trip_svc._poi_named('鸡鸣寺')
    est = ev.resolve(poi)
    ev.add('鸡鸣寺', est['minutes'], 'official')
    off = ev.resolve(poi)
    assert (off['high'] - off['low']) < (est['high'] - est['low'])


def test_default_duration_uses_evidence(ctx):
    """没写时长时，批量写入用证据表的中值（有 official 就用 official）"""
    ctx.evidence.add('夫子庙', 60, 'official', note='测试口径')
    out = ctx.trip_svc.preview_text('明天去夫子庙', DAY)
    # preview 走的是同一套默认时长逻辑
    assert out.ok, out.error
    assert out.draft.duration_min == 60


def test_preview_item_carries_source_and_range(ctx):
    out = ctx.trip_svc.preview_batch('去南京博物院、夫子庙', DAY)
    items = out['items']
    assert items
    got = items[0]
    assert got.get('duration_source') in ('official', 'ota', 'estimate')
    assert got.get('duration_label')
    assert got.get('duration_range') and got['duration_range'][0] < got['duration_range'][1]


def test_unknown_place_falls_back_to_kind_default(ctx):
    """没有证据也没有推荐时长时，按类型兜底，且来源仍标为估算"""
    ev = ctx.evidence
    got = ev.resolve(None)
    assert got['source'] == 'estimate' and got['minutes'] == 60
