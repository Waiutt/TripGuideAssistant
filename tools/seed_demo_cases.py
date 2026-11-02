# -*- coding: utf-8 -*-
"""把三个测试案例的日期固化成可复现的演示数据（现场演示前跑一遍即可）。

对应 `07_测试留档/现场演示脚本.md`：

    2026-11-01  案例一：单天 4 点顺路（含批量录入 / 按序排 / 自动择优）
    2026-11-02  案例二：无序 5 点自动最优（最近邻 + 2-opt / ≤7 精确枚举）
    2026-11-03  案例三：延误 40 分钟后链式重算（delay 动作）

用法
----
    python tools/seed_demo_cases.py                # 只预览要做什么（默认）
    python tools/seed_demo_cases.py --apply        # 真正写入（已排过的日期自动跳过）
    python tools/seed_demo_cases.py --apply --reset   # 强制清空重建（会覆盖已有安排）
    python tools/seed_demo_cases.py --apply --case 3  # 只处理案例三
    python tools/seed_demo_cases.py --port 8765    # 指定服务端口（默认 8765）

说明：脚本通过 HTTP 接口操作，不改数据库文件；服务需先启动（tools/dev.py 或 main.py）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

CASES = {
    1: {
        'date': '2026-11-01',
        'name': '案例一·单天四点顺路',
        'origin': '南京邮电大学三牌楼校区',
        'keep_order': True,
        'items': [
            ('新街口', '新街口', 40),
            ('总统府', '总统府', 60),
            ('夫子庙', '夫子庙', 80),
            ('老门东', '老门东', 60),
        ],
        'delay': None,
    },
    2: {
        'date': '2026-11-02',
        'name': '案例二·无序五点自动最优',
        'origin': '南京站',
        'keep_order': False,          # 让系统自动排最短路
        'items': [
            ('鸡鸣寺', '鸡鸣寺', 50),
            ('玄武湖', '玄武湖公园', 50),
            ('南京博物院', '南京博物院', 50),
            ('先锋书店', '先锋书店五台山店', 50),
            ('大报恩寺', '大报恩寺遗址公园', 50),
        ],
        'delay': None,
    },
    3: {
        'date': '2026-11-03',
        'name': '案例三·延误 40 分钟链式重算',
        'origin': '南京邮电大学仙林校区',
        'keep_order': True,
        'items': [
            ('中山陵', '中山陵', 90),
            ('苜蓿园美食街', '苜蓿园', 40),
            ('颐和路民国公馆', '颐和路民国公馆区', 50),
            ('莫愁湖', '莫愁湖公园', 60),
        ],
        'delay': ('中山陵', 40),      # 最后把中山陵延长 40 分钟
    },
}


def call(port: int, path: str, body: dict | None = None) -> dict:
    url = f'http://127.0.0.1:{port}{path}'
    data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body else None
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode('utf-8'))


def show(port: int, day: str) -> None:
    st = call(port, f'/api/state?date={day}')
    print(f'   起点 {st.get("lodging") or "—"} · {st["stats"]["count"]} 项 · '
          f'通行 {st["stats"]["transit_text"]}')
    for it in st.get('items') or []:
        print(f'     {it["time"]}-{it["end"]}  {it["poi"]}  {it["minutes_text"]}')


def seed(port: int, idx: int, apply: bool, reset: bool = False) -> None:
    c = CASES[idx]
    day = c['date']
    print(f'\n=== {c["name"]}（{day}）===')
    if not apply:
        print(f'  [预览] 该日为空时写入 {len(c["items"])} 项，起点 {c["origin"]}'
              + (f'，最后延误：{c["delay"][0]} +{c["delay"][1]} 分钟' if c['delay'] else ''))
        return

    st = call(port, f'/api/state?date={day}')
    ids = [i['task_id'] for i in (st.get('items') or [])]
    if ids and not reset:
        # 这三天的数据就是"已有的规划记录"，默认绝不覆盖
        print(f'  已有 {len(ids)} 项，跳过（要按本脚本重建请加 --reset）')
        show(port, day)
        return
    if ids:
        call(port, '/api/command', {'action': 'delete_many', 'date': day,
                                    'task_ids': ids})
        print(f'  已清空 {len(ids)} 项（--reset）')
    call(port, '/api/command', {'action': 'set_lodging', 'date': day,
                                'name': c['origin']})
    out = call(port, '/api/command', {
        'action': 'add_batch', 'date': day, 'origin': c['origin'],
        'keep_order': c['keep_order'],
        'items': [{'title': t, 'poi': p, 'duration_min': d}
                  for t, p, d in c['items']]})
    print(f'  写入：{out.get("message")}')
    if c['delay']:
        st = call(port, f'/api/state?date={day}')
        target = next((i for i in st['items'] if c['delay'][0] in (i['poi'] or '')),
                      None)
        if target:
            out = call(port, '/api/command', {
                'action': 'delay', 'date': day,
                'task_id': target['task_id'], 'delay_min': c['delay'][1]})
            print(f'  延误：{out.get("ok")} {(out.get("message") or "")[:40]}')
    show(port, day)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='固化三个演示案例的数据')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--case', type=int, choices=[1, 2, 3], default=None)
    ap.add_argument('--apply', action='store_true', help='真正写入（默认只预览）')
    ap.add_argument('--reset', action='store_true',
                    help='清空该日再重建（默认：该日已有安排就跳过，绝不覆盖）')
    a = ap.parse_args(argv)
    idxs = [a.case] if a.case else [1, 2, 3]
    for i in idxs:
        seed(a.port, i, a.apply, a.reset)
    if not a.apply:
        print('\n以上为预览。确认无误后加 --apply 落库。')
    else:
        print('\n完成。现场演示时按 07_测试留档/现场演示脚本.md 的顺序操作即可。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
