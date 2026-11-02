"""长段落批量解析：不许静默丢句（D-58 回归）

背景：用户"把一整段话贴进起始页"，结果只排出一两项，剩下的整句消失——
既没进 items，也没进 unparsed，界面上完全看不出来。三类根因：
  1. 片段缺时长 → preview_text 直接 ok=False，被丢进 unparsed 只在小字里提一句；
  2. "中午跟同事吃饭" 这种无地点餐饮块，同样因为缺时长被丢掉，而不是当默认 60 分钟；
  3. 整段没有标点分隔时，split_batch 只切出 1 段，后面的地点全被吞掉。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.parsing_rules import split_batch, split_by_pois


DAY = date(2026, 11, 2)


@pytest.fixture()
def svc(tmp_path):
    ctx = AppContext(tmp_path)
    ctx.settings.offline_mode = True
    ctx.apply_settings(ctx.settings)
    yield ctx.trip_svc
    ctx.close()


# ---------------- 切分：无标点长句按已知地点名兜底 ----------------

def test_split_by_pois_cuts_punctuation_free_paragraph():
    names = ['南京博物院', '新街口', '先锋书店五台山店', '老门东']
    text = ('我想明天上午去南京博物院看展两个小时中午在新街口吃个饭'
            '下午三点去先锋书店五台山店待一个半小时晚上去老门东逛逛')
    segs = split_by_pois(text, names)
    assert len(segs) >= 4, segs
    assert segs[0].startswith('我想明天上午') or '南京博物院' in segs[0]


def test_split_by_pois_needs_two_places():
    assert split_by_pois('去南京博物院看展', ['南京博物院']) == []


def test_split_batch_unchanged_on_delimiters():
    segs = split_batch('依次去：玄武湖、鸡鸣寺、老门东')
    assert len(segs) == 3


# ---------------- 批量预览：不静默丢句 ----------------

def test_punctuation_free_paragraph_yields_multiple_items(svc):
    text = ('我想明天上午去南京博物院看展两个小时中午在新街口吃个饭'
            '下午三点去先锋书店待一个半小时晚上去老门东逛逛')
    out = svc.preview_batch(text, DAY)
    assert len(out['items']) >= 3, out['items']
    names = {i.get('poi') or '' for i in out['items']}
    assert any('博物院' in n for n in names)


def test_connector_paragraph_keeps_every_sentence(svc):
    """「先…然后…中午…下午…」：每句都要有下落，不能只剩一项"""
    text = '明天先去医院拿报告，然后去公司开会两小时，中午跟同事吃饭，下午去图书馆还书。'
    out = svc.preview_batch(text, DAY)
    items = out['items']
    assert len(items) >= 3, items
    # 没认出的地点要变成"待补地点"的条目，而不是消失
    todo = [i for i in items if i.get('needs_poi')]
    assert todo, items
    for t in todo:
        assert t.get('reason'), t
        assert t.get('segment'), t


def test_food_segment_without_place_becomes_block(svc):
    """"中午跟同事吃饭" 没写时长也要按无地点餐饮块保住（默认 60 分钟）"""
    out = svc.preview_batch('中午跟同事吃饭', DAY)
    items = out['items']
    assert len(items) == 1, items
    it = items[0]
    assert it['kind'] == 'block'
    assert it['category'] == '餐饮'
    assert it['poi'] == ''
    assert it['duration_min'] == 60


def test_instruction_tail_is_dropped_not_turned_into_item(svc):
    """纯指令尾巴（"帮我规划全天最优路线"）不该变成一条待办"""
    text = '上午去玄武湖，下午去鸡鸣寺。帮我规划全天最优路线并生成行程表。'
    out = svc.preview_batch(text, DAY)
    segs = ' '.join(i.get('segment') or '' for i in out['items'])
    assert '帮我规划' not in segs, out['items']
    assert len(out['items']) == 2, out['items']


def test_long_paragraph_nothing_silently_lost(svc):
    """总约束：段落里提到的每个已知地点都要么成项、要么成待补项"""
    text = ('明天上午先去玄武湖，然后去鸡鸣寺，中午吃饭，下午去先锋书店，'
            '顺路去医院拿报告，晚上去老门东。')
    out = svc.preview_batch(text, DAY)
    items = out['items']
    assert len(items) >= 5, items
    assert not out['unparsed'], out['unparsed']
    joined = ' '.join((i.get('poi') or '') + (i.get('segment') or '') for i in items)
    for kw in ('玄武湖', '鸡鸣寺', '先锋书店', '老门东', '医院'):
        assert kw in joined, (kw, items)


def test_salvaged_title_keeps_place_name_intact(svc):
    """兜底标题不能把地点名切坏（'先锋书店' 去掉开头的'先'就成了'锋书店'）"""
    out = svc.preview_batch('下午去先锋书店', DAY)
    it = out['items'][0]
    assert it['poi'] == '先锋书店五台山店'
    assert it['title'] != '锋书店'
    assert '先' in it['title'] or it['title'] == it['poi']


def test_known_place_without_duration_is_kept(svc):
    """认得出地点、只是没写时长：当正常事项排（60 分钟），不要变成待补"""
    out = svc.preview_batch('晚上去老门东', DAY)
    it = out['items'][0]
    assert it['poi'] == '老门东'
    assert not it.get('needs_poi')
    # 老门东在城市包里的推荐游玩时长是 90 分钟，缺时长时应当用它
    assert it['duration_min'] == 90, it['duration_min']


def test_todo_title_is_clean(svc):
    """待补条目的标题别带着"我先""，也别留空格"""
    out = svc.preview_batch('我明天先去医院拿报告，下午去图书馆还书', DAY)
    todos = [i for i in out['items'] if i.get('needs_poi')]
    assert todos, out['items']
    for it in todos:
        assert not it['title'].startswith('我先'), it['title']
        assert ' ' not in it['title'].strip(), it['title']


