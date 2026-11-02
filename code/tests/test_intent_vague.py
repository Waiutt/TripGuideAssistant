# -*- coding: utf-8 -*-
"""含糊/澄清类意图（50 条）：这句话信息不全，系统该追问或明说，不该猜。

判定口径照任务契约，不照现状写死：
  · 指代不清与条件句**必须追问或明确说明**，且**不许臆造成具体地点**；
  · 纯应答应给出可继续的下一步（追问或确认），而不是硬排一版；
  · 情绪与否定至少要被接住（追问哪儿不满意），超短/空输入要么追问、要么把指令真正落进槽位。

分组：V-01~10 指代不清、V-11~20 纯应答、V-21~28 情绪与否定、
      V-29~36 条件句、V-37~45 超短输入、V-46~50 空输入与纯标点。
对照 `tests/test_intent_matrix.py` 的 I-41~I-50，本文件不重复那十句。

三种上下文：
  `base`  已有一版行程（还不知道住哪、吃哪）
  `full`  已有一版行程且住宿、吃饭都定了——此时也没有待答问题，任何"再排一版"都是多余的
  `fresh` 空会话（一开口就含糊）
"""
from datetime import date

import pytest

from nanjingtrip.app import AppContext
from nanjingtrip.consult import Consultant, ConsultStore

TODAY = date(2026, 11, 1)
BASE = '11月14号到16号去南京玩，想去南京博物院、总统府和夫子庙，每天两个点'
#: 住宿与吃饭都给全：会话里不再有待答问题，纯应答若还出一版方案就是"硬排"
FULL = BASE + '，住金陵饭店，午饭就近随便'


@pytest.fixture(scope='module')
def ctx(tmp_path_factory):
    c = AppContext(tmp_path_factory.mktemp('intent-vague'))
    c.settings.offline_mode = True
    c.apply_settings(c.settings)
    yield c
    c.close()


def run(ctx, text, session):
    con = Consultant(ctx.trip_svc, ConsultStore(ctx.db))
    return con.chat(session, text, TODAY)


def has_plan(out):
    return bool((out.get('plan') or {}).get('days'))


def asked(out):
    """追问或明确说明：kind 是 ask/notice"""
    return out.get('kind') in ('ask', 'notice', 'answer', 'clarify')


def pois(out):
    return sorted({str(i.get('poi')) for d in ((out.get('plan') or {}).get('days') or [])
                   for i in d.get('items') or [] if i.get('poi')})


def fabricated(out, before):
    """比基准多出来的具体地点——"那边""那个""老地方"不许被翻译成它"""
    return [p for p in pois(out) if p not in pois(before)]


def slot(out, key, default=None):
    return (out.get('slots') or {}).get(key, default)


def slot_moved(out, before, *keys):
    return any(slot(out, k) != slot(before, k) for k in keys)


# ---------- 各类契约对应的判定 ----------
def want_clarify(out, before):
    """指代不清：必须追问，且不许臆造地点"""
    return asked(out) and not fabricated(out, before)


def want_next_step(out, before):
    """纯应答：给出可继续的下一步（追问 / 带待答问题），而不是硬排一版"""
    return asked(out) or bool(out.get('questions'))


def want_held(out, before):
    """情绪与否定（算了/不满意）：接住这句，别又甩一版方案"""
    return asked(out)


def want_other_version(out, before):
    """"换一个"：要么追问换什么，要么真的换出一版不同的"""
    return asked(out) or pois(out) != pois(before)


def want_conditional(out, before):
    """条件句：必须追问或明确说明，不许当无条件指令直接执行"""
    return asked(out) and not slot_moved(out, before, 'per_day')


def want_short_taken(out, before):
    """超短输入：要么追问，要么真的把天数/日期落进槽位（不许静默丢弃）"""
    return asked(out) or slot_moved(out, before, 'trip_days', 'start', 'end')


def want_ask_only(out, before):
    """空会话里的含糊输入：追问即可，绝不能凭空出一版方案"""
    return asked(out) and not has_plan(out)


