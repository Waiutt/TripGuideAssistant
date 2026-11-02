# -*- coding: utf-8 -*-
"""删除/排除类意图矩阵（50 轮）：是"别再给我排这个"，还是"把已经排的删掉"。

两条口径必须分开，混在一起就是数据事故
--------------------------------------
* **软排除**（别去X / 不想去X / X我不想去了）只影响**新草案**：写进 `slots.avoid`，
  已经写进服务端的那几项**一个都不能少**（用户只是说"别再加了"，不是"删了"）。
* **硬删除**（把X去掉/删掉/拿掉、X不要了）必须**真的动数据**：
  `ctx.trip_svc.day_view(day).tasks` 少一条，名字真的消失，回复里点名删了谁。

为什么每条用例都先建一份"真行程"
--------------------------------
只跑 `chat()` 得到的是**草案**，不写库；那样测"删掉没有"永远测不到。
所以先跑 `BASE` 把会话槽位铺好（11-14~11-16），再用 `add_batch` 把三个点
**真正写进临时库**的 11-14，然后才发待测的那一句——否则测到的是
"没有东西可删"。写库只发生在 `tmp_path` 的临时库里。

判定细节见 `code/docs/意图测试结果_修改删除.md`。
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)
#: 底稿会话：三天、三个点名必去的点
BASE = '11月14号到16号去南京玩，想去南京博物院、总统府和夫子庙，每天两个点'
#: 底稿真正落库的那一天（BASE 的起始日）
DAY = date(2026, 11, 14)
BASE_TASKS = [
    {'poi': '南京博物院', 'title': '游览南京博物院', 'duration_min': 150,
     'kind': 'task', 'category': '展览'},
    {'poi': '总统府', 'title': '游览总统府', 'duration_min': 120,
     'kind': 'task', 'category': '展览'},
    {'poi': '夫子庙', 'title': '游览夫子庙', 'duration_min': 120,
     'kind': 'task', 'category': '展览'},
]


@pytest.fixture
def ctx(tmp_path):
    """每个用例一个临时库：删掉的事项不能串到下一条用例"""
    c = AppContext(tmp_path)
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def write_base(ctx):
    """把底稿真正写进临时库（不是草案），返回写进去的条数"""
    ok, detail = ctx.trip_svc.add_batch(DAY, BASE_TASKS,
                                        origin='金陵饭店', keep_order=True)
    assert ok, f'底稿没写进库，后面的删除判定就没意义：{detail}'
    return detail


def task_names(ctx, day=DAY):
    return [t.poi.name if t.poi is not None else t.title
            for t in ctx.trip_svc.day_view(day).tasks]


def plan_pois(out):
    plan = out.get('plan') or {}
    return [str(i.get('poi') or '') for d in plan.get('days') or []
            for i in d.get('items') or [] if i.get('poi')]


# ------------------------------------------------------------------ 判定口径
def soft(name):
    """软排除：槽位记下来 + 已写入的数据一条不少 + 不许说"删掉" """
    def check(rec):
        return (name in rec['avoid'] and rec['after'] == rec['before']
                and '删掉' not in rec['message'])
    return check


def hard(name):
    """硬删除：当天事项真少一条 + 那条真的不见 + 回复里点名 """
    def check(rec):
        return (rec['after'] == rec['before'] - 1
                and name not in rec['remaining']
                and '删掉' in rec['message'] and name in rec['message'])
    return check


def clear_day():
    """删除整天：这天的事项全清空（期望值）"""
    def check(rec):
        return rec['after'] == 0
    return check


def missing(name):
    """删一个没排进来的点：数据不变，但必须明确告诉你"行程里没有它" """
    def check(rec):
        return rec['after'] == rec['before'] and name in rec['message']
    return check


def plan_without(name):
    """软排除的另外半条：新草案里不能再出现它 """
    def check(rec):
        return name not in rec['poi_names'] and rec['after'] == rec['before']
    return check


CASES = [
    # ---------- 软排除（别去 / 不想去 / X 我不想去了 / X 别去了）----------
    ('P-01', '别去夫子庙', soft('夫子庙')),
    ('P-02', '不想去夫子庙', soft('夫子庙')),
    ('P-03', '夫子庙我不想去了', soft('夫子庙')),
    ('P-04', '不要去总统府', soft('总统府')),
    ('P-05', '不想要南京博物院', soft('南京博物院')),
    ('P-06', '老门东别去', soft('老门东')),
    ('P-07', '不去中山陵', soft('中山陵')),
    ('P-08', '别去牛首山', soft('牛首山文化旅游区')),
    ('P-09', '不想去老门东了', soft('老门东')),
    ('P-10', '总统府别去了', soft('总统府')),
    ('P-11', '夫子庙我不想再去', soft('夫子庙')),
    ('P-12', '不想要老门东', soft('老门东')),
    ('P-13', '别去总统府了', soft('总统府')),
    ('P-14', '不想去中山陵', soft('中山陵')),
    # ---------- X 换掉：产品口径是"别排这个"，不是"删数据" ----------
    ('P-15', '夫子庙换掉', soft('夫子庙')),
    ('P-16', '把总统府换掉', soft('总统府')),
    # ---------- 别安排 X ----------
    ('P-17', '别安排夫子庙', soft('夫子庙')),
    ('P-18', '不要安排总统府', soft('总统府')),
    # ---------- 硬删除（把X去掉 / 删掉 / 拿掉 / X不要了）----------
    ('P-19', '把总统府去掉', hard('总统府')),
    ('P-20', '把夫子庙删掉', hard('夫子庙')),
    ('P-21', '把南京博物院拿掉', hard('南京博物院')),
    ('P-22', '夫子庙不要了', hard('夫子庙')),
    ('P-23', '总统府不要了', hard('总统府')),
    ('P-24', '把总统府从行程里去掉', hard('总统府')),
    ('P-25', '删掉夫子庙', hard('夫子庙')),
    ('P-26', '取消总统府', hard('总统府')),
    ('P-27', '移掉总统府', hard('总统府')),
    ('P-28', '把南京博物院删除', hard('南京博物院')),
    ('P-29', '把总统府拿掉', hard('总统府')),
    ('P-30', '把夫子庙去掉吧', hard('夫子庙')),
    ('P-31', '把中山陵去掉', missing('中山陵')),
    ('P-32', '夫子庙删了吧', hard('夫子庙')),
    # ---------- 删除整天 ----------
    ('P-33', '这天清空吧', clear_day()),
    ('P-34', '这一天的安排全删掉', clear_day()),
    ('P-35', '把第二天清空', clear_day()),
    ('P-36', '整天删掉', clear_day()),
    ('P-37', '这天去掉', clear_day()),
    ('P-38', '把今天的夫子庙去掉', hard('夫子庙')),
    # ---------- 删除不存在的项：不许乱删，也不许装没事 ----------
    ('P-39', '把人民大会堂去掉', missing('人民大会堂')),
    ('P-40', '把故宫删掉', missing('故宫')),
    ('P-41', '把西湖去掉', missing('西湖')),
    ('P-42', '把迪士尼删掉', missing('迪士尼')),
    ('P-43', '把外滩去掉', missing('外滩')),
    ('P-44', '把东方明珠删掉', missing('东方明珠')),
    ('P-45', '今天的不去了', clear_day()),
    # ---------- 软排除的半条命：新草案里不许再出现 ----------
    ('P-46', '别去夫子庙', plan_without('夫子庙')),
    ('P-47', '不想去老门东', plan_without('老门东')),
    ('P-48', '不去中山陵', plan_without('中山陵')),
    ('P-49', '不去总统府', plan_without('总统府')),
    ('P-50', '别去牛首山', plan_without('牛首山文化旅游区')),
]

#: 已知未通过：句子 → 根因（报告里逐条对得上）
FAILING = {
    'P-15': '"换掉"被同时收进软排除词表和硬删除词表（_HARD_NO 含"换掉"），'
            '于是软排除也真删了数据、回复还说"删掉"',
    'P-16': '同上："把总统府换掉"同样按硬删除执行',
    'P-17': '"别安排"不在软排除词表（只认 别去/不去/不想去/不想要），avoid 没写入',
    'P-18': '同上："不要安排"没命中软排除词表',
    'P-31': '行程里没有这个点时静默通过：既不说明"没排进来"，也不说没删',
    'P-32': '口语"删了吧"不在动词表（只认 删掉/删除），整句没被识别成删除',
    'P-33': '没有"清空整天"的通道：排除词表靠解析地名，'
            '"这天/整天"不是地名，_trim_to_place 返回空，什么都没发生',
    'P-34': '同上：整天删除没有落点，当天 3 项原样保留',
    'P-35': '同上："第二天"不是地名，没有按序号定位整天的能力',
    'P-36': '同上："整天删掉"同样解析不出地点',
    'P-37': '同上："这天去掉"同样无落点',
    'P-38': '"今天"被 parse_date_range 当成新的行程日期，st.start 被覆写成 '
            '2026-11-01（草案只剩 1 天），删除按新日期找不到已排在 11-14 的点，什么都没删',
    'P-39': '删不存在的点时没有任何反馈：地名解析不到就被跳过，回复不提这件事',
    'P-40': '既没反馈又误匹配：把"故宫"解析成了"明故宫(地铁站)"写进 avoid（幸好当天没这个点，数据没动）',
    'P-41': '删不存在的点时没有任何反馈，用户无法判断"是删了还是没听懂"',
    'P-42': '同上：静默',
    'P-43': '同上：静默',
    'P-44': '同上：静默',
    'P-45': '"今天"覆写行程日期（同 P-38），且没有"清空整天"通道，data 未变',
}

PARAMS = [
    pytest.param(code, text, check, id=code,
                 marks=pytest.mark.xfail(reason=FAILING[code], strict=False)
                 if code in FAILING else ())
    for code, text, check in CASES
]


@pytest.mark.parametrize('code,text,check', PARAMS)
def test_prune_intent(ctx, code, text, check):
    session = f'intent-prune-{code}'
    run(ctx, BASE, session)                 # 会话底稿（日期 + 三个必去点）
    write_base(ctx)                         # 真正写进临时库的那三条
    before = task_names(ctx)
    out = run(ctx, text, session)
    remaining = task_names(ctx)
    rec = {
        'out': out,
        'message': str(out.get('message') or ''),
        'avoid': list((out.get('slots') or {}).get('avoid') or []),
        'drop': list((out.get('slots') or {}).get('drop') or []),
        'before': len(before),
        'after': len(remaining),
        'remaining': remaining,
        'poi_names': plan_pois(out),
        'today': '、'.join(before),
    }
    assert isinstance(out, dict), f'{code} 返回结构不对'
    assert check(rec), (
        f'{code} 「{text}」→ 事项 {rec["before"]}→{rec["after"]} '
        f'（{rec["today"]} → {rec["remaining"]}）；avoid={rec["avoid"]} '
        f'drop={rec["drop"]}；kind={out.get("kind")}')
