# -*- coding: utf-8 -*-
"""真实旅游计划模拟：把"游客真的会说的话"整段丢进去，看系统排出什么、以及它到底诚不诚实。

与 `stress_long_input.py` 的分工：
    stress_long_input.py  压的是**长度与结构**（无标点、未知地点、极限长段），走 HTTP，只看解析
    simulate_trips.py     压的是**现实感**：模糊时长、可选项、条件分支、自我更正、口语别称、
                          赶车死线、孩子午睡、信息不全……在**临时数据目录**里跑完整链路
                          （解析→写入→排程→提示），真实 DeepSeek + 真实高德，跑完删目录，
                          绝不碰留档库。

场景刻意**不理想化**：不给每处都写时长，允许"看情况/人多就算了/能去就去"，
允许"哦不对，先去……"这类自我更正，允许条件句（下雨就换），也允许说着说着没说完。
判定标准不是"全解析出来"，而是：**该排的排进去、该报的如实报、不臆造、不假装懂**。

用法：
    python tools/simulate_trips.py            # 跑全部场景并打印报告
    python tools/simulate_trips.py --only R3  # 只跑某一个场景
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'code'))

from nanjingtrip import engine as engine_mod                    # noqa: E402
from nanjingtrip import hours as hours_mod                      # noqa: E402
from nanjingtrip.app import AppContext                          # noqa: E402
from nanjingtrip.config import SettingsService                  # noqa: E402
from nanjingtrip.profile import Profile                         # noqa: E402


SCENARIOS = [
    {
        'tag': 'G1', 'day': date(2026, 12, 5),
        'title': '三个地名·空格分隔（最小可用输入）',
        'profile': Profile(name='模拟-默认', identity='', effort='标准', pace='标准',
                           budget='不设', has_car=False, mode_priority='最快'),
        'text': '总统府 夫子庙 老门东',
        'must': ['总统府', '夫子庙', '老门东'], 'soft': [],
        'expect_flag': [], 'order_hint': ['总统府', '夫子庙', '老门东'],
        'note': '纯空格分隔，无任何时长与顺序说明',
    },
    {
        'tag': 'G2', 'day': date(2026, 12, 6),
        'title': '五个地名·顿号（城东一线，名字带景区后缀）',
        'profile': Profile(name='模拟-默认', identity='', effort='标准', pace='标准',
                           budget='不设', has_car=False, mode_priority='最快'),
        'text': '玄武湖、鸡鸣寺、台城、北极阁、凯瑟琳广场',
        'must': ['玄武湖', '鸡鸣寺', '台城', '北极阁', '凯瑟琳'], 'soft': [],
        'expect_flag': [], 'order_hint': ['玄武湖', '鸡鸣寺'],
        'note': '顿号分隔；台城/凯瑟琳要用别名匹配到城市包标准名',
    },
    {
        'tag': 'G3', 'day': date(2026, 12, 7),
        'title': '八个地名·换行 + 简称（南博 / 先锋书店）',
        'profile': Profile(name='模拟-学生', identity='学生', effort='能走能骑', pace='标准',
                           budget='省', has_car=False, mode_priority='骑行优先'),
        'text': ('南博\n先锋书店\n新街口\n总统府\n夫子庙\n老门东\n雨花台\n大报恩寺'),
        'must': ['博物院', '先锋', '新街口', '总统府', '夫子庙', '老门东', '雨花台', '大报恩寺'],
        'soft': [], 'expect_flag': [], 'order_hint': ['博物院', '先锋'],
        'note': '一行一个地名（手机备忘录粘贴的样子），含"南博"这种简称',
    },
    {
        'tag': 'G4', 'day': date(2026, 12, 8),
        'title': '十二个地名·空格长串（一天到底能排几个）',
        'profile': Profile(name='模拟-默认', identity='', effort='标准', pace='标准',
                           budget='不设', has_car=False, mode_priority='最快'),
        'text': ('中山陵 玄武湖 鸡鸣寺 台城 总统府 新街口 夫子庙 老门东 雨花台 大报恩寺 '
                 '先锋书店 南京博物院'),
        # 12 个点一天装不下：不要求全排进，只要求"能排的排进、排不下的有明确交代"
        'must': ['中山陵', '玄武湖', '鸡鸣寺', '总统府', '新街口', '夫子庙', '老门东'],
        'soft': [], 'expect_flag': [], 'order_hint': None,
        'note': '12 个地名没有任何时长：系统自己给默认时长，看一天装得下几个、装不下的怎么报',
    },
    {
        'tag': 'G5', 'day': date(2026, 12, 9),
        'title': '地名堆 + 出发点 + 回程（"从南京站出发…最后回南京站"）',
        'profile': Profile(name='模拟-游客', identity='', effort='标准', pace='标准',
                           budget='不设', has_car=False, mode_priority='公共交通优先'),
        'text': '从南京站出发：总统府、夫子庙、老门东、先锋书店，最后回南京站',
        'must': ['总统府', '夫子庙', '老门东', '先锋'], 'soft': [],
        'expect_flag': [], 'order_hint': ['总统府', '夫子庙', '老门东'],
        'note': '起点/返程声明 + 地名堆：起点要变成锚点，返程不能变成第 5 个景点',
    },
    {
        'tag': 'G6', 'day': date(2026, 12, 10),
        'title': '地名堆里混进包外地点与含糊指代',
        'profile': Profile(name='模拟-游客', identity='', effort='标准', pace='标准',
                           budget='不设', has_car=False, mode_priority='最快'),
        'text': '栖霞山、红山动物园、总统府、那个什么寺、夫子庙',
        'must': ['总统府', '夫子庙'],
        'soft': ['栖霞山', '红山动物园'],
        'expect_flag': ['待补', '未解析', '地点'],
        'order_hint': None,
        'note': '栖霞山/红山动物园不在离线包、"那个什么寺"是含糊指代：宁可待补，不许臆造',
    },
    {
        'tag': 'R1', 'day': date(2026, 12, 11),
        'title': '真实口语（模糊时长 + 可选项 + 括号附注 + 赶高铁）',
        'profile': Profile(name='模拟-学生', identity='学生', effort='能走能骑', pace='标准',
                           budget='省', has_car=False, mode_priority='骑行优先'),
        'text': ('我周六早上到南京站，想先去玄武湖转转，大概一两个小时吧；然后鸡鸣寺，看情况，'
                 '人多就算了；中午随便吃点，人均五十以内就行；下午总统府（这个必须去，我已经预约了），'
                 '再去南京博物院，能看多久看多久；傍晚夫子庙逛逛，晚上老门东吃点小吃。'
                 '别排太满，我晚上还要赶 21:00 的高铁回上海。'),
        'must': ['总统府'],
        'soft': ['玄武湖', '鸡鸣寺', '博物院', '夫子庙', '老门东'],
        'expect_flag': ['晚', '高铁'],
        'order_hint': None,
        'note': '括号里带附注（"这个必须去，我已经预约了"）、"看情况/人多就算了"、21:00 返程死线',
    },
]


def make_ctx() -> tuple[AppContext, Path]:
    """临时数据目录 + 借用真实密钥（只在本进程内用，跑完整目录删除）"""
    tmp = Path(tempfile.mkdtemp(prefix='nanjingtrip-sim-'))
    real = SettingsService()
    for name in ('config.json', 'secrets.json', 'profiles.json'):
        src = real.dir / name
        if src.exists():
            shutil.copy2(src, tmp / name)
    return AppContext(tmp), tmp


def run_scenario(ctx: AppContext, sc: dict) -> dict:
    svc = ctx.trip_svc
    day = sc['day']
    t0 = time.perf_counter()

    ctx.apply_profile(sc['profile'])
    prev = svc.preview_batch(sc['text'], day)
    items = prev.get('items') or []
    parse_ms = int((time.perf_counter() - t0) * 1000)

    payload = [{k: i.get(k) for k in ('kind', 'title', 'poi', 'duration_min',
                                      'category', 'desired_start')}
               for i in items if i.get('poi') or i.get('kind') in ('block', 'lodging')]
    write_msg = svc.add_batch(day, payload, origin=prev.get('origin') or None,
                              keep_order=True)[1]

    view = svc.day_view(day)
    opts = svc.options(lodging=view.lodging, plan_date=day)
    result = engine_mod.schedule(view.tasks, opts, svc.transit.as_fn(), svc._mode_pick())

    timeline, hours_bad = [], []
    for it in result.items:
        poi = it.task.poi
        timeline.append({
            'start': f'{it.start_min // 60:02d}:{it.start_min % 60:02d}',
            'end': f'{it.end_min // 60:02d}:{it.end_min % 60:02d}',
            'title': it.task.title, 'poi': poi.name if poi else '（无地点）',
            'mode': (it.task.mode.value if it.task.mode else ''),
            'leg': (it.leg_before.duration_min if it.leg_before else 0),
            'locked': bool(it.task.is_locked),
        })
        hint = hours_mod.hours_hint(poi, it.start_min, it.end_min)
        if hint:
            hours_bad.append((poi.name if poi else it.task.title, hint,
                              f'{it.start_min // 60:02d}:{it.start_min % 60:02d}'))
    closed_bad = [(t.poi.name, hours_mod.closed_detail(t.poi, day))
                  for t in view.tasks
                  if t.poi is not None and hours_mod.closed_detail(t.poi, day)]

    blob = ' '.join(f'{t["poi"]}{t["title"]}' for t in timeline)
    places_all = blob + ' ' + ' '.join(prev.get('unparsed') or [])

    def _soft_hit(name: str) -> bool:
        """软期望：允许标准名比输入多字（"红山动物园"→"红山森林动物园"）"""
        if name in places_all:
            return True
        key = name.replace('森林', '').replace('公园', '').replace('景区', '')
        return key in places_all.replace('森林', '').replace('公园', '').replace('景区', '')

    lost_soft = [p for p in sc['soft'] if not _soft_hit(p)]
    flags = ' '.join([c.detail for c in result.conflicts]
                     + [c.detail for c in result.notices]
                     + (prev.get('unparsed') or []))

    # 条件句检查：两套方案不该同时排进去
    forbid_hit = []
    if 'forbid_all_of' in sc:
        hit = [p for p in sc['forbid_all_of'] if p in blob]
        if len(hit) == len(sc['forbid_all_of']):
            forbid_hit = hit

    # 顺序检查（改口后的顺序）
    order_ok = None
    if sc.get('order_hint'):
        pos = [(blob.find(p), p) for p in sc['order_hint']]
        pos = [(i, p) for i, p in pos if i >= 0]
        order_ok = [p for _, p in sorted(pos)] == sc['order_hint']

    missing_must = [p for p in sc['must'] if p not in blob]
    flag_hit = [k for k in sc['expect_flag'] if k in flags]

    return {
        'tag': sc['tag'], 'title': sc['title'], 'day': day.isoformat(),
        'profile': sc['profile'].name, 'note': sc['note'],
        'chars': len(sc['text']), 'parse_ms': parse_ms,
        'items': len(items), 'needs_poi': sum(1 for i in items if i.get('needs_poi')),
        'unparsed': prev.get('unparsed') or [], 'origin': prev.get('origin') or '',
        'written': len(payload), 'write_msg': write_msg,
        'scheduled': len(result.items), 'unscheduled': [t.title for t in result.unscheduled],
        'transit': sum(i.leg_before.duration_min for i in result.items if i.leg_before),
        'first': timeline[0]['start'] if timeline else '',
        'last': timeline[-1]['end'] if timeline else '',
        'modes': {m: sum(1 for t in timeline if t['mode'] == m)
                  for m in {t['mode'] for t in timeline if t['mode']}},
        'hours_bad': hours_bad, 'closed_bad': closed_bad,
        'conflicts': [(c.kind, c.detail) for c in result.conflicts][:8],
        'notices': [c.detail for c in result.notices][:10],
        'timeline': timeline,
        'missing_must': missing_must, 'lost_soft': lost_soft,
        'flag_hit': flag_hit, 'forbid_hit': forbid_hit, 'order_ok': order_ok,
        'return_by': opts.return_by,
    }


def report(rows: list[dict]) -> None:
    print('# 真实旅游计划模拟（临时库 · 真实 LLM + 真实高德 · 刻意不理想化）\n')
    print('| 场景 | 日期 | 档案 | 解析 | 待补 | 未解析 | 写入 | 排入/未排 | 通行 | 时段 | 硬伤 |')
    print('|---|---|---|---|---|---|---|---|---|---|---|')
    for r in rows:
        hard = []
        if r['missing_must']:
            hard.append('必须项没排进:' + '、'.join(r['missing_must']))
        if r['lost_soft']:
            hard.append('整句消失:' + '、'.join(r['lost_soft']))
        if r['forbid_hit']:
            hard.append('两套条件方案同时排进')
        if r['order_ok'] is False:
            hard.append('改口后顺序没生效')
        if r['hours_bad']:
            hard.append(f'超营业时间 {len(r["hours_bad"])} 处')
        print(f'| {r["tag"]} {r["title"]} | {r["day"]} | {r["profile"]} | {r["items"]} 项 | '
              f'{r["needs_poi"]} | {len(r["unparsed"])} | {r["written"]} | '
              f'{r["scheduled"]}/{len(r["unscheduled"])} | {r["transit"]} 分 | '
              f'{r["first"]}–{r["last"]} | {"；".join(hard) or "—"} |')

    for r in rows:
        print(f'\n## {r["tag"]} · {r["title"]}\n')
        print(f'- {r["day"]}｜档案「{r["profile"]}」')
        print(f'- 真实输入（{r["chars"]} 字）：{r["note"]}')
        print(f'- 解析 {r["items"]} 项（待补 {r["needs_poi"]}、未解析 {len(r["unparsed"])}）'
              f'｜起点 {r["origin"] or "未声明"}｜{r["parse_ms"]} ms')
        print(f'- 写入：{r["write_msg"]}')
        print(f'- 排入 {r["scheduled"]}，未排入 {r["unscheduled"] or "无"}；'
              f'通行 {r["transit"]} 分；方式 {r["modes"] or "—"}；时段 {r["first"]}–{r["last"]}')
        if r['missing_must']:
            print(f'- ❌ 必须去的地点没排进去：{r["missing_must"]}')
        if r['lost_soft']:
            print(f'- ❌ 提到的地点整句消失：{r["lost_soft"]}')
        if r['forbid_hit']:
            print(f'- ❌ 条件句两套方案同时排进：{r["forbid_hit"]}')
        if r['order_ok'] is False:
            print('- ❌ 自我更正后的顺序没生效')
        elif r['order_ok'] is True:
            print('- ✅ 改口后的顺序正确')
        print('- 开放时段：' + ('全部在营业时间内 ✅' if not r['hours_bad']
                            else f'⚠ {r["hours_bad"]}'))
        if r['closed_bad']:
            print(f'- ⚠ 撞闭馆日：{r["closed_bad"]}')
        if r['flag_hit']:
            print(f'- 如实上报（命中预期提示）：{r["flag_hit"]}')
        print('- 时间轴：')
        for t in r['timeline']:
            print(f'    {t["start"]}–{t["end"]}  {t["title"]}｜{t["poi"]}'
                  + ('｜[锁定]' if t['locked'] else '')
                  + (f'｜{t["mode"]}' if t['mode'] else '')
                  + (f'｜前段 {t["leg"]} 分' if t['leg'] else ''))
        if r['conflicts']:
            print('- 冲突：')
            for k, d in r['conflicts']:
                print(f'    · [{k}] {d}')
        if r['notices']:
            print('- 主动报告：')
            for n in r['notices']:
                print(f'    · {n}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', default='')
    args = ap.parse_args()
    rows = []
    for sc in SCENARIOS:
        if args.only and sc['tag'] != args.only:
            continue
        ctx, tmp = make_ctx()
        try:
            if not ctx.degrade.llm_ok() or not ctx.degrade.map_ready():
                print(f'!! {sc["tag"]} 需要在线：llm_ok={ctx.degrade.llm_ok()} '
                      f'map_ready={ctx.degrade.map_ready()}')
            rows.append(run_scenario(ctx, sc))
        finally:
            ctx.close()
            shutil.rmtree(tmp, ignore_errors=True)
    report(rows)
    hard = [r['tag'] for r in rows if (r['missing_must'] or r['lost_soft']
                                       or r['forbid_hit'] or r['order_ok'] is False)]
    print(f'\n小结：{len(rows)} 个场景，无硬伤 {len(rows) - len(hard)} 个'
          + (f'，硬伤 {hard}' if hard else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
