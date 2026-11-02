# -*- coding: utf-8 -*-
"""录入类意图（50 条）：用户只是在报地点。

与 `test_intent_matrix.py`（I-11~I-18）不重复：那边只覆盖了 8 句，这里把"报地点"
拆成七个口径——裸地名堆、带时长、带时刻、带先后、单选加点、含括号附注、含数量。

判定口径（只看 `slots.must` 这个可观察结果）：
  · `must` 是否包含用户说的那些地点（按关键词包含判定，容忍包内标准名差异）；
  · 地点数量是否正确（说了几个就该有几个，不多不少）；
  · 城市名/车站**不允许**被当地点（"南京""南京南站"不是可游玩的地方）；
  · 报时长时不能把时长数字读成日期（"1.5 小时" 不是 1 月 5 号）。

这批句子**故意不带日期**：真实用户第一句往往就是把地点丢过来。带日期时系统一定会
出草案，反而掩盖"地点到底有没有被录入"这个问题。
"""
import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

from datetime import date

TODAY = date(2026, 11, 1)

#: 城市名与车站：它们不是可游玩的地点
CITY_OR_STATION = ('南京', '南京市', '南京站', '南京南站', '南京禄口国际机场',
                   '禄口机场', '南京南')


@pytest.fixture(scope='module')
def ctx(tmp_path_factory):
    c = AppContext(tmp_path_factory.mktemp('entry-intent'))
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def must(out):
    return list((out.get('slots') or {}).get('must') or [])


def has_place(out, *keys):
    """每个关键词都要在 must 里找到一处包含它的地点名"""
    names = must(out)
    return all(any(k in m for m in names) for k in keys)


def count_is(out, n):
    return len(must(out)) == n


def no_spurious_date(out):
    """报的是时长/地名，不该凭空解析出一个日期"""
    return not (out.get('slots') or {}).get('start')


