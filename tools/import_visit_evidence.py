# -*- coding: utf-8 -*-
"""把人工核对过的"官方口径/平台登记"游览时长导进证据表。

为什么需要它：`visit_evidence` 的 official / ota 两级**不可能由代码猜**，
必须有人核对景区官网、官方公众号、博物馆推荐参观路线后录入。这个脚本就是那个入口。

用法：
    python tools/import_visit_evidence.py --file 游览时长.json          # 预演（不写库）
    python tools/import_visit_evidence.py --file 游览时长.json --apply  # 真写

JSON 形状（数组，字段少而明确）：
    [
      {"name": "南京博物院", "minutes": 120, "source": "official",
       "note": "官网『参观约 2 小时』", "checked_at": "2026-10-31"},
      {"name": "夫子庙", "minutes": 90, "source": "ota",
       "note": "多个平台登记 1.5 小时"}
    ]
字段约束：
    * `source` 只能是 official / ota（estimate 由系统现算，不要手填）
    * `minutes` 为正整数；`note` 写清出处，便于以后复核
    * 同一 (name, source) 重复导入会覆盖（以最后一次核对为准）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'code'))

from nanjingtrip.app import AppContext            # noqa: E402
from nanjingtrip.evidence import PRIORITY         # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True, help='核对后的 JSON 文件')
    ap.add_argument('--apply', action='store_true', help='真写入（默认只预演）')
    ap.add_argument('--city', default='南京')
    a = ap.parse_args()

    raw = json.loads(Path(a.file).read_text(encoding='utf-8'))
    if not isinstance(raw, list):
        print('文件内容必须是数组')
        return 2

    ok, bad = [], []
    for row in raw:
        name = str(row.get('name') or '').strip()
        source = str(row.get('source') or '').strip()
        try:
            minutes = int(row.get('minutes'))
        except (TypeError, ValueError):
            minutes = 0
        if not name or source not in ('official', 'ota') or minutes <= 0:
            bad.append(row)
            continue
        ok.append({'name': name, 'source': source, 'minutes': minutes,
                   'note': str(row.get('note') or '')})

    print(f'可导入 {len(ok)} 条，跳过 {len(bad)} 条')
    for row in ok:
        print(f'  · {row["name"]} ← {PRIORITY[row["source"]]} 级（{row["source"]}）'
              f' {row["minutes"]} 分钟  {row["note"][:40]}')
    for row in bad:
        print(f'  ✗ 跳过：{row}')

    if not a.apply:
        print('\n预演结束（加 --apply 才写库）')
        return 0

    ctx = AppContext()
    try:
        for row in ok:
            ctx.evidence.add(row['name'], row['minutes'], row['source'],
                             city=a.city, note=row['note'])
        print(f'\n已写入 {len(ok)} 条证据（城市：{a.city}）')
    finally:
        ctx.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
