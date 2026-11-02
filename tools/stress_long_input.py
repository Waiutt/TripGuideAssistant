"""整段自然语言输入 · 阶梯式压力测试（跑在 9 月隔离区）

用法（服务需在跑）：
    python tools/stress_long_input.py                # 只读预览，逐级加压并打印结果
    python tools/stress_long_input.py --live-write   # 额外做一次"写入 → 校验 → 清理"

为什么用 9 月：11 月是留档的 12 份真实案例，压测一律不碰；9 月是空档，
写入后可按 task_id 全量删除，跑完数据库回到原样。

每级都比上一级多一个难度维度：
    L1 单点            L2 双点带连接词      L3 三点带括号与指令尾巴
    L4 四点半口语+无时长吃饭                L5 无标点长句
    L6 起点声明 + 每处平均时长 + 5 点       L7 10 点大段混写
    L8 未知地点与办事事项（考验"待补地点"）  L9 极限长段（12 点/双餐/截止/预约/重复点）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'http://127.0.0.1:8765'
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'code'))


def api(path: str, params: dict | None = None):
    q = ('?' + urllib.parse.urlencode(params)) if params else ''
    with urllib.request.urlopen(f'{BASE}{path}{q}', timeout=300) as r:
        return json.loads(r.read().decode('utf-8'))


def cmd(action: str, payload: dict, day: str):
    body = json.dumps({'action': action, 'date': day, **payload}).encode('utf-8')
    req = urllib.request.Request(f'{BASE}/api/command?date={day}', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode('utf-8'))


#: 阶梯：编号、难度、日期（9 月隔离）、原文、必须出现的地点关键字、最少条目数
LADDER = [
    ('L1', '单点·有时长', '2026-09-01',
     '去玄武湖，1小时', ['玄武湖'], 1),
    ('L2', '双点·连接词', '2026-09-02',
     '先去玄武湖，1小时，然后去鸡鸣寺，50分钟', ['玄武湖', '鸡鸣寺'], 2),
    ('L3', '三点·括号时长+指令尾巴', '2026-09-03',
     '依次去：新街口（40分钟）、总统府（1小时）、老门东（1小时），帮我规划全天最优路线并生成行程表。',
     ['新街口', '总统府', '老门东'], 3),
    ('L4', '四点半口语·无时长吃饭', '2026-09-04',
     '明天上午先去玄武湖，然后去鸡鸣寺，中午吃饭，下午去先锋书店。',
     ['玄武湖', '鸡鸣寺', '吃饭', '先锋'], 4),
    ('L5', '五点·整段无标点', '2026-09-05',
     '我想明天上午去南京博物院看展两个小时中午在新街口吃个饭下午三点去先锋书店待一个半小时晚上去老门东逛逛',
     ['博物院', '新街口', '先锋', '老门东'], 4),
    ('L6', '五点·起点声明+每处平均时长', '2026-09-06',
     '明天我从东南大学四牌楼校区出发，想去鸡鸣寺、台城、北极阁、凯瑟琳广场、玄武湖，每个地方平均停留50分钟，帮我排一下。',
     ['鸡鸣寺', '台城', '北极阁', '凯瑟琳', '玄武湖'], 5),
    ('L7', '九点·大段混写（片段内多地点）', '2026-09-07',
     '我明天从南京邮电大学三牌楼校区出发：上午九点先到总统府参观1小时，然后去南京博物院看展2小时，'
     '中午在南京大牌档吃饭1小时，下午依次去夫子庙（1小时20分钟）、老门东（1小时）、先锋书店（40分钟），'
     '傍晚回玄武湖散步1小时，晚上去鸡鸣寺（40分钟）和台城（30分钟），最后回三牌楼校区。帮我排最优顺序。',
     ['总统府', '博物院', '大牌档', '夫子庙', '老门东', '先锋', '玄武湖', '鸡鸣寺', '台城'], 9),
    ('L8', '未知地点·办事事项（考验待补）', '2026-09-08',
     '明天上午先去银行取钱，然后去市政务服务中心办社保，中午在公司楼下吃个饭，下午去朋友家帮忙搬东西，'
     '傍晚去玄武湖走走1小时，晚上回鼓楼医院复查。',
     ['银行', '政务', '吃个饭', '朋友家', '玄武湖', '鼓楼医院'], 5),
    ('L9', '极限长段·12 点·双餐·截止·预约·重复点', '2026-09-09',
     '这次行程比较满，麻烦帮我仔细排：我周六早上从南京南站出发，先去玄武湖跑一圈大概40分钟，'
     '然后九点半到南京博物院看特展，大概两小时，记得博物院周一闭馆、要实名预约；'
     '十一点半去总统府，1小时，总统府也需要提前预约；中午十二点半在1912街区吃个饭，1小时左右；'
     '下午两点去新街口商圈逛街40分钟，再去夫子庙1小时20分钟，接着去老门东1小时，'
     '中途在先锋书店待40分钟；下午五点半要赶到鼓楼医院取报告，10:30 前必须抵达不适用，就按17:30算；'
     '晚上六点半在南京大牌档吃晚饭1个小时，餐后回玄武湖散步30分钟，最后回南京南站。'
     '另外重复一句：玄武湖和夫子庙我都想再去一次。全程尽量少走路，能骑车就骑车。',
     ['玄武湖', '博物院', '总统府', '1912', '新街口', '夫子庙', '老门东', '先锋', '鼓楼医院', '大牌档'], 10),
]


def pack_names(city: str = '南京') -> list[str]:
    try:
        from nanjingtrip.cities import get_city
        pack = get_city(city)
        return list(pack.names()) + list(pack.aliases())
    except Exception:                       # noqa: BLE001 拿不到就跳过地名校验
        return []


def check(text: str, out: dict, expect: list[str], expect_min: int) -> tuple[bool, list[str]]:
    """判分口径：① 该出现的地点一处都不能少 ② 条目数不少于预期 ③ 不许有"未解析"残渣"""
    items = out.get('items') or []
    unparsed = out.get('unparsed') or []
    blob = ' '.join(((i.get('poi') or '') + ' ' + (i.get('segment') or '')
                     + ' ' + (i.get('title') or '')) for i in items)
    problems: list[str] = []
    missing = [kw for kw in expect if kw not in blob]
    if missing:
        problems.append('缺地点 ' + '、'.join(missing))
    if len(items) < expect_min:
        problems.append(f'条目只有 {len(items)} 条（预期 ≥{expect_min}）')
    if unparsed:
        problems.append('有未解析片段 ' + ' / '.join(unparsed[:2]))
    # 附注/偏好的句子不许变成事项
    junk = [i.get('title') or '' for i in items
            if any(w in (i.get('title') or '') for w in ('预约', '尽量', '骑车', '重复'))]
    if junk:
        problems.append('混进交代表述 ' + '、'.join(junk))
    return (not problems), problems


def run_preview(case, names):
    tag, level, day, text, expect, expect_min = case
    t0 = time.perf_counter()
    d = cmd('preview_batch', {'text': text}, day)
    ms = int((time.perf_counter() - t0) * 1000)
    ok = bool(d.get('ok'))
    items = d.get('items') or []
    unparsed = d.get('unparsed') or []
    kinds = {}
    for it in items:
        kinds[it.get('kind') or '?'] = kinds.get(it.get('kind') or '?', 0) + 1
    passed, problems = check(text, d, expect, expect_min) if ok else (False, [d.get('message') or '解析失败'])
    return {
        'tag': tag, 'level': level, 'day': day, 'chars': len(text),
        'ok': ok, 'segments': len(d.get('segments') or []), 'split_by': d.get('split_by') or '-',
        'items': len(items), 'kinds': kinds,
        'needs_poi': sum(1 for i in items if i.get('needs_poi')),
        'unparsed': len(unparsed), 'unparsed_text': unparsed,
        'origin': d.get('origin') or '', 'ms': ms,
        'pass': passed, 'problems': problems,
        'items_detail': [(i.get('kind'), i.get('title'), i.get('poi'), i.get('duration_text'))
                         for i in items],
    }


def live_write_cycle(day: str, text: str):
    """真写一遍：批量写入 → 校验排程完整性/顺序 → 全部删除还原"""
    d = cmd('preview_batch', {'text': text}, day)
    items = [i for i in (d.get('items') or []) if i.get('poi') or i.get('kind') in ('block', 'lodging')]
    order_in = [i.get('poi') for i in items]
    payload = [{k: i.get(k) for k in ('kind', 'title', 'poi', 'duration_min',
                                      'category', 'desired_start')} for i in items]
    w = cmd('add_batch', {'items': payload, 'origin': d.get('origin') or None,
                          'keep_order': True}, day)
    st = api('/api/state', {'date': day})
    got = st.get('items') or []
    scheduled = [i for i in got if not i.get('unscheduled')]
    times = [i.get('time') for i in scheduled]
    order_out = [i.get('poi') for i in scheduled]
    res = {'write_msg': w.get('message'), 'sent': len(payload), 'written': len(got),
           'order_kept': order_out == [p for p in order_in if p in order_out],
           'time_monotonic': times == sorted(times),
           'unscheduled': [i.get('title') for i in got if i.get('unscheduled')],
           'transit': st.get('stats', {}).get('transit'),
           'first': times[0] if times else '', 'last': times[-1] if times else '',
           'timeline': [(i.get('time'), i.get('poi') or i.get('title')) for i in scheduled]}
    ids = [i.get('task_id') for i in got if i.get('task_id')]
    if ids:
        res['cleanup'] = cmd('delete_many', {'task_ids': ids}, day).get('message')
    res['after'] = api('/api/state', {'date': day}).get('stats', {}).get('count')
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--live-write', action='store_true', help='额外跑一次写入-校验-清理')
    ap.add_argument('--only', default='', help='只跑某一级，如 L7')
    args = ap.parse_args()

    city = api('/api/health').get('city') or '南京'
    names = pack_names(city)
    print(f'# 整段输入压力测试 · 城市 {city} · 已知地点 {len(names)} 个 '
          f'· 隔离区 2026-09-01~09\n')
    print('| 级 | 难度 | 字符 | 片段 | 断句 | 条目 | 类型(task/block/todo) | 待补 | '
          '未解析 | 耗时 | 判定 |')
    print('|---|---|---|---|---|---|---|---|---|---|---|')

    rows = []
    for case in LADDER:
        if args.only and case[0] != args.only:
            continue
        r = run_preview(case, names)
        rows.append(r)
        kinds = '/'.join(f'{r["kinds"].get(k, 0)}' for k in ('task', 'block', 'todo'))
        verdict = 'PASS' if r['pass'] else 'FAIL: ' + '；'.join(r['problems'])
        print(f'| {r["tag"]} | {r["level"]} | {r["chars"]} | {r["segments"]} | {r["split_by"]} | '
              f'{r["items"]} | {kinds} | {r["needs_poi"]} | {r["unparsed"]} | {r["ms"]} ms | {verdict} |')

    print('\n## 逐级明细\n')
    for r in rows:
        print(f'**{r["tag"]} · {r["level"]}**（{r["chars"]} 字，{r["ms"]} ms）'
              + (f' 起点={r["origin"]}' if r['origin'] else ''))
        for kind, title, poi, dur in r['items_detail']:
            print(f'  - [{kind}] {title} | {poi or "（无地点）"} | {dur}')
        if r['unparsed_text']:
            print(f'  - 未解析：{r["unparsed_text"]}')
        print()

    if args.live_write:
        print('\n## 写入闭环（顺序无关时刻的大段，验证"依次去"真的按序排）\n')
        res = live_write_cycle('2026-09-23',
                               '我从南京邮电大学三牌楼校区出发，依次去总统府1小时、南京博物院2小时、'
                               '南京大牌档1小时、夫子庙1小时20分钟、老门东1小时、先锋书店40分钟、'
                               '玄武湖1小时、鸡鸣寺40分钟、台城30分钟，帮我排最优顺序。')
        for k, v in res.items():
            print(f'- {k}：{v}')
        print('\n## 写入闭环（L7 原文，含用户明确时刻 -> 锁定项优先）\n')
        res2 = live_write_cycle('2026-09-24', LADDER[6][3])
        for k, v in res2.items():
            print(f'- {k}：{v}')
    fails = [r for r in rows if not r['pass']]
    print(f'\n小结：{len(rows)} 级中 {len(rows) - len(fails)} 级 PASS，{len(fails)} 级 FAIL')
    return 0 if not fails else 1


if __name__ == '__main__':
    raise SystemExit(main())