CASES = [
    # ---------- 裸地名堆：空格 / 顿号 / 箭头 / 逗号 / 换行 ----------
    ('E-01', '总统府 中山陵 明孝陵', lambda o: has_place(o, '总统府', '中山陵', '明孝陵')),
    ('E-02', '夫子庙、老门东、中华门', lambda o: has_place(o, '夫子庙', '老门东', '中华门')),
    ('E-03', '南京博物院→中山陵→夫子庙',
     lambda o: has_place(o, '南京博物院', '中山陵', '夫子庙')),
    ('E-04', '总统府，1912，新街口', lambda o: has_place(o, '总统府', '1912', '新街口')),
    ('E-05', '玄武湖\n鸡鸣寺\n台城', lambda o: has_place(o, '玄武湖', '鸡鸣寺', '台城')),
    ('E-06', '中山陵 明孝陵 灵谷寺', lambda o: has_place(o, '中山陵', '明孝陵', '灵谷寺')),
    ('E-07', '先锋书店、颐和路、南京眼',
     lambda o: has_place(o, '先锋书店', '颐和路', '南京眼')),
    ('E-08', '总统府→1912→夫子庙→老门东',
     lambda o: has_place(o, '总统府', '1912', '夫子庙', '老门东')),
    ('E-09', '南京 总统府 夫子庙', lambda o: has_place(o, '总统府', '夫子庙')),
    ('E-10', '南京南站 中山陵', lambda o: has_place(o, '中山陵')),
    # ---------- 带时长 ----------
    ('E-11', '南京博物院 1.5 小时',
     lambda o: has_place(o, '南京博物院') and no_spurious_date(o)),
    ('E-12', '总统府两小时', lambda o: has_place(o, '总统府')),
    ('E-13', '中山陵待三小时', lambda o: has_place(o, '中山陵')),
    ('E-14', '老门东半小时', lambda o: has_place(o, '老门东')),
    ('E-15', '明孝陵 40 分钟', lambda o: has_place(o, '明孝陵')),
    ('E-16', '夫子庙逛1.5小时',
     lambda o: has_place(o, '夫子庙') and no_spurious_date(o)),
    ('E-17', '博物院两小时，总统府一小时',
     lambda o: has_place(o, '博物院', '总统府')),
    ('E-18', '玄武湖 2 小时 鸡鸣寺 40 分钟',
     lambda o: has_place(o, '玄武湖', '鸡鸣寺')),
    ('E-19', '总统府1小时，夫子庙1.5小时，老门东半小时',
     lambda o: has_place(o, '总统府', '夫子庙', '老门东') and no_spurious_date(o)),
    ('E-20', '中山陵三小时，明孝陵两小时',
     lambda o: has_place(o, '中山陵', '明孝陵')),
    # ---------- 带时刻 ----------
    ('E-21', '九点去总统府', lambda o: has_place(o, '总统府')),
    ('E-22', '上午十点半到南京博物院', lambda o: has_place(o, '南京博物院')),
    ('E-23', '下午三点去夫子庙', lambda o: has_place(o, '夫子庙')),
    ('E-24', '明天八点出发去中山陵', lambda o: has_place(o, '中山陵')),
    ('E-25', '十点中山陵，十四点明孝陵', lambda o: has_place(o, '中山陵', '明孝陵')),
    ('E-26', '今天晚上七点去老门东', lambda o: has_place(o, '老门东')),
    ('E-27', '早上八点半去玄武湖', lambda o: has_place(o, '玄武湖')),
    ('E-28', '下午两点半去总统府', lambda o: has_place(o, '总统府')),
    # ---------- 带先后 ----------
    ('E-29', '先总统府后夫子庙', lambda o: has_place(o, '总统府', '夫子庙')),
    ('E-30', '先去中山陵然后去明孝陵', lambda o: has_place(o, '中山陵', '明孝陵')),
    ('E-31', '总统府 然后 老门东', lambda o: has_place(o, '总统府', '老门东')),
    ('E-32', '先去南京博物院，之后去总统府',
     lambda o: has_place(o, '南京博物院', '总统府')),
    ('E-33', '从总统府出发去夫子庙', lambda o: has_place(o, '总统府', '夫子庙')),
    ('E-34', '明孝陵完了去中山陵', lambda o: has_place(o, '明孝陵', '中山陵')),
    ('E-35', '总统府出来接着去1912', lambda o: has_place(o, '总统府', '1912')),
    ('E-36', '先玄武湖，再鸡鸣寺，最后台城',
     lambda o: has_place(o, '玄武湖', '鸡鸣寺', '台城')),
    # ---------- 单选加点：把 X 加到某天 ----------
    ('E-37', '把中山陵加到明天上午', lambda o: has_place(o, '中山陵')),
    ('E-38', '把老门东加到第二天下午', lambda o: has_place(o, '老门东')),
    ('E-39', '帮我把南京博物院加到行程里', lambda o: has_place(o, '南京博物院')),
    ('E-40', '把明孝陵加到11月14号下午', lambda o: has_place(o, '明孝陵')),
    # ---------- 含括号附注 ----------
    ('E-41', '夫子庙（夜景）', lambda o: has_place(o, '夫子庙')),
    ('E-42', '南京博物院（看展）', lambda o: has_place(o, '南京博物院')),
    ('E-43', '中山陵(早上)', lambda o: has_place(o, '中山陵')),
    ('E-44', '老门东（吃小吃）', lambda o: has_place(o, '老门东')),
    ('E-45', '总统府（1.5小时）',
     lambda o: has_place(o, '总统府') and no_spurious_date(o)),
    # ---------- 含数量：说了几个就该有几个 ----------
    ('E-46', '去三个地方：总统府、夫子庙、老门东', lambda o: count_is(o, 3)),
    ('E-47', '想去五个点：博物院、总统府、夫子庙、老门东、中山陵',
     lambda o: count_is(o, 5)),
    ('E-48', '三个地点帮我排：中山陵、明孝陵、中山植物园', lambda o: count_is(o, 3)),
    ('E-49', '今天去四个地方：中山陵、明孝陵、南京博物院、鸡鸣寺',
     lambda o: count_is(o, 4)),
    ('E-50', '一共去四个地方，中山陵 明孝陵 南京博物院 鸡鸣寺',
     lambda o: count_is(o, 4)),
]