def test_segment_with_many_places_is_split_further(svc):
    """片段内部挤着多个地点时要再切：否则只解析第一处，其余**连未解析都不出现**

    实测原文（L7 型）：「…先锋书店（40分钟），傍晚回玄武湖散步1小时，
    晚上去鸡鸣寺（40分钟）和台城（30分钟），最后回三牌楼校区。」
    """
    text = ('上午九点先到总统府参观1小时，然后去南京博物院看展2小时，'
            '下午依次去夫子庙（1小时20分钟）、老门东（1小时）、先锋书店（40分钟），'
            '傍晚回玄武湖散步1小时，晚上去鸡鸣寺（40分钟）和台城（30分钟）。')
    out = svc.preview_batch(text, DAY)
    places = [i.get('poi') or '' for i in out['items']]
    for kw in ('先锋书店', '玄武湖', '鸡鸣寺', '台城', '总统府', '博物院', '夫子庙', '老门东'):
        assert any(kw in p for p in places), (kw, places)
    assert out['split_by'] in ('poi', 'rules+poi'), out['split_by']


def test_homecoming_to_origin_is_not_an_item(svc):
    """「最后回南邮三牌楼校区」是收尾说明，不该变成一条待办"""
    text = ('我从南京邮电大学三牌楼校区出发，上午去总统府1小时，'
            '下午去夫子庙1小时，最后回三牌楼校区。')
    out = svc.preview_batch(text, DAY)
    assert out['origin'] == '南京邮电大学三牌楼校区'
    titles = ' '.join((i.get('poi') or '') + (i.get('title') or '') for i in out['items'])
    assert '三牌楼' not in titles, out['items']
    assert len(out['items']) == 2, out['items']


def test_note_and_preference_sentences_are_not_items(svc):
    """预约/闭馆/偏好/重复提及这类附注不是一件事"""
    text = ('上午去南京博物院看展2小时，记得博物院周一闭馆、要实名预约；'
            '下午去玄武湖散步1小时，晚上去老门东1小时；'
            '另外重复一句：玄武湖和夫子庙我都想再去一次。全程尽量少走路，能骑车就骑车。')
    out = svc.preview_batch(text, DAY)
    titles = ' '.join(i.get('title') or '' for i in out['items'])
    for bad in ('实名预约', '尽量', '骑车', '重复'):
        assert bad not in titles, (bad, titles)
    assert len(out['items']) == 3, out['items']
