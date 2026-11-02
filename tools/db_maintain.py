# -*- coding: utf-8 -*-
"""数据治理命令行：查看库规模、清理过期缓存、截断 WAL。

用法（在 code/ 下）：
    python ../tools/db_maintain.py --stats     # 只看规模
    python ../tools/db_maintain.py            # 执行维护
    python ../tools/db_maintain.py --data-dir "C:\\Users\\我\\AppData\\Local\\NanjingTrip"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))

from nanjingtrip.storage import Database          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='南京行程应用的数据维护')
    ap.add_argument('--data-dir', default='',
                    help='数据目录（默认取应用配置里的路径）')
    ap.add_argument('--stats', action='store_true', help='只打印规模，不做维护')
    args = ap.parse_args()

    if args.data_dir:
        db_path = Path(args.data_dir) / 'nanjingtrip.db'
    else:
        from nanjingtrip.config import SettingsService
        db_path = SettingsService().db_path()
    if not Path(db_path).exists():
        print(f'没有找到数据库：{db_path}')
        return 1

    db = Database(db_path)
    try:
        before = db.stats()
        kb = lambda n: f'{n / 1024:.0f} KB'
        print(f'库文件：{db_path}')
        print(f'  主库 {kb(before["db_bytes"])} / WAL {kb(before["wal_bytes"])}')
        print(f'  行程 {before["trip"]} 天 / 事项 {before["task"]} 项 / '
              f'地点 {before["poi"]} / 通行缓存 {before["transit_leg"]} / '
              f'情报 {before["poi_intel"]} / 版本 {before["trip_version"]}')
        if args.stats:
            return 0
        report = db.maintain(force_checkpoint=True)
        after = db.stats()
        print('维护结果：')
        print(f'  清理通行缓存 {report["transit_pruned"]} 条、'
              f'过期情报 {report["intel_pruned"]} 条')
        print(f'  WAL 截断：{"是" if report["checkpointed"] else "否"}'
              f'（{kb(before["wal_bytes"])} → {kb(after["wal_bytes"])}）')
        print(f'  VACUUM：{"是" if report["vacuumed"] else "否"}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