#: (编号, 用户这句话, 上下文, 判定)
CASES = [
    # ---------- 指代不清：不许把"那边/那个/老地方"猜成具体景点 ----------
    ('V-01', '就去那边吧', 'base', want_clarify),
    ('V-02', '还是那个地方', 'base', want_clarify),
    ('V-03', '老地方', 'base', want_clarify),
    ('V-04', '就那个塔', 'base', want_clarify),
    ('V-05', '你懂的那个', 'base', want_clarify),
    ('V-06', '前面说的那个', 'base', want_clarify),
    ('V-07', '那家店', 'base', want_clarify),
    ('V-08', '就昨天那个点', 'base', want_clarify),
    ('V-09', '换到那边去', 'base', want_clarify),
    ('V-10', '那个景点加进去', 'base', want_clarify),
    # ---------- 纯应答：已有完整方案时说"好"，该确认或问下一步 ----------
    ('V-11', '嗯嗯', 'full', want_next_step),
    ('V-12', '好的', 'full', want_next_step),
    ('V-13', '行吧', 'full', want_next_step),
    ('V-14', '可以呀', 'full', want_next_step),
    ('V-15', '随便吧', 'full', want_next_step),
    ('V-16', '你定', 'fresh', want_ask_only),
    ('V-17', '听你的安排', 'fresh', want_ask_only),
    ('V-18', '都行', 'fresh', want_ask_only),
    ('V-19', '没问题', 'fresh', want_ask_only),
    ('V-20', 'ok', 'fresh', want_ask_only),
    # ---------- 情绪与否定 ----------
    ('V-21', '算了', 'full', want_held),
    ('V-22', '还是算了吧', 'full', want_held),
    ('V-23', '不太满意', 'full', want_held),
    ('V-24', '换一个', 'full', want_other_version),
    ('V-25', '这个不好', 'fresh', want_ask_only),
    ('V-26', '太赶了', 'fresh', want_ask_only),
    ('V-27', '再想想', 'fresh', want_ask_only),
    ('V-28', '有点贵', 'fresh', want_ask_only),
    # ---------- 条件句：不许把"如果…"当"就…"直接执行 ----------
    ('V-29', '下雨就去博物院', 'full', want_conditional),
    ('V-30', '如果太累就少一个', 'full', want_conditional),
    ('V-31', '要是太晚就直接回酒店', 'full', want_conditional),
    ('V-32', '天气好就多走两个点', 'full', want_conditional),
    ('V-33', '不行就改天', 'fresh', want_ask_only),
    ('V-34', '有票就去', 'fresh', want_ask_only),
    ('V-35', '太贵就不去', 'fresh', want_ask_only),
    ('V-36', '堵车就坐地铁', 'fresh', want_ask_only),
    # ---------- 超短输入：要么追问，要么真把指令落实 ----------
    ('V-37', '就一天', 'base', want_short_taken),
    ('V-38', '两天吧', 'base', want_short_taken),
    ('V-39', '明天', 'base', want_short_taken),
    ('V-40', '半天', 'fresh', want_ask_only),
    ('V-41', '三', 'fresh', want_ask_only),
    ('V-42', '下午', 'fresh', want_ask_only),
    ('V-43', '周末', 'fresh', want_ask_only),
    ('V-44', '两天一夜', 'fresh', want_ask_only),
    ('V-45', '嗯？', 'fresh', want_ask_only),
    # ---------- 空输入与纯标点：不该被当成"再排一版" ----------
    ('V-46', '\n', 'fresh', want_ask_only),
    ('V-47', '？', 'fresh', want_ask_only),
    ('V-48', '。。。', 'fresh', want_ask_only),
    ('V-49', '　', 'fresh', want_ask_only),
    ('V-50', '！！', 'fresh', want_ask_only),
]

#: 已知未通过（根因分类见 code/docs/意图测试结果_问答含糊.md：K1~K6）
FAILING = {
    # K1 指代不清：没有指代消解，也没有追问，直接按已有槽位重排一版
    **{code: 'K1 指代不清未追问：kind=plan'
       for code in ('V-01', 'V-02', 'V-03', 'V-04', 'V-05',
                    'V-06', 'V-07', 'V-08', 'V-09', 'V-10')},
    # K2 纯应答被当成重排请求：方案齐了还再排一遍，也没有下一步提示
    **{code: 'K2 纯应答被当作重排：kind=plan 且无待答问题'
       for code in ('V-11', 'V-12', 'V-13', 'V-14', 'V-15')},
    # K3 情绪与否定没有入口
    **{code: 'K3 情绪/否定未接住：kind=plan'
       for code in ('V-21', 'V-22', 'V-23')},
    'V-24': 'K3 "换一个"重排出与上一版完全相同的一版',
    # K4 条件句：不追问，且"如果太累就少一个"被无条件执行（per_day 3→2）
    **{code: 'K4 条件句未追问：kind=plan'
       for code in ('V-29', 'V-30', 'V-31', 'V-32')},
    # K5 超短输入：指令被静默丢弃（天数没变，也没追问）
    'V-37': 'K5 超短指令静默丢弃："就一天"既没改天数也没追问',
    'V-38': 'K5 超短指令静默丢弃："两天吧"既没改天数也没追问',
}

MODES = {'base': BASE, 'full': FULL, 'fresh': None}

PARAMS = [
    pytest.param(code, text, mode, check, id=code,
                 marks=pytest.mark.xfail(reason=FAILING[code], strict=False)
                 if code in FAILING else ())
    for code, text, mode, check in CASES
]


@pytest.mark.parametrize('code,text,mode,check', PARAMS)
def test_intent_vague(ctx, code, text, mode, check):
    session = f'vague-{code}'
    seed = MODES[mode]
    before = run(ctx, seed, session) if seed else {'kind': '', 'slots': {}}
    if seed:
        if not has_plan(before):
            pytest.skip('前置：基准草案没出来（意图闸改动后口径变化）')
    if mode == 'full':
        # 住宿吃饭都给了，还没有待答问题：这时的"再排一版"才是硬排
        if before.get('questions'):
            pytest.skip('前置：基准会话仍有待答问题')
    out = run(ctx, text, session)
    assert isinstance(out, dict), f'{code} 返回结构不对'
    keys = ('start', 'end', 'per_day', 'trip_days')
    shown = {k: slot(out, k) for k in keys if slot(out, k) not in ('', None, 0, [])}
    detail = (f'{code}「{text}」[{mode}] → kind={out.get("kind")} 出了方案={has_plan(out)} '
              f'待答={[q.get("key") for q in out.get("questions") or []]} '
              f'新地点={fabricated(out, before)} 槽位={shown}')
    assert check(out, before), '含糊/澄清契约未满足：' + detail
