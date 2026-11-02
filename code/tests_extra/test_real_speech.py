"""真实口语里的三个坑（D-61/D-62/D-63 回归）

都是"拿真实游客的话"模拟时暴露的，不是构造出来的边角：
  · 括号里的附注（"（这个必须去，我已经预约了）"）会把地点名挤丢；
  · "我周六早上到南京站"把车站排成第一个景点，还占掉 1 小时；
  · "晚上还要赶 21:00 的高铁"这条死线没人管，排到 22:00 也不吭声。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.parsing_rules import strip_note_parens


DAY = date(2026, 12, 11)


@pytest.fixture()
def svc(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    yield ctx.trip_svc
    ctx.close()


def test_note_parens_stripped_but_duration_parens_kept():
    assert strip_note_parens('下午总统府（这个必须去，我已经预约了）') == '下午总统府'
    assert strip_note_parens('鸡鸣寺（看情况，人多就算了）') == '鸡鸣寺'
    # 括号里是时长：不能剥
    assert '40分钟' in strip_note_parens('新街口（停留40分钟）')


def test_place_survives_parenthetical_note(svc):
    text = '下午总统府（这个必须去，我已经预约了），傍晚去夫子庙1小时'
    out = svc.preview_batch(text, DAY)
    pois = [i.get('poi') or '' for i in out['items']]
    assert any('总统府' in p for p in pois), out['items']
    assert any('夫子庙' in p for p in pois), out['items']


def test_arrival_station_becomes_origin_not_first_stop(svc):
    text = '我周六早上到南京站，先去玄武湖1小时，然后去总统府1小时'
    out = svc.preview_batch(text, DAY)
    assert out['origin'] == '南京站', out['origin']
    pois = [i.get('poi') or '' for i in out['items']]
    assert not any('南京站' in p for p in pois), pois
    assert any('玄武湖' in p for p in pois) and any('总统府' in p for p in pois)


def test_return_deadline_parsed_from_train(svc):
    out = svc.preview_batch('晚上还要赶 21:00 的高铁回上海', DAY)
    assert out['return_by'] == '21:00'


def test_return_deadline_written_to_settings(svc, tmp_path):
    out = svc.preview_batch('去玄武湖1小时，晚上赶20:30的火车', DAY)
    svc.add_batch(DAY, [{'kind': 'task', 'title': '玄武湖', 'poi': '玄武湖公园',
                         'duration_min': 60, 'category': '其他'}],
                  return_by=out['return_by'], keep_order=True)
    assert svc.settings.return_by == '20:30'


def test_vague_reference_is_not_guessed(svc):
    """"那个什么寺"不许被猜成某个具体地点（曾经被解析成电影院）"""
    out = svc.preview_batch('栖霞山、那个什么寺、总统府', DAY)
    items = out['items']
    todo = [i for i in items if i.get('needs_poi')]
    assert todo, items
    assert any('含糊' in (i.get('reason') or '') for i in todo), todo
    # 不该冒出与"寺"无关的臆造地点
    names = ' '.join((i.get('poi') or '') for i in items)
    assert '戏院' not in names and '影城' not in names, names

def test_vague_fragment_not_resolved_as_any_place(svc):
    """只有"动宾短语"没有地名的片段，不许被高德模糊匹配成某个具体地点"""
    from nanjingtrip.services import TripService
    assert not TripService._name_alike(svc, '能看多久看多久', '辰龙广场')
    assert TripService._name_alike(svc, '栖霞山', '栖霞山风景名胜区')
    assert TripService._name_alike(svc, '红山动物园', '红山森林动物园')

def test_return_deadline_tolerates_extra_words(svc):
    """"晚上 21:00 还要赶高铁" 里时刻与"高铁"之间有别的词，也要认出来"""
    assert svc.preview_batch('晚上 21:00 还要赶高铁回上海', DAY)['return_by'] == '21:00'
    assert svc.preview_batch('晚上还要赶 21:00 的高铁', DAY)['return_by'] == '21:00'
    assert svc.preview_batch('下午 18:00 的飞机', DAY)['return_by'] == '18:00'
