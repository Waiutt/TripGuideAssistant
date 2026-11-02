# -*- coding: utf-8 -*-
"""测试期盯盘（只读）：把"系统实际给出的回答与方案"打出来。

看三处：
  1. consult_session —— 会话槽位 + 草案每天的项目与时长 + note（= 回答里的说明）
  2. trip / task     —— 真正写进库的行程
  3. 库与 WAL 体积   —— 长时间测试的体积变化

用法（在 code/ 下）：
    python ../tools/watch_live.py             # 打印最近状态
    python ../tools/watch_live.py --watch 5   # 每 5 秒看增量（只报新变化）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))

SLOT_KEYS = ('start', 'end', 'trip_days', 'window', 'per_day', 'pace', 'wake', 'day_end',
             'party', 'hotel', 'meal', 'taste', 'budget', 'must', 'avoid', 'drop',
             'friend_weekend', 'practical')


def db_path() -> Path:
    from nanjingtrip.config import SettingsService
    return Path(SettingsService().db_path())


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def show_session(row, verbose: bool = True) -> None:
    data = json.loads(row['payload'])
    slots = {k: data.get(k) for k in SLOT_KEYS
             if data.get(k) not in (None, '', [], {}, 0, False)}
    print(f"\n[会话 {row['session'][:12]}] {row['updated_at']}")
    if slots:
        print("  槽位:", json.dumps(slots, ensure_ascii=False))
    if data.get('note'):
        print("  说明:", str(data['note'])[:200])
    days = data.get('days') or []
    for d in days[:6]:
        items = d.get('items') or []
        line = " → ".join(
            f"{(i.get('poi') or i.get('title') or '')}"
            f"({i.get('duration_min') or '?'}分)" for i in items)
        print(f"    {d.get('date')} 周{d.get('weekday')}: {line}")
    if len(days) > 6:
        print(f"    …共 {len(days)} 天")
    if verbose and not days:
        print("    （还没有草案：可能停在追问）")


def show_trips(conn, limit: int = 4) -> None:
    rows = conn.execute(
        'SELECT trip_id, plan_date, title FROM trip ORDER BY plan_date DESC, trip_id DESC'
        ' LIMIT ?', (limit,)).fetchall()
    if not rows:
        print("\n[行程] 库里还没有行程")
        return
    print("\n[最近行程]")
    for t in rows:
        tasks = conn.execute(
            'SELECT title, start_ts, end_ts, seq FROM task WHERE trip_id = ?'
            ' ORDER BY COALESCE(start_ts, "99:99")', (t['trip_id'],)).fetchall()
        line = "、".join(f"{(x['start_ts'] or '--')[:5]}-{(x['end_ts'] or '--')[:5]} {x['title'][:12]}"
                        for x in tasks[:8])
        print(f"  {t['plan_date']}: {len(tasks)} 项 | {line}")


def snapshot(path: Path, verbose: bool = True):
    conn = connect(path)
    try:
        sessions = conn.execute(
            'SELECT session, payload, updated_at FROM consult_session'
            ' ORDER BY updated_at DESC LIMIT 2').fetchall()
        for row in sessions:
            show_session(row, verbose)
        show_trips(conn)
        wal = path.parent / (path.name + '-wal')
        sizes = f"主库 {path.stat().st_size // 1024} KB"
        if wal.exists():
            sizes += f" / WAL {wal.stat().st_size // 1024} KB"
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {sizes}")
        return {(r['session'], r['updated_at']) for r in sessions}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description='测试期盯盘（只读）')
    ap.add_argument('--watch', type=int, default=0, help='每 N 秒轮询一次，只报变化')
    args = ap.parse_args()
    path = db_path()
    if not path.exists():
        print('没有找到数据库：', path)
        return 1
    seen = snapshot(path)
    if args.watch <= 0:
        return 0
    print(f'\n—— 开始盯盘（每 {args.watch}s，Ctrl+C 结束）——')
    try:
        while True:
            time.sleep(args.watch)
            conn = connect(path)
            try:
                sessions = conn.execute(
                    'SELECT session, payload, updated_at FROM consult_session'
                    ' ORDER BY updated_at DESC LIMIT 2').fetchall()
            finally:
                conn.close()
            changed = {(r['session'], r['updated_at']) for r in sessions} - seen
            if changed:
                print('\n=== 有更新 ===')
                snapshot(path)
                seen |= changed
    except KeyboardInterrupt:
        print('\n盯盘结束')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