#: 已确认与契约不符的用例 → 一句话根因（报告里按根因归类）
WHY = {
    'E-01': '裸地名堆未提取：只有"去/逛/参观"带出的片段才进 must',
    'E-02': '裸地名堆（顿号）未提取，must 为空',
    'E-03': '裸地名堆（箭头）未提取，must 为空',
    'E-04': '裸地名堆（逗号）未提取，must 为空',
    'E-05': '裸地名堆（换行）未提取，must 为空',
    'E-06': '裸地名堆未提取；另「灵谷寺」被解析成灵谷寺·钟灵街(地铁站)，被交通类过滤掉',
    'E-07': '裸地名堆未提取，must 为空',
    'E-08': '裸地名堆（箭头）未提取，must 为空',
    'E-09': '裸地名堆未提取，「南京」也无法与「南京博物院」区分',
    'E-10': '裸地名堆未提取，must 为空',
    'E-11': '「地点 + 1.5 小时」无动词，地点未提取；且 1.5 被读成 1 月 5 号，凭空生成过去日期',
    'E-12': '「地点 + 两小时」无动词，地点未提取',
    'E-13': '「地点 + 待三小时」的"待"不在动名词表，地点未提取',
    'E-14': '「地点 + 半小时」无动词，地点未提取',
    'E-15': '「地点 + 40 分钟」无动词，地点未提取',
    'E-16': '「逛」后接时长被整段吞掉，地点未提取；1.5 仍被读成 1 月 5 号',
    'E-17': '「地点 + 时长」并列句，两处地点都未提取',
    'E-18': '「地点 + 时长」空格分隔，两处地点都未提取',
    'E-19': '「地点 + 时长」三连，地点全未提取；1.5 又被读成 1 月 5 号',
    'E-20': '「地点 + 中文时长」并列，两处地点都未提取',
    'E-22': '「十点半到 X」的"到"不是游玩动词，地点未提取',
    'E-25': '「时刻 + 地点」开头无动词，两处地点都未提取',
    'E-29': '「先A后B」结构未识别，must 为空',
    'E-30': '「先A然后去B」只抓到带"去"的 B，A 丢失',
    'E-31': '「A 然后 B」结构未识别，must 为空',
    'E-34': '「A完了去B」只抓到带"去"的 B，A 丢失',
    'E-35': '「A出来接着去B」只抓到带"去"的 B，A 丢失',
    'E-36': '「先A，再B，最后C」结构未识别，must 为空',
    'E-37': '「把 X 加到…」不在追加动词表（只认"加个/加上/再加"），地点未进 must',
    'E-38': '「把 X 加到…」未被识别为追加，地点未进 must',
    'E-39': '「把 X 加到行程里」未被识别为追加，地点未进 must',
    'E-40': '「把 X 加到某日」未被识别为追加，地点未进 must',
    'E-41': '「地名（夜景）」无动词，地点未提取',
    'E-42': '「地名（看展）」无动词，地点未提取（只把"看展"记成了兴趣）',
    'E-43': '「地名(早上)」无动词，地点未提取',
    'E-44': '「地名（吃小吃）」无动词，地点未提取（只把"吃"记成了兴趣）',
    'E-45': '「地名（1.5小时）」无动词，地点未提取；1.5 仍被读成 1 月 5 号',
    'E-46': '「去三个地方：A、B、C」数量词与首个地点粘连，A 解析失败只录到 2 个',
    'E-47': '「想去五个点：…」数量词与首个地点粘连，且 must 有 5 个上限',
    'E-48': '「三个地点帮我排：A、B、C」无"去/逛"动词，一个都没录到',
    'E-49': '「今天去四个地方：A、B、C、D」数量词与首个地点粘连，只录到 1 个',
    'E-50': '「一共去四个地方，A B C D」只录到 1 个地点',
}

PARAMS = [
    pytest.param(code, text, check, id=code,
                 marks=pytest.mark.xfail(reason=WHY[code], strict=False)
                 if code in WHY else ())
    for code, text, check in CASES
]


@pytest.mark.parametrize('code,text,check', PARAMS)
def test_entry_intent(ctx, code, text, check):
    session = f'entry-{code}'
    out = run(ctx, text, session)
    assert isinstance(out, dict), f'{code} 返回结构不对'
    assert check(out), (
        f'{code} 「{text}」→ kind={out.get("kind")} must={must(out)} '
        f'start={(out.get("slots") or {}).get("start")!r}')
    assert not [m for m in must(out) if m in CITY_OR_STATION or m.endswith('站')], (
        f'{code} 「{text}」把城市名/车站当成了地点：{must(out)}')
