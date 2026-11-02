"""批量写入：类别跟着地点走（D-58 补充）

"待补地点"的条目在预览卡里补了地名后，类别不该一律是"其他"——
医院该是医疗、饭馆该是餐饮，否则右栏的按类统计与餐食提示都会看漏。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext


DAY = date(2026, 11, 9)


@pytest.fixture()
def ctx(tmp_path):
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def test_add_batch_infers_category_from_poi(ctx):
    ok, msg = ctx.trip_svc.add_batch(DAY, [
        {'kind': 'todo', 'title': '医院拿报告', 'poi': '南京鼓楼医院',
         'duration_min': 60, 'category': ''},
        {'kind': 'task', 'title': '看展', 'poi': '南京博物院',
         'duration_min': 120, 'category': ''},
    ], keep_order=True)
    assert ok, msg
    cats = {t.title: t.category for t in ctx.trip_svc.day_view(DAY).tasks}
    assert cats['医院拿报告'] == '医疗'
    assert cats['看展'] == '展览'


def test_add_batch_keeps_explicit_category(ctx):
    ok, _ = ctx.trip_svc.add_batch(DAY, [
        {'kind': 'task', 'title': '开会', 'poi': '南京博物院',
         'duration_min': 60, 'category': '办事'},
    ], keep_order=True)
    assert ok
    assert ctx.trip_svc.day_view(DAY).tasks[0].category == '办事'
